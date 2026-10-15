"""
Larizinha Store — SecurityMonitor (Antifraude real)
Detecta, marca e previne:
- Múltiplas contas por IP (via eventos login/webapp)
- Contas duplicadas por dados (PIX, WhatsApp, e-mail)
- Transações financeiras suspeitas (valor alto, velocidade)
- Saques repetidos em janela curta
- Uso abusivo (tentativas inválidas, padrão de spam)
- Bloqueio temporário automático

Persistência:
- `security_events` (tabela) — registro bruto
- `alerts` (tabela) — alertas para painel admin
- Redis — janelas de contagem rápida + bloqueios temporários
- `logs` — auditoria via log_security()

Regras invioláveis:
- NUNCA acumula mensagens (quem notifica admins é a automação).
- Respeita ANTIFRAUD_ENABLED, TEMP_BLOCK_MINUTES, THROTTLE_RATE.
- Sem depender de bibliotecas externas.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from bot.config import settings
from bot.database import (
    Alert,
    SecurityEvent,
    Transaction,
    TransactionType,
    User,
    Withdraw,
    WithdrawStatus,
    session_scope,
)
from bot.logger import log_security, logger
from bot.redis_client import Counters, Keys, cache_get, cache_set, get_redis


# ============================================================
# 🔑 CONSTANTES
# ============================================================
SEC_NS = "security"

# Janelas em segundos
WINDOW_SWITCH = 60          # 60s para contar tentativas rápidas
WINDOW_WITHDRAW = 3600      # 1h para contar saques
WINDOW_DEPOSIT = 3600       # 1h para contar depósitos
WINDOW_LOGIN = 3600         # 1h para contar logins por IP

# Limites (podem ser sobrepostos por settings)
MAX_TX_PER_HOUR = 20
MAX_WITHDRAW_PER_HOUR = 3
MAX_WITHDRAW_PER_DAY = 5
MAX_FAILED_ACTIONS_PER_HOUR = 15

# Motivos canônicos
REASON_MULTI_ACCOUNT_IP = "multi_account_ip"
REASON_DUPLICATE_PIX = "duplicate_pix_key"
REASON_DUPLICATE_WHATSAPP = "duplicate_whatsapp"
REASON_DUPLICATE_EMAIL = "duplicate_email"
REASON_SUSPICIOUS_AMOUNT = "suspicious_amount"
REASON_VELOCITY_TX = "velocity_tx"
REASON_VELOCITY_WITHDRAW = "velocity_withdraw"
REASON_FAILED_ACTIONS = "failed_actions"
REASON_ABUSE = "abuse"


# ============================================================
# 🧱 RESULTADO
# ============================================================
@dataclass
class SecurityVerdict:
    """Resultado de uma checagem de segurança."""

    ok: bool
    reason: Optional[str] = None
    severity: str = "info"
    details: Dict[str, Any] = None  # type: ignore[assignment]
    blocked_until: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = {}


# ============================================================
# 🧠 SERVIÇO
# ============================================================
class SecurityMonitor:
    """
    Serviço de antifraude.

    Uso:
        v = await security_monitor.check_withdraw(user_id, amount)
        if not v.ok:
            # bloqueia
        await security_monitor.register_event(...)
    """

    def __init__(self) -> None:
        self._enabled = bool(settings.ANTIFRAUD_ENABLED)
        self._block_minutes = max(1, int(settings.TEMP_BLOCK_MINUTES or 15))
        self._max_accounts_per_ip = max(1, int(settings.ANTIFRAUD_MAX_ACCOUNTS_PER_IP or 3))
        self._suspicious_amount = float(settings.ANTIFRAUD_SUSPICIOUS_AMOUNT or 1000.0)

    # --------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    # --------------------------------------------------------
    # 💾 REGISTRO DE EVENTO
    # --------------------------------------------------------
    async def register_event(
        self,
        *,
        type_: str,
        severity: str = "info",
        user_id: Optional[int] = None,
        ip: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persiste um SecurityEvent + log."""
        try:
            async with session_scope() as session:
                ev = SecurityEvent(
                    type=type_,
                    severity=severity,
                    user_id=user_id,
                    ip=ip,
                    details=details or {},
                )
                session.add(ev)
        except Exception as exc:
            logger.debug(f"register_event falhou: {exc}")

        log_security(
            f"event.{type_}",
            user_id=user_id,
            severity=severity,
            details={"ip": ip, **(details or {})},
        )

    # --------------------------------------------------------
    # 🚨 ALERTA PARA PAINEL ADMIN
    # --------------------------------------------------------
    async def raise_alert(
        self,
        *,
        category: str,
        title: str,
        message: str,
        severity: str = "warning",
    ) -> None:
        try:
            async with session_scope() as session:
                session.add(
                    Alert(
                        category=category,
                        severity=severity,
                        title=title[:200],
                        message=message,
                        resolved=False,
                    )
                )
        except Exception as exc:
            logger.debug(f"raise_alert falhou: {exc}")

    # --------------------------------------------------------
    # 🚫 BLOQUEIO TEMPORÁRIO (Redis)
    # --------------------------------------------------------
    async def temp_block(
        self,
        user_id: int,
        *,
        seconds: Optional[int] = None,
        reason: str = REASON_ABUSE,
    ) -> datetime:
        """Aplica bloqueio temporário e retorna quando expira."""
        ttl = seconds if seconds is not None else self._block_minutes * 60
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        try:
            r = await get_redis()
            await r.set(
                Keys.k(f"{SEC_NS}:tempblock:{{uid}}", uid=user_id),
                reason,
                ex=ttl,
            )
        except Exception as exc:
            logger.debug(f"temp_block falhou: {exc}")
        return expires

    async def is_temp_blocked(self, user_id: int) -> Optional[datetime]:
        """Retorna quando expira (ou None se não bloqueado)."""
        try:
            r = await get_redis()
            key = Keys.k(f"{SEC_NS}:tempblock:{{uid}}", uid=user_id)
            ttl = await r.ttl(key)
            if ttl and ttl > 0:
                return datetime.now(timezone.utc) + timedelta(seconds=ttl)
        except Exception:
            pass
        return None

    async def unblock(self, user_id: int) -> None:
        try:
            r = await get_redis()
            await r.delete(Keys.k(f"{SEC_NS}:tempblock:{{uid}}", uid=user_id))
        except Exception:
            pass

    # --------------------------------------------------------
    # 📈 JANELAS DE CONTAGEM (Redis)
    # --------------------------------------------------------
    async def _bump(self, user_id: int, kind: str, window: int) -> int:
        """
        Incrementa um contador com TTL (janela deslizante simples).
        Retorna o valor atual.
        """
        try:
            r = await get_redis()
            key = Keys.k(f"{SEC_NS}:win:{{uid}}:{kind}", uid=user_id)
            pipe = r.pipeline()
            pipe.incr(key, 1)
            pipe.expire(key, window, nx=True)
            results = await pipe.execute()
            return int(results[0])
        except Exception:
            return 0

    async def _get_count(self, user_id: int, kind: str) -> int:
        try:
            r = await get_redis()
            key = Keys.k(f"{SEC_NS}:win:{{uid}}:{kind}", uid=user_id)
            val = await r.get(key)
            return int(val) if val else 0
        except Exception:
            return 0

    # --------------------------------------------------------
    # 🔎 CONSULTAS DE BANCO (duplicidade)
    # --------------------------------------------------------
    async def _count_users_by_pix(self, pix_key: str, exclude_user_id: int) -> int:
        if not pix_key:
            return 0
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(func.count(User.id)).where(
                        User.pix_key == pix_key,
                        User.id != exclude_user_id,
                    )
                )
                return int(res.scalar() or 0)
        except Exception:
            return 0

    async def _count_users_by_whatsapp(self, whatsapp: str, exclude_user_id: int) -> int:
        if not whatsapp:
            return 0
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(func.count(User.id)).where(
                        User.whatsapp == whatsapp,
                        User.id != exclude_user_id,
                    )
                )
                return int(res.scalar() or 0)
        except Exception:
            return 0

    async def _count_users_by_email(self, email: str, exclude_user_id: int) -> int:
        if not email:
            return 0
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(func.count(User.id)).where(
                        User.email == email,
                        User.id != exclude_user_id,
                    )
                )
                return int(res.scalar() or 0)
        except Exception:
            return 0

    # --------------------------------------------------------
    # 🧪 CHECKS DE ALTO NÍVEL
    # --------------------------------------------------------
    async def check_login(
        self,
        *,
        user_id: int,
        ip: Optional[str] = None,
    ) -> SecurityVerdict:
        """Verifica múltiplas contas por IP."""
        if not self._enabled:
            return SecurityVerdict(ok=True)

        if ip:
            try:
                r = await get_redis()
                key = Keys.k(f"{SEC_NS}:ip:{{ip}}:users", ip=ip)
                # registra usuário no Set
                await r.sadd(key, str(user_id))
                await r.expire(key, WINDOW_LOGIN)
                distinct = int(await r.scard(key))

                if distinct > self._max_accounts_per_ip:
                    await self.register_event(
                        type_=REASON_MULTI_ACCOUNT_IP,
                        severity="warning",
                        user_id=user_id,
                        ip=ip,
                        details={"distinct_accounts": distinct, "limit": self._max_accounts_per_ip},
                    )
                    await self.raise_alert(
                        category="security",
                        title="Múltiplas contas por IP",
                        message=(
                            f"IP {ip} tem {distinct} contas (limite "
                            f"{self._max_accounts_per_ip})."
                        ),
                        severity="warning",
                    )
                    return SecurityVerdict(
                        ok=False,
                        reason=REASON_MULTI_ACCOUNT_IP,
                        severity="warning",
                        details={"distinct": distinct, "ip": ip},
                    )
            except Exception as exc:
                logger.debug(f"check_login falhou: {exc}")

        return SecurityVerdict(ok=True)

    async def check_duplicate_pix(
        self,
        *,
        user_id: int,
        pix_key: str,
        pix_key_type: Optional[str] = None,
    ) -> SecurityVerdict:
        if not self._enabled or not pix_key:
            return SecurityVerdict(ok=True)

        count = await self._count_users_by_pix(pix_key, user_id)
        if count > 0:
            await self.register_event(
                type_=REASON_DUPLICATE_PIX,
                severity="warning",
                user_id=user_id,
                details={"pix_key": pix_key, "type": pix_key_type, "count": count},
            )
            await self.raise_alert(
                category="security",
                title="Chave PIX duplicada",
                message=(
                    f"A chave PIX '{pix_key}' já está em uso por "
                    f"{count} outra(s) conta(s)."
                ),
                severity="warning",
            )
            return SecurityVerdict(
                ok=False,
                reason=REASON_DUPLICATE_PIX,
                severity="warning",
                details={"count": count},
            )
        return SecurityVerdict(ok=True)

    async def check_duplicate_whatsapp(
        self,
        *,
        user_id: int,
        whatsapp: str,
    ) -> SecurityVerdict:
        if not self._enabled or not whatsapp:
            return SecurityVerdict(ok=True)

        count = await self._count_users_by_whatsapp(whatsapp, user_id)
        if count > 0:
            await self.register_event(
                type_=REASON_DUPLICATE_WHATSAPP,
                severity="warning",
                user_id=user_id,
                details={"whatsapp": whatsapp, "count": count},
            )
            return SecurityVerdict(
                ok=False,
                reason=REASON_DUPLICATE_WHATSAPP,
                severity="warning",
                details={"count": count},
            )
        return SecurityVerdict(ok=True)

    async def check_duplicate_email(
        self,
        *,
        user_id: int,
        email: str,
    ) -> SecurityVerdict:
        if not self._enabled or not email:
            return SecurityVerdict(ok=True)

        count = await self._count_users_by_email(email, user_id)
        if count > 0:
            await self.register_event(
                type_=REASON_DUPLICATE_EMAIL,
                severity="warning",
                user_id=user_id,
                details={"email": email, "count": count},
            )
            return SecurityVerdict(
                ok=False,
                reason=REASON_DUPLICATE_EMAIL,
                severity="warning",
                details={"count": count},
            )
        return SecurityVerdict(ok=True)

    async def check_withdraw(
        self,
        *,
        user_id: int,
        amount: float,
    ) -> SecurityVerdict:
        """
        Verifica:
        - Valor acima do limite suspeito
        - Velocidade de saques na última hora / dia
        """
        if not self._enabled:
            return SecurityVerdict(ok=True)

        # ---------- Valor suspeito ----------
        if amount >= self._suspicious_amount:
            await self.register_event(
                type_=REASON_SUSPICIOUS_AMOUNT,
                severity="warning",
                user_id=user_id,
                details={"amount": amount, "limit": self._suspicious_amount},
            )
            # NÃO bloqueia — apenas registra alerta; aprovado manualmente
            await self.raise_alert(
                category="withdraw",
                title="Saque com valor alto",
                message=(
                    f"Usuário {user_id} solicitou saque de R$ {amount:.2f} "
                    f"(acima de R$ {self._suspicious_amount:.2f})."
                ),
                severity="warning",
            )

        # ---------- Velocidade por hora ----------
        count_hour = await self._bump(user_id, "withdraw_hour", WINDOW_WITHDRAW)
        if count_hour > MAX_WITHDRAW_PER_HOUR:
            await self.register_event(
                type_=REASON_VELOCITY_WITHDRAW,
                severity="error",
                user_id=user_id,
                details={"count_hour": count_hour, "limit": MAX_WITHDRAW_PER_HOUR},
            )
            expires = await self.temp_block(
                user_id,
                reason=REASON_VELOCITY_WITHDRAW,
            )
            return SecurityVerdict(
                ok=False,
                reason=REASON_VELOCITY_WITHDRAW,
                severity="error",
                details={"count_hour": count_hour},
                blocked_until=expires,
            )

        # ---------- Velocidade por dia (via banco) ----------
        try:
            since = datetime.now(timezone.utc) - timedelta(days=1)
            async with session_scope() as session:
                res = await session.execute(
                    select(func.count(Withdraw.id)).where(
                        Withdraw.user_id == user_id,
                        Withdraw.created_at >= since,
                        Withdraw.status.in_(
                            [
                                WithdrawStatus.PENDING,
                                WithdrawStatus.PROCESSING,
                                WithdrawStatus.APPROVED,
                                WithdrawStatus.PAID,
                            ]
                        ),
                    )
                )
                total_day = int(res.scalar() or 0)
            if total_day > MAX_WITHDRAW_PER_DAY:
                await self.register_event(
                    type_=REASON_VELOCITY_WITHDRAW,
                    severity="error",
                    user_id=user_id,
                    details={"total_day": total_day, "limit": MAX_WITHDRAW_PER_DAY},
                )
                return SecurityVerdict(
                    ok=False,
                    reason=REASON_VELOCITY_WITHDRAW,
                    severity="error",
                    details={"total_day": total_day},
                )
        except Exception:
            pass

        return SecurityVerdict(ok=True)

    async def check_transaction(
        self,
        *,
        user_id: int,
        amount: float,
        type_: TransactionType = TransactionType.DEPOSIT,
    ) -> SecurityVerdict:
        """Verifica velocidade de transações e valores suspeitos."""
        if not self._enabled:
            return SecurityVerdict(ok=True)

        if amount >= self._suspicious_amount:
            await self.register_event(
                type_=REASON_SUSPICIOUS_AMOUNT,
                severity="warning",
                user_id=user_id,
                details={"amount": amount, "type": type_.value},
            )

        count_hour = await self._bump(user_id, f"tx_{type_.value}", WINDOW_DEPOSIT)
        if count_hour > MAX_TX_PER_HOUR:
            await self.register_event(
                type_=REASON_VELOCITY_TX,
                severity="error",
                user_id=user_id,
                details={
                    "count_hour": count_hour,
                    "limit": MAX_TX_PER_HOUR,
                    "type": type_.value,
                },
            )
            expires = await self.temp_block(user_id, reason=REASON_VELOCITY_TX)
            return SecurityVerdict(
                ok=False,
                reason=REASON_VELOCITY_TX,
                severity="error",
                details={"count_hour": count_hour},
                blocked_until=expires,
            )
        return SecurityVerdict(ok=True)

    async def register_failed_action(
        self,
        *,
        user_id: int,
        action: str,
    ) -> SecurityVerdict:
        """Chamado quando uma ação sensível falha (ex.: senha errada)."""
        if not self._enabled:
            return SecurityVerdict(ok=True)

        count_hour = await self._bump(user_id, "failed_action", WINDOW_SWITCH)
        if count_hour >= MAX_FAILED_ACTIONS_PER_HOUR:
            await self.register_event(
                type_=REASON_FAILED_ACTIONS,
                severity="warning",
                user_id=user_id,
                details={"action": action, "count": count_hour},
            )
            expires = await self.temp_block(user_id, reason=REASON_FAILED_ACTIONS)
            return SecurityVerdict(
                ok=False,
                reason=REASON_FAILED_ACTIONS,
                severity="warning",
                details={"count": count_hour},
                blocked_until=expires,
            )
        return SecurityVerdict(ok=True)

    # --------------------------------------------------------
    # 📊 ESTATÍSTICAS (painel admin)
    # --------------------------------------------------------
    async def get_stats(
        self,
        *,
        hours: int = 24,
    ) -> Dict[str, Any]:
        """Resumo de segurança para o painel."""
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        try:
            async with session_scope() as session:
                total_res = await session.execute(
                    select(func.count(SecurityEvent.id)).where(
                        SecurityEvent.created_at >= since
                    )
                )
                total = int(total_res.scalar() or 0)

                warn_res = await session.execute(
                    select(func.count(SecurityEvent.id)).where(
                        SecurityEvent.created_at >= since,
                        SecurityEvent.severity == "warning",
                    )
                )
                warnings = int(warn_res.scalar() or 0)

                err_res = await session.execute(
                    select(func.count(SecurityEvent.id)).where(
                        SecurityEvent.created_at >= since,
                        SecurityEvent.severity == "error",
                    )
                )
                errors = int(err_res.scalar() or 0)

                alerts_res = await session.execute(
                    select(func.count(Alert.id)).where(
                        Alert.category == "security",
                        Alert.resolved == False,  # noqa: E712
                    )
                )
                open_alerts = int(alerts_res.scalar() or 0)

            return {
                "total": total,
                "warnings": warnings,
                "errors": errors,
                "open_alerts": open_alerts,
                "window_hours": hours,
            }
        except Exception as exc:
            logger.debug(f"get_stats falhou: {exc}")
            return {
                "total": 0,
                "warnings": 0,
                "errors": 0,
                "open_alerts": 0,
                "window_hours": hours,
            }

    async def list_recent(
        self,
        *,
        limit: int = 50,
        severity: Optional[str] = None,
    ) -> List[SecurityEvent]:
        """Lista eventos recentes."""
        try:
            async with session_scope() as session:
                stmt = select(SecurityEvent).order_by(
                    SecurityEvent.created_at.desc()
                ).limit(limit)
                if severity:
                    stmt = stmt.where(SecurityEvent.severity == severity)
                res = await session.execute(stmt)
                return list(res.scalars().all())
        except Exception:
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
security_monitor = SecurityMonitor()


# ============================================================
# 🧩 HELPERS DE MÓDULO
# ============================================================
async def check_temp_block(user_id: int) -> Optional[datetime]:
    """Atalho: retorna expiração do bloqueio temporário (ou None)."""
    return await security_monitor.is_temp_blocked(user_id)


async def register_security_event(
    type_: str,
    *,
    user_id: Optional[int] = None,
    ip: Optional[str] = None,
    severity: str = "info",
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Atalho: registra um evento de segurança."""
    await security_monitor.register_event(
        type_=type_,
        severity=severity,
        user_id=user_id,
        ip=ip,
        details=details,
    )


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "SecurityMonitor",
    "SecurityVerdict",
    "security_monitor",
    "check_temp_block",
    "register_security_event",
    "REASON_MULTI_ACCOUNT_IP",
    "REASON_DUPLICATE_PIX",
    "REASON_DUPLICATE_WHATSAPP",
    "REASON_DUPLICATE_EMAIL",
    "REASON_SUSPICIOUS_AMOUNT",
    "REASON_VELOCITY_TX",
    "REASON_VELOCITY_WITHDRAW",
    "REASON_FAILED_ACTIONS",
    "REASON_ABUSE",
]
