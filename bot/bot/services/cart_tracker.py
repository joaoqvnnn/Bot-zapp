"""
Larizinha Store — CartTracker
Rastreia carrinhos abandonados e dispara a automação de reengajamento.

Fluxo real (DIM):
- Usuário abre "Detalhes do Produto" → `track_view(user_id, product_id)`.
- Se em N minutos ele NÃO comprar → `worker` chama `collect_due()` e dispara
  `carrinho_abandonado` (mensagem editável key="carrinho").
- Ao comprar → `mark_converted(user_id, product_id)` cancela o rastreio.
- Ao fechar o detalhe sem comprar → o rastreio permanece até o delay.
- NUNCA acumula mensagens: quem envia é o handler, este serviço só fornece dados.
- Estado persistido em Redis (Sorted Set + Hash) com TTL configurável.
- Respeita `AUTO_CART_ABANDONED` e `AUTO_CART_ABANDONED_DELAY`.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from bot.config import settings
from bot.logger import logger
from bot.redis_client import Counters, Keys, get_redis


# ============================================================
# 🔑 CONSTANTES
# ============================================================
CART_QUEUE_KEY = "cart:pending"          # Sorted Set (score = timestamp alvo)
CART_HASH_KEY = "cart:item:{uid}"        # Hash com dados do carrinho do usuário
CART_MAX_ITEMS_PER_USER = 1              # 1 produto por usuário por vez
CART_STATE_TTL = 60 * 60 * 24            # 24h (limpeza automática)

# Chaves lógicas de mensagem editável
MESSAGE_KEY_CARRINHO = "carrinho"
MESSAGE_KEY_PROMO = "promo_nova"


# ============================================================
# 🧱 DATACLASS
# ============================================================
@dataclass
class CartItem:
    """Dados do carrinho abandonado."""

    user_id: int
    product_id: int
    product_name: str
    price: float
    opened_at: int
    due_at: int
    chat_id: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "price": self.price,
            "opened_at": self.opened_at,
            "due_at": self.due_at,
            "chat_id": self.chat_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CartItem":
        return cls(
            user_id=int(d["user_id"]),
            product_id=int(d["product_id"]),
            product_name=str(d.get("product_name", "")),
            price=float(d.get("price", 0.0)),
            opened_at=int(d.get("opened_at", 0)),
            due_at=int(d.get("due_at", 0)),
            chat_id=int(d.get("chat_id", 0)),
        )


# ============================================================
# 🧠 SERVIÇO
# ============================================================
class CartTracker:
    """
    Serviço de rastreamento de carrinho abandonado.

    Uso típico (handler de "detalhe_produto"):
        await cart_tracker.track_view(user_id, chat_id, product)

    Ao aprovar a compra:
        await cart_tracker.mark_converted(user_id, product_id)

    No worker (a cada X segundos):
        for item in await cart_tracker.collect_due():
            # envia mensagem editável "carrinho"
            ...
            await cart_tracker.mark_notified(item)
    """

    def __init__(self) -> None:
        self._enabled = bool(settings.AUTO_CART_ABANDONED)
        self._delay_min = max(1, int(settings.AUTO_CART_ABANDONED_DELAY or 15))

    # --------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def delay_seconds(self) -> int:
        return self._delay_min * 60

    # --------------------------------------------------------
    def _hash_key(self, user_id: int) -> str:
        return Keys.k(CART_HASH_KEY, uid=user_id)

    # --------------------------------------------------------
    # 📌 REGISTRAR VISUALIZAÇÃO
    # --------------------------------------------------------
    async def track_view(
        self,
        *,
        user_id: int,
        chat_id: int,
        product_id: int,
        product_name: str,
        price: float,
        force: bool = False,
    ) -> bool:
        """
        Registra que o usuário abriu os detalhes do produto.
        Se já existir um rastreio ativo para este usuário, ATUALIZA
        (não acumula).

        Retorna True se registrou/atualizou.
        """
        if not self._enabled and not force:
            return False

        now = int(time.time())
        due_at = now + self.delay_seconds

        item = CartItem(
            user_id=user_id,
            product_id=product_id,
            product_name=product_name,
            price=float(price),
            opened_at=now,
            due_at=due_at,
            chat_id=chat_id,
        )

        try:
            r = await get_redis()

            # Salva hash do item
            await r.hset(
                self._hash_key(user_id),
                mapping={k: str(v) for k, v in item.to_dict().items()},
            )
            await r.expire(self._hash_key(user_id), CART_STATE_TTL)

            # Adiciona no Sorted Set com score = due_at
            # Se já existia, atualiza o score
            await r.zadd(CART_QUEUE_KEY, {str(user_id): due_at})

            await Counters.incr("cart:tracked")
            logger.bind(user_id=user_id).debug(
                f"Carrinho rastreado: user={user_id} product={product_id} due={due_at}"
            )
            return True
        except Exception as exc:
            logger.exception(f"track_view falhou: {exc}")
            return False

    # --------------------------------------------------------
    # ❌ CANCELAR (usuário comprou ou fechou)
    # --------------------------------------------------------
    async def mark_converted(self, user_id: int, product_id: Optional[int] = None) -> bool:
        """
        Remove o rastreio do usuário (comprou ou interagiu).
        Se `product_id` for passado, só remove se bater.
        """
        try:
            r = await get_redis()
            key = self._hash_key(user_id)

            if product_id is not None:
                existing = await r.hgetall(key)
                if existing and int(existing.get("product_id", 0)) != int(product_id):
                    return False

            await r.delete(key)
            await r.zrem(CART_QUEUE_KEY, str(user_id))
            await Counters.incr("cart:converted")
            logger.bind(user_id=user_id).debug(
                f"Carrinho cancelado (convertido): user={user_id}"
            )
            return True
        except Exception as exc:
            logger.debug(f"mark_converted falhou: {exc}")
            return False

    # --------------------------------------------------------
    # 📥 COLETAR VENCIDOS
    # --------------------------------------------------------
    async def collect_due(self, limit: int = 50) -> List[CartItem]:
        """
        Retorna os carrinhos cujo prazo já venceu.
        NÃO remove da fila (o worker chama mark_notified depois).
        """
        if not self._enabled:
            return []

        now = int(time.time())
        due_items: List[CartItem] = []

        try:
            r = await get_redis()
            raw_uids = await r.zrangebyscore(
                CART_QUEUE_KEY, 0, now, start=0, num=limit
            )
            if not raw_uids:
                return []

            for uid_str in raw_uids:
                try:
                    uid = int(uid_str)
                except Exception:
                    continue

                data = await r.hgetall(self._hash_key(uid))
                if not data:
                    # órfão → remove da fila
                    await r.zrem(CART_QUEUE_KEY, uid_str)
                    continue

                try:
                    item = CartItem(
                        user_id=int(data.get("user_id", uid)),
                        product_id=int(data.get("product_id", 0)),
                        product_name=str(data.get("product_name", "")),
                        price=float(data.get("price", 0.0)),
                        opened_at=int(data.get("opened_at", 0)),
                        due_at=int(data.get("due_at", 0)),
                        chat_id=int(data.get("chat_id", 0)),
                    )
                except Exception:
                    await r.zrem(CART_QUEUE_KEY, uid_str)
                    continue

                # item já notificado?
                notified = data.get("notified")
                if notified == "1":
                    await r.zrem(CART_QUEUE_KEY, uid_str)
                    continue

                due_items.append(item)

            return due_items
        except Exception as exc:
            logger.exception(f"collect_due falhou: {exc}")
            return []

    # --------------------------------------------------------
    # ✅ MARCAR COMO NOTIFICADO
    # --------------------------------------------------------
    async def mark_notified(self, item: CartItem) -> bool:
        """Marca o item como notificado e o remove da fila."""
        try:
            r = await get_redis()
            key = self._hash_key(item.user_id)
            await r.hset(key, "notified", "1")
            await r.expire(key, 60 * 30)  # mantém por 30min para consulta
            await r.zrem(CART_QUEUE_KEY, str(item.user_id))
            await Counters.incr("cart:notified")
            return True
        except Exception as exc:
            logger.debug(f"mark_notified falhou: {exc}")
            return False

    # --------------------------------------------------------
    # 🔎 CONSULTA
    # --------------------------------------------------------
    async def get_item(self, user_id: int) -> Optional[CartItem]:
        """Retorna o item de carrinho atual do usuário (se houver)."""
        try:
            r = await get_redis()
            data = await r.hgetall(self._hash_key(user_id))
            if not data:
                return None
            return CartItem.from_dict(data)
        except Exception:
            return None

    async def is_tracking(self, user_id: int) -> bool:
        """Diz se o usuário tem rastreio ativo."""
        try:
            r = await get_redis()
            return bool(await r.exists(self._hash_key(user_id)))
        except Exception:
            return False

    async def pending_count(self) -> int:
        """Total de carrinhos pendentes na fila."""
        try:
            r = await get_redis()
            return int(await r.zcard(CART_QUEUE_KEY))
        except Exception:
            return 0

    # --------------------------------------------------------
    # 🧹 LIMPEZA
    # --------------------------------------------------------
    async def clear_all(self) -> int:
        """
        Remove TODOS os rastreios de carrinho.
        Uso: painel admin (módulo Automações) ou troubleshooting.
        """
        try:
            r = await get_redis()
            count = 0
            raw_uids = await r.zrange(CART_QUEUE_KEY, 0, -1)
            for uid in raw_uids:
                try:
                    await r.delete(self._hash_key(int(uid)))
                except Exception:
                    pass
                count += 1
            await r.delete(CART_QUEUE_KEY)
            return count
        except Exception:
            return 0

    # --------------------------------------------------------
    # 💚 HEALTHCHECK
    # --------------------------------------------------------
    async def healthcheck(self) -> bool:
        try:
            r = await get_redis()
            await r.ping()
            return True
        except Exception:
            return False


# ============================================================
# 📌 SINGLETON
# ============================================================
cart_tracker = CartTracker()


# ============================================================
# 🧩 HELPERS DE MÓDULO
# ============================================================
async def track_product_view(
    *,
    user_id: int,
    chat_id: int,
    product_id: int,
    product_name: str,
    price: float,
) -> None:
    """Atalho: registra visualização de produto."""
    await cart_tracker.track_view(
        user_id=user_id,
        chat_id=chat_id,
        product_id=product_id,
        product_name=product_name,
        price=price,
    )


async def cancel_cart(user_id: int, product_id: Optional[int] = None) -> None:
    """Atalho: cancela rastreio do usuário."""
    await cart_tracker.mark_converted(user_id, product_id)


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "CartTracker",
    "CartItem",
    "cart_tracker",
    "track_product_view",
    "cancel_cart",
    "MESSAGE_KEY_CARRINHO",
    "MESSAGE_KEY_PROMO",
]
