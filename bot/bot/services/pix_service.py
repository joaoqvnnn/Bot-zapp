"""
Larizinha Store — PixService
Serviço real de PIX usando Mercado Pago (API v1).

Responsabilidades:
- Criar cobrança PIX (QR base64 + copia-e-cola + txid interno)
- Consultar status na API do MP
- Aprovar pagamento (chamado pelo webhook)
- Expirar/cancelar cobranças vencidas (chamado pela automação)
- Reservar estoque via LockManager durante a geração
- Registrar tudo em `pix_charges`, `transactions`, `users`
- Atualizar saldo, bônus, comissões de afiliado
- Nunca acumular mensagem — apenas fornece dados (o handler edita)
"""
from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import select

from bot.config import settings
from bot.database import (
    PixCharge,
    PixStatus,
    Transaction,
    TransactionType,
    User,
    session_scope,
)
from bot.logger import log_payment, logger
from bot.redis_client import (
    Counters,
    FlowState,
    Keys,
    LockManager,
    get_redis,
)


# ============================================================
# 🔑 CONSTANTES
# ============================================================
MP_API_BASE = "https://api.mercadopago.com"
MP_PAYMENT_ENDPOINT = f"{MP_API_BASE}/v1/payments"

HTTP_TIMEOUT = 20.0
HTTP_RETRIES = 2
HTTP_BACKOFF = 1.5

FLOW_NAME_PIX = "pix"
STATUS_PAID = {"approved", "authorized"}
STATUS_PENDING = {"pending", "in_process", "in_mediation"}
STATUS_FAILED = {"rejected", "cancelled", "refunded", "charged_back"}


# ============================================================
# 🧱 DATACLASS DE RESULTADO
# ============================================================
@dataclass
class PixResult:
    """Retorno padronizado para os handlers."""

    ok: bool
    charge_id: Optional[int] = None
    txid: Optional[str] = None
    amount: Optional[float] = None
    bonus: Optional[float] = None
    qr_code: Optional[str] = None
    qr_code_base64: Optional[str] = None
    copy_paste: Optional[str] = None
    expires_at: Optional[datetime] = None
    mp_payment_id: Optional[str] = None
    error: Optional[str] = None


