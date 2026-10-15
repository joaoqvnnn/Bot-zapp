"""
Larizinha Store — StockAlerts
Serviço de alertas de estoque (baixo / esgotado / reabastecido).

Responsabilidades (DIM):
- Monitorar produtos com estoque abaixo do limite (`stock_min`) ou esgotados.
- Disparar alerta para ADMINS no privado (editável key="alerta_estoque").
- Auto-postar no CANAL quando:
    * estoque é ABAIXO do mínimo       → "canal_estoque" (reabastecido)
    * estoque é REABASTECIDO (> 0)     → "canal_estoque"
    * estoque fica ESGOTADO            → "canal_estoque" com aviso
- Persistir alertas em `alerts` (para o painel admin).
- NUNCA acumula mensagens: o worker/handler é quem envia/editando.
- Respeita `AUTO_LOW_STOCK` + `AUTO_LOW_STOCK_THRESHOLD`.
- Cooldown por produto (Redis) para não spammar.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from bot.config import settings
from bot.database import (
    Alert,
    Channel,
    Product,
    session_scope,
)
from bot.logger import info, logger, warning
from bot.redis_client import Counters, cache_delete, cache_get, cache_set


# ============================================================
# 🔑 CONSTANTES
# ============================================================
CACHE_NS = "stock_alerts"
COOLDOWN_PRODUCT = "cooldown:product:{pid}"      # 1 alerta a cada X min
DEFAULT_COOLDOWN_SEC = 15 * 60                   # 15 minutos
STATES_CACHE_KEY = "stock_state"

STATE_OK = "ok"
STATE_LOW = "low"
STATE_OUT = "out"
STATE_REFILLED = "refilled"

MESSAGE_KEY_ALERTA_ESTOQUE = "alerta_estoque"
MESSAGE_KEY_CANAL_ESTOQUE = "canal_estoque"


# ============================================================
# 🧱 DATACLASS
# ============================================================
@dataclass
class StockEvent:
    """Evento de mudança de estoque."""

    product_id: int
    product_name: str
    prev_stock: int
    new_stock: int
    state: str            # "low" | "out" | "refilled" | "ok"
    threshold: int
    occurred_at: datetime

    def is_alert(self) -> bool:
        return self.state in (STATE_LOW, STATE_OUT)


# ============================================================
# 🧠 SERVIÇO
# ============================================================
class StockAlerts:
    """
    Serviço de alertas de estoque.

    Uso (após qualquer alteração de estoque):
        event = await stock_alerts.check_product(product_id)
        if event:
            # o worker/handler notifica admins e canal
    """

    def __init__(self) -> None:
        self._enabled = bool(settings.AUTO_LOW_STOCK)
        self._threshold = max(1, int(settings.AUTO_LOW_STOCK_THRESHOLD or 5))

    # --------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def threshold(self) -> int:
        return self._threshold

    # --------------------------------------------------------
    # 🔎 ESTADO ANTERIOR (cache)
    # --------------------------------------------------------
    async def _get_prev_state(self, product_id: int) -> Optional[int]:
        """Estoque anterior cacheado (ou None)."""
        data = await cache_get(CACHE_NS, f"{product_id}")
        if not data:
            return None
        try:
            return int(data.get("stock"))
        except Exception:
            return None

    async def _save_state(self, product_id: int, stock: int, state: str) -> None:
        await cache_set(
            CACHE_NS,
            f"{product_id}",
            {"stock": int(stock), "state": state},
            ttl=60 * 60 * 24 * 7,  # 7 dias
        )

    # --------------------------------------------------------
    # ⏱️ COOLDOWN
    # --------------------------------------------------------
    async def _check_cooldown(self, product_id: int) -> bool:
        """
        Retorna True se pode alertar (não está em cooldown).
        """
        from bot.redis_client import get_redis, Keys

        try:
            r = await get_redis()
            key = Keys.k(COOLDOWN_PRODUCT, pid=product_id)
            ok = await r.set(key, "1", ex=DEFAULT_COOLDOWN_SEC, nx=True)
            return bool(ok)
        except Exception:
            return True

    async def _clear_cooldown(self, product_id: int) -> None:
        from bot.redis_client import get_redis, Keys

        try:
            r = await get_redis()
            await r.delete(Keys.k(COOLDOWN_PRODUCT, pid=product_id))
        except Exception:
            pass

    # --------------------------------------------------------
    # 🎯 CHECK PRINCIPAL
    # --------------------------------------------------------
    async def check_product(self, product_id: int) -> Optional[StockEvent]:
        """
        Verifica o estado atual do produto e retorna um StockEvent se houve
        mudança relevante (low / out / refilled). Caso contrário, None.
        """
        # Carrega produto
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Product).where(Product.id == product_id)
                )
                product: Optional[Product] = res.scalar_one_or_none()
                if product is None:
                    return None

                # Snapshot (evita lazy loading fora da sessão)
                snap = {
                    "id": int(product.id),
                    "name": str(product.name or ""),
                    "stock": int(product.stock or 0),
                    "stock_min": int(product.stock_min or 0),
                    "unlimited": bool(product.unlimited),
                    "visible": bool(product.visible),
                    "sold_out": bool(product.sold_out),
                }
        except Exception as exc:
            logger.exception(f"check_product falhou ({product_id}): {exc}")
            return None

        if snap["unlimited"]:
            return None

        new_stock: int = snap["stock"]
        min_stock: int = snap["stock_min"] if snap["stock_min"] > 0 else self._threshold

        # Estado atual
        if new_stock <= 0:
            state = STATE_OUT
        elif new_stock <= min_stock:
            state = STATE_LOW
        else:
            state = STATE_OK

        # Estado anterior
        prev_stock = await self._get_prev_state(product_id)

        # Primeira vez que vemos o produto → apenas salva
        if prev_stock is None:
            await self._save_state(product_id, new_stock, state)
            return None

        # Nada mudou
        if prev_stock == new_stock:
            await self._save_state(product_id, new_stock, state)
            return None

        # Reabastecido
        if prev_stock <= 0 and new_stock > 0:
            await self._save_state(product_id, new_stock, STATE_REFILLED)
            # limpa cooldown para permitir postar no canal
            await self._clear_cooldown(product_id)
            return StockEvent(
                product_id=product_id,
                product_name=snap["name"],
                prev_stock=prev_stock,
                new_stock=new_stock,
                state=STATE_REFILLED,
                threshold=min_stock,
                occurred_at=datetime.now(timezone.utc),
            )

        # Passou de OK → LOW / OUT
        if state in (STATE_LOW, STATE_OUT):
            # cooldown evita spam de alerta
            if not await self._check_cooldown(product_id):
                await self._save_state(product_id, new_stock, state)
                return None
            await self._save_state(product_id, new_stock, state)
            return StockEvent(
                product_id=product_id,
                product_name=snap["name"],
                prev_stock=prev_stock,
                new_stock=new_stock,
                state=state,
                threshold=min_stock,
                occurred_at=datetime.now(timezone.utc),
            )

        # Passou de LOW/OUT → OK (mas não estava esgotado)
        if state == STATE_OK and prev_stock <= min_stock:
            await self._save_state(product_id, new_stock, state)
            return StockEvent(
                product_id=product_id,
                product_name=snap["name"],
                prev_stock=prev_stock,
                new_stock=new_stock,
                state=STATE_REFILLED,
                threshold=min_stock,
                occurred_at=datetime.now(timezone.utc),
            )

        # Fallback: apenas salva
        await self._save_state(product_id, new_stock, state)
        return None

    # --------------------------------------------------------
    # 📊 SCAN GERAL (worker periódico)
    # --------------------------------------------------------
    async def scan_all(self) -> List[StockEvent]:
        """
        Varre TODOS os produtos visíveis e retorna a lista de eventos
        (low / out / refilled). Ideal para rodar a cada X minutos.
        """
        events: List[StockEvent] = []
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Product.id).where(
                        Product.unlimited == False,   # noqa: E712
                        Product.visible == True,      # noqa: E712
                    )
                )
                ids = [int(r[0]) for r in res.all()]

            for pid in ids:
                ev = await self.check_product(pid)
                if ev is not None:
                    events.append(ev)
            return events
        except Exception as exc:
            logger.exception(f"scan_all falhou: {exc}")
            return []

    # --------------------------------------------------------
    # 💾 PERSISTIR ALERTA
    # --------------------------------------------------------
    async def persist_alert(
        self,
        *,
        category: str = "stock",
        severity: str = "warning",
        title: str,
        message: str,
    ) -> None:
        """
        Grava o alerta na tabela `alerts` para o painel admin.
        Não é para o usuário final — é interno.
        """
        try:
            async with session_scope() as session:
                alert = Alert(
                    category=category,
                    severity=severity,
                    title=title[:200],
                    message=message,
                    resolved=False,
                )
                session.add(alert)
        except Exception as exc:
            logger.debug(f"persist_alert falhou: {exc}")

    # --------------------------------------------------------
    # 📡 CANAIS QUE RECEBEM AUTO-POST DE ESTOQUE
    # --------------------------------------------------------
    async def channels_for_stock(self) -> List[Dict[str, Any]]:
        """Retorna os canais marcados com `auto_post_stock=True`."""
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Channel).where(Channel.auto_post_stock == True)  # noqa: E712
                )
                rows = res.scalars().all()
                return [
                    {
                        "chat_id": int(c.chat_id),
                        "username": c.username,
                        "title": c.title,
                        "type": c.type.value if hasattr(c.type, "value") else str(c.type),
                    }
                    for c in rows
                ]
        except Exception as exc:
            logger.debug(f"channels_for_stock falhou: {exc}")
            return []

    # --------------------------------------------------------
    # 💚 HEALTHCHECK
    # --------------------------------------------------------
    async def healthcheck(self) -> bool:
        try:
            from bot.database import healthcheck as db_health

            return await db_health()
        except Exception:
            return False


# ============================================================
# 📌 SINGLETON
# ============================================================
stock_alerts = StockAlerts()


# ============================================================
# 🧩 HELPERS DE MÓDULO
# ============================================================
async def notify_stock_change(product_id: int) -> Optional[StockEvent]:
    """Atalho: verifica um produto e retorna o evento (se houver)."""
    return await stock_alerts.check_product(product_id)


async def invalidate_stock_cache(product_id: Optional[int] = None) -> None:
    """
    Chamado pelo painel admin quando o estoque é editado manualmente,
    para forçar o próximo scan a detectar corretamente.
    """
    try:
        if product_id is not None:
            await cache_delete(CACHE_NS, f"{product_id}")
        else:
            await cache_delete(CACHE_NS, "*")
    except Exception:
        pass


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "StockAlerts",
    "StockEvent",
    "stock_alerts",
    "notify_stock_change",
    "invalidate_stock_cache",
    "MESSAGE_KEY_ALERTA_ESTOQUE",
    "MESSAGE_KEY_CANAL_ESTOQUE",
    "STATE_OK",
    "STATE_LOW",
    "STATE_OUT",
    "STATE_REFILLED",
]