# ============================================================
# 🧠 SERVIÇO
# ============================================================
class PixService:
    """
    Serviço de PIX.

    Uso:
        result = await pix_service.create_charge(user_id=..., amount=50.0)
        result = await pix_service.check_status(txid)
        await pix_service.mark_paid(txid, mp_payment_id="...")
        await pix_service.expire_charge(txid)
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    # --------------------------------------------------------
    # 🔌 HTTP CLIENT
    # --------------------------------------------------------
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                headers={
                    "Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Idempotency-Key": uuid.uuid4().hex,
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # --------------------------------------------------------
    # 🧾 GERAR TXID INTERNO
    # --------------------------------------------------------
    @staticmethod
    def _generate_txid() -> str:
        """Gera txid único, alfanumérico, até 35 caracteres (limite do MP)."""
        return f"LZ{uuid.uuid4().hex[:28]}".upper()

    # --------------------------------------------------------
    # 💰 CRIAR COBRANÇA PIX
    # --------------------------------------------------------
    async def create_charge(
        self,
        *,
        user_id: int,
        amount: float,
        description: Optional[str] = None,
        bonus: float = 0.0,
        order_id: Optional[int] = None,
        reserve_product_id: Optional[int] = None,
        reserve_quantity: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PixResult:
        """
        Cria uma cobrança PIX no Mercado Pago.

        Passos:
          1) Valida valor (min/max)
          2) Carrega o usuário (para dados do payer)
          3) Reserva estoque (se `reserve_product_id`) via LockManager
          4) Chama a API do MP
          5) Persiste em `pix_charges`
          6) Guarda FlowState
          7) Retorna PixResult
        """
        # ----------------------------------------------------
        # 1) Validação
        # ----------------------------------------------------
        if amount < settings.MIN_DEPOSIT:
            return PixResult(ok=False, error=f"Valor mínimo: R$ {settings.MIN_DEPOSIT:.2f}")
        if amount > settings.MAX_DEPOSIT:
            return PixResult(ok=False, error=f"Valor máximo: R$ {settings.MAX_DEPOSIT:.2f}")

        # ----------------------------------------------------
        # 2) Carrega usuário
        # ----------------------------------------------------
        async with session_scope() as session:
            result = await session.execute(
                select(User).where(User.id == user_id)
            )
            user: Optional[User] = result.scalar_one_or_none()
            if user is None:
                return PixResult(ok=False, error="Usuário não encontrado.")

            payer_email = user.email or f"user{user.telegram_id}@larizinha.store"
            first_name = (user.first_name or "Cliente").strip() or "Cliente"
            last_name = (user.last_name or "Larizinha").strip() or "Larizinha"

        # ----------------------------------------------------
        # 3) Reserva de estoque (trava)
        # ----------------------------------------------------
        lock_token: Optional[str] = None
        if reserve_product_id and settings.ENABLE_CONCURRENCY_LOCK:
            lock_token = await LockManager.reserve_stock(
                product_id=reserve_product_id,
                user_id=user_id,
                quantity=reserve_quantity,
                ttl=settings.PRODUCT_LOCK_TTL,
            )
            if lock_token is None:
                return PixResult(
                    ok=False,
                    error="Este produto está temporariamente reservado. Tente novamente em instantes.",
                )

        # ----------------------------------------------------
        # 4) Chamada à API do MP
        # ----------------------------------------------------
        txid = self._generate_txid()
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=settings.MP_PIX_EXPIRATION_MINUTES
        )

        description = description or f"Recarga {settings.APP_NAME}"

        payload: Dict[str, Any] = {
            "transaction_amount": round(float(amount), 2),
            "description": description,
            "payment_method_id": "pix",
            "external_reference": txid,
            "notification_url": settings.MP_WEBHOOK_URL or None,
            "statement_descriptor": settings.MP_PIX_STATEMENT_DESCRIPTOR,
            "date_of_expiration": expires_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "payer": {
                "email": payer_email,
                "first_name": first_name[:60],
                "last_name": last_name[:60],
            },
            "metadata": {
                "user_id": user_id,
                "txid": txid,
                "bonus": float(bonus or 0.0),
                "order_id": order_id,
                **(metadata or {}),
            },
        }

        try:
            mp_response = await self._mp_request("POST", MP_PAYMENT_ENDPOINT, payload)
        except Exception as exc:
            # libera trava se falhou
            if reserve_product_id and lock_token:
                await LockManager.release_lock(reserve_product_id, user_id)
            logger.exception(f"Falha ao criar PIX no MP: {exc}")
            return PixResult(ok=False, error="Erro ao comunicar com o Mercado Pago.")

        if not mp_response or "id" not in mp_response:
            if reserve_product_id and lock_token:
                await LockManager.release_lock(reserve_product_id, user_id)
            msg = (mp_response or {}).get("message", "Erro desconhecido.")
            logger.warning(f"MP retornou erro: {msg}")
            return PixResult(ok=False, error=msg)

        # ----------------------------------------------------
        # 4.1) Extrai dados do PIX
        # ----------------------------------------------------
        mp_payment_id = str(mp_response.get("id"))
        poi = (mp_response.get("point_of_interaction") or {})
        tx_data = (poi.get("transaction_data") or {})

        qr_code = tx_data.get("qr_code")
        qr_code_base64 = tx_data.get("qr_code_base64")
        ticket_url = tx_data.get("ticket_url")

        if not qr_code:
            if reserve_product_id and lock_token:
                await LockManager.release_lock(reserve_product_id, user_id)
            return PixResult(
                ok=False,
                error="Mercado Pago não retornou o QR Code. Tente novamente.",
            )

        # ----------------------------------------------------
        # 5) Persiste no banco
        # ----------------------------------------------------
        try:
            async with session_scope() as session:
                charge = PixCharge(
                    txid=txid,
                    user_id=user_id,
                    amount=Decimal(str(round(float(amount), 2))),
                    bonus=Decimal(str(round(float(bonus or 0.0), 2))),
                    qr_code=qr_code,
                    qr_code_base64=qr_code_base64,
                    copy_paste=qr_code,
                    mp_payment_id=mp_payment_id,
                    status=PixStatus.PENDING,
                    expires_at=expires_at,
                    order_id=order_id,
                    metadata_={
                        "ticket_url": ticket_url,
                        "reserve_product_id": reserve_product_id,
                        "reserve_quantity": reserve_quantity,
                        "lock_token": lock_token,
                        **(metadata or {}),
                    },
                )
                session.add(charge)
                await session.flush()
                charge_id = int(charge.id)
        except Exception as exc:
            logger.exception(f"Falha ao persistir PixCharge: {exc}")
            if reserve_product_id and lock_token:
                await LockManager.release_lock(reserve_product_id, user_id)
            return PixResult(ok=False, error="Erro ao salvar cobrança.")

        # ----------------------------------------------------
        # 6) FlowState (handlers usam para editar sem acumular)
        # ----------------------------------------------------
        await FlowState.set(
            user_id=user_id,
            name=FLOW_NAME_PIX,
            data={
                "charge_id": charge_id,
                "txid": txid,
                "amount": float(amount),
                "bonus": float(bonus or 0.0),
                "mp_payment_id": mp_payment_id,
                "expires_at": expires_at.isoformat(),
                "reserve_product_id": reserve_product_id,
            },
            ttl=settings.MP_PIX_EXPIRATION_MINUTES * 60 + 300,
        )

        await Counters.incr("pix:created")

        log_payment(
            "pix.created",
            user_id=user_id,
            amount=float(amount),
            status="pending",
            details={
                "txid": txid,
                "mp_payment_id": mp_payment_id,
                "bonus": float(bonus or 0.0),
                "order_id": order_id,
            },
        )

        return PixResult(
            ok=True,
            charge_id=charge_id,
            txid=txid,
            amount=float(amount),
            bonus=float(bonus or 0.0),
            qr_code=qr_code,
            qr_code_base64=qr_code_base64,
            copy_paste=qr_code,
            expires_at=expires_at,
            mp_payment_id=mp_payment_id,
        )

    # --------------------------------------------------------
    # 🔎 CONSULTA STATUS NA API
    # --------------------------------------------------------
    async def fetch_status(self, mp_payment_id: str) -> Optional[Dict[str, Any]]:
        """Consulta o pagamento na API do MP."""
        url = f"{MP_PAYMENT_ENDPOINT}/{mp_payment_id}"
        try:
            return await self._mp_request("GET", url)
        except Exception as exc:
            logger.warning(f"fetch_status falhou ({mp_payment_id}): {exc}")
            return None

    # --------------------------------------------------------
    # ✅ MARCAR COMO PAGO
    # --------------------------------------------------------
    async def mark_paid(
        self,
        txid: str,
        *,
        mp_payment_id: Optional[str] = None,
        paid_amount: Optional[float] = None,
    ) -> Optional[PixCharge]:
        """
        Marca uma cobrança como paga (chamado pelo webhook).
        - Aumenta saldo + bônus
        - Cria Transaction
        - Gera comissão de afiliado (se aplicável)
        - Libera trava de estoque (vira venda)
        - Limpa FlowState
        """
        async with session_scope() as session:
            # Carrega a cobrança
            result = await session.execute(
                select(PixCharge).where(PixCharge.txid == txid)
            )
            charge: Optional[PixCharge] = result.scalar_one_or_none()
            if charge is None:
                logger.warning(f"mark_paid: cobrança não encontrada ({txid})")
                return None

            # Idempotência
            if charge.status == PixStatus.PAID:
                return charge

            # Atualiza a charge
            now = datetime.now(timezone.utc)
            charge.status = PixStatus.PAID
            charge.paid_at = now
            if mp_payment_id:
                charge.mp_payment_id = mp_payment_id
            session.add(charge)

            # Carrega user
            user_res = await session.execute(
                select(User).where(User.id == charge.user_id)
            )
            user: Optional[User] = user_res.scalar_one_or_none()
            if user is None:
                logger.warning(f"mark_paid: usuário não encontrado (id={charge.user_id})")
                return charge

            # Credita saldo
            credit = float(charge.amount) + float(charge.bonus or 0)
            user.balance = Decimal(str(round(float(user.balance) + credit, 2)))
            user.total_deposited = Decimal(
                str(round(float(user.total_deposited) + float(charge.amount), 2))
            )
            session.add(user)

            # Transaction
            tx = Transaction(
                user_id=user.id,
                type=TransactionType.DEPOSIT,
                amount=Decimal(str(round(credit, 2))),
                balance_after=user.balance,
                description=(
                    f"Depósito PIX aprovado ({txid})"
                    + (f" + bônus R$ {float(charge.bonus):.2f}" if charge.bonus else "")
                ),
                reference=txid,
                metadata_={"mp_payment_id": charge.mp_payment_id},
            )
            session.add(tx)

        # ----------------------------------------------------
        # Efeitos pós-commit (fora da sessão)
        # ----------------------------------------------------
        await Counters.incr("pix:paid")

        # Libera trava de estoque → vira venda (a baixa no DB é feita no handler de compra)
        try:
            meta = charge.metadata_ or {}
            rp = meta.get("reserve_product_id") if isinstance(meta, dict) else None
            if rp:
                await LockManager.commit_lock(int(rp), int(charge.user_id))
        except Exception as exc:
            logger.debug(f"commit_lock pós-PIX falhou: {exc}")

        # Comissões de afiliado
        try:
            await self._apply_affiliate_commission(
                user_id=int(charge.user_id),
                amount=float(charge.amount),
                reference=txid,
            )
        except Exception as exc:
            logger.debug(f"Comissão afiliado falhou: {exc}")

        # Limpa FlowState (o handler de sucesso usa para editar)
        try:
            await FlowState.update(
                user_id=int(charge.user_id),
                name=FLOW_NAME_PIX,
                patch={"status": "paid", "paid_at": now.isoformat()},
                ttl=600,
            )
        except Exception:
            pass

        log_payment(
            "pix.paid",
            user_id=int(charge.user_id),
            amount=float(charge.amount),
            status="paid",
            details={"txid": txid, "bonus": float(charge.bonus or 0)},
        )
        return charge

    # --------------------------------------------------------
    # ❌ EXPIRAR
    # --------------------------------------------------------
    async def expire_charge(self, txid: str) -> bool:
        """Marca cobrança como expirada e libera a trava."""
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(PixCharge).where(PixCharge.txid == txid)
                )
                charge: Optional[PixCharge] = result.scalar_one_or_none()
                if charge is None:
                    return False
                if charge.status != PixStatus.PENDING:
                    return False
                charge.status = PixStatus.EXPIRED
                session.add(charge)
                meta = charge.metadata_ or {}
                reserve_product_id = meta.get("reserve_product_id") if isinstance(meta, dict) else None
                user_id = int(charge.user_id)

            if reserve_product_id:
                await LockManager.release_lock(int(reserve_product_id), user_id)

            await Counters.incr("pix:expired")
            log_payment("pix.expired", user_id=user_id, status="expired", details={"txid": txid})
            return True
        except Exception as exc:
            logger.exception(f"expire_charge falhou ({txid}): {exc}")
            return False

    # --------------------------------------------------------
    # 🚫 CANCELAR (manual)
    # --------------------------------------------------------
    async def cancel_charge(self, txid: str) -> bool:
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(PixCharge).where(PixCharge.txid == txid)
                )
                charge: Optional[PixCharge] = result.scalar_one_or_none()
                if charge is None or charge.status != PixStatus.PENDING:
                    return False
                charge.status = PixStatus.CANCELED
                session.add(charge)
                meta = charge.metadata_ or {}
                rp = meta.get("reserve_product_id") if isinstance(meta, dict) else None
                uid = int(charge.user_id)

            if rp:
                await LockManager.release_lock(int(rp), uid)
            log_payment("pix.canceled", user_id=uid, status="canceled", details={"txid": txid})
            return True
        except Exception as exc:
            logger.exception(f"cancel_charge falhou ({txid}): {exc}")
            return False

    # --------------------------------------------------------
    # 📥 BUSCAR CHARGE
    # --------------------------------------------------------
    async def get_by_txid(self, txid: str) -> Optional[PixCharge]:
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(PixCharge).where(PixCharge.txid == txid)
                )
                return result.scalar_one_or_none()
        except Exception:
            return None

    async def get_by_mp_id(self, mp_payment_id: str) -> Optional[PixCharge]:
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(PixCharge).where(PixCharge.mp_payment_id == mp_payment_id)
                )
                return result.scalar_one_or_none()
        except Exception:
            return None

    async def list_expired(self, limit: int = 100) -> list[PixCharge]:
        """Cobranças PENDING já vencidas (usado pela automação)."""
        now = datetime.now(timezone.utc)
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(PixCharge)
                    .where(PixCharge.status == PixStatus.PENDING)
                    .where(PixCharge.expires_at <= now)
                    .limit(limit)
                )
                return list(result.scalars().all())
        except Exception as exc:
            logger.exception(f"list_expired falhou: {exc}")
            return []

    async def list_pending(self, limit: int = 100) -> list[PixCharge]:
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(PixCharge)
                    .where(PixCharge.status == PixStatus.PENDING)
                    .limit(limit)
                )
                return list(result.scalars().all())
        except Exception:
            return []

    # --------------------------------------------------------
    # 🎁 COMISSÃO DE AFILIADO
    # --------------------------------------------------------
    async def _apply_affiliate_commission(
        self,
        *,
        user_id: int,
        amount: float,
        reference: str,
    ) -> None:
        """
        Gera comissão para o padrinho, se o usuário foi indicado.
        Usa `AFFILIATE_COMMISSION_DEPOSIT_PERCENT`.
        """
        if not settings.AFFILIATE_ENABLED:
            return
        percent = float(settings.AFFILIATE_COMMISSION_DEPOSIT_PERCENT or 0)
        if percent <= 0:
            return

        from bot.database import Commission, CommissionStatus, CommissionType

        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == user_id))
            user: Optional[User] = res.scalar_one_or_none()
            if user is None or not user.referred_by_id:
                return

            earner_id = int(user.referred_by_id)
            commission_value = round(amount * (percent / 100.0), 2)
            if commission_value <= 0:
                return

            comm = Commission(
                earner_id=earner_id,
                source_user_id=user_id,
                amount=Decimal(str(commission_value)),
                percent=Decimal(str(percent)),
                type=CommissionType.DEPOSIT,
                status=CommissionStatus.PENDING,
                reference=reference,
            )
            session.add(comm)

            # Atualiza saldo de comissões do padrinho
            earner_res = await session.execute(select(User).where(User.id == earner_id))
            earner: Optional[User] = earner_res.scalar_one_or_none()
            if earner:
                earner.affiliate_balance = Decimal(
                    str(round(float(earner.affiliate_balance) + commission_value, 2))
                )
                session.add(earner)

        log_payment(
            "commission.generated",
            user_id=earner_id,
            amount=commission_value,
            status="pending",
            details={"reference": reference, "source_user": user_id, "percent": percent},
        )

    # --------------------------------------------------------
    # 🔌 HTTP WRAPPER COM RETRY
    # --------------------------------------------------------
    async def _mp_request(
        self,
        method: str,
        url: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Requisição ao Mercado Pago com retry exponencial."""
        client = await self._get_client()

        last_exc: Optional[Exception] = None
        for attempt in range(HTTP_RETRIES + 1):
            try:
                if method == "POST":
                    resp = await client.post(url, json=payload)
                else:
                    resp = await client.get(url)

                # 2xx
                if 200 <= resp.status_code < 300:
                    try:
                        return resp.json()
                    except Exception:
                        return None

                # 4xx (não recuperável, exceto 429)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    logger.warning(
                        f"MP {resp.status_code} (tentativa {attempt + 1}): {resp.text[:200]}"
                    )
                    last_exc = RuntimeError(f"MP status {resp.status_code}")
                else:
                    logger.warning(f"MP erro {resp.status_code}: {resp.text[:200]}")
                    try:
                        return resp.json()
                    except Exception:
                        return None

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                logger.warning(f"MP timeout/network (tentativa {attempt + 1}): {exc}")

            if attempt < HTTP_RETRIES:
                await asyncio.sleep(HTTP_BACKOFF * (attempt + 1))

        if last_exc:
            raise last_exc
        return None

    # --------------------------------------------------------
    # 💚 HEALTHCHECK
    # --------------------------------------------------------
    async def healthcheck(self) -> bool:
        """Verifica se o token do MP está válido."""
        try:
            client = await self._get_client()
            resp = await client.get(f"{MP_API_BASE}/users/me")
            return resp.status_code == 200
        except Exception:
            return False

    # --------------------------------------------------------
    # 🧾 RECUPERAR FLUXO ATIVO
    # --------------------------------------------------------
    async def get_active_flow(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Retorna o FlowState de PIX (se ainda ativo)."""
        return await FlowState.get(user_id=user_id, name=FLOW_NAME_PIX)


# ============================================================
# 📌 SINGLETON
# ============================================================
pix_service = PixService()


# ============================================================
# 🧩 HELPERS DE MÓDULO
# ============================================================
def decode_qr_base64(qr_code_base64: Optional[str]) -> Optional[bytes]:
    """Decodifica o QR em base64 para bytes (para enviar como foto)."""
    if not qr_code_base64:
        return None
    try:
        # o MP às vezes retorna sem o header; garantimos
        raw = qr_code_base64
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        return base64.b64decode(raw)
    except Exception:
        return None


async def cleanup_pix_state(user_id: int) -> None:
    """Limpa o FlowState de PIX do usuário."""
    try:
        await FlowState.clear(user_id=user_id, name=FLOW_NAME_PIX)
    except Exception:
        pass


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "PixService",
    "PixResult",
    "pix_service",
    "decode_qr_base64",
    "cleanup_pix_state",
    "FLOW_NAME_PIX",
]
