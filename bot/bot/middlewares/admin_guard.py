"""
Larizinha Store — Middleware Admin Guard + Permission Engine
Responsável por:
- Detectar se o usuário é admin (main admin ou admin no banco).
- Carregar role + permissions[] do admin.
- Checar permissão por módulo/ação (ex.: "products:create").
- Injetar `admin` e `permission_engine` no contexto do handler.
- Logar TODAS as ações administrativas (auditoria).
- Bloquear acesso a módulos sem permissão (mensagem editável, sem acumular).
"""
from __future__ import annotations

import fnmatch
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TgUser
from sqlalchemy import select

from bot.config import settings
from bot.database import Admin, Role, session_scope
from bot.logger import audit, logger


# ============================================================
# 🔐 PERMISSION ENGINE
# ============================================================
class PermissionEngine:
    """
    Verifica permissões baseadas em:
    - Admin Principal (MAIN_ADMIN_ID): acesso TOTAL.
    - Admin com role.is_system=True: acesso TOTAL.
    - Permissões do role + permissões diretas do admin.
    - Suporte a wildcards: "products:*", "admin:*", "*".
    """

    MAIN_PERMISSION = "*"

    def __init__(
        self,
        *,
        is_main_admin: bool = False,
        permissions: Optional[Iterable[str]] = None,
    ) -> None:
        self.is_main_admin = is_main_admin
        perms: Set[str] = set()
        if permissions:
            for p in permissions:
                if p:
                    perms.add(str(p).strip())
        self.permissions = perms

    # --------------------------------------------------------
    def has(self, required: str) -> bool:
        """Verifica se possui a permissão requerida."""
        if self.is_main_admin:
            return True
        if self.MAIN_PERMISSION in self.permissions:
            return True
        if not required:
            return True

        required = required.strip()

        # Match direto
        if required in self.permissions:
            return True

        # Match com wildcard
        for perm in self.permissions:
            if fnmatch.fnmatchcase(required, perm):
                return True

        # Permissão pai (ex.: "products" cobre "products:create")
        parts = required.split(":")
        for i in range(len(parts) - 1, 0, -1):
            parent = ":".join(parts[:i])
            if parent in self.permissions:
                return True
            for perm in self.permissions:
                if fnmatch.fnmatchcase(parent, perm):
                    return True

        return False

    def has_all(self, required_list: Iterable[str]) -> bool:
        return all(self.has(r) for r in required_list)

    def has_any(self, required_list: Iterable[str]) -> bool:
        return any(self.has(r) for r in required_list)

    def __repr__(self) -> str:
        return (
            f"<PermissionEngine main={self.is_main_admin} "
            f"perms={sorted(self.permissions)}>"
        )


# ============================================================
# 💬 MENSAGENS (editáveis — fallback local)
# ============================================================
MSG_NOT_ADMIN = (
    "⛔ <b>Acesso negado.</b>\n\n"
    "Você não tem permissão para acessar o painel administrativo."
)
MSG_NO_PERMISSION = (
    "🚫 <b>Sem permissão.</b>\n\n"
    "Você não possui acesso a este módulo.\n"
    "Permissão necessária: <code>{perm}</code>"
)


# ============================================================
# 🛡️ MIDDLEWARE
# ============================================================
class AdminGuardMiddleware(BaseMiddleware):
    """
    Use este middleware SOMENTE no router do painel admin:
        admin_router.message.middleware(AdminGuardMiddleware())
        admin_router.callback_query.middleware(AdminGuardMiddleware())

    Ele NÃO bloqueia o resto do bot — apenas garante que o handler admin
    só roda se o usuário for admin de verdade E tiver permissão.
    """

    def __init__(
        self,
        *,
        required_permission: Optional[str] = None,
        required_callback_prefix: Optional[str] = None,
    ) -> None:
        self.required_permission = required_permission
        self.required_callback_prefix = required_callback_prefix

    # --------------------------------------------------------
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user: Optional[TgUser] = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        # 1) Carrega admin do banco
        admin_row, engine = await self._load_admin(tg_user)

        if admin_row is None and tg_user.id not in settings.admin_ids_list:
            # Não é admin → bloqueia silenciosamente (sem poluir)
            await self._reject(event, MSG_NOT_ADMIN)
            return None

        # Se está na env mas não no banco, cria engine só com base no main admin
        if engine is None:
            engine = PermissionEngine(
                is_main_admin=(tg_user.id == settings.MAIN_ADMIN_ID),
                permissions=(
                    [PermissionEngine.MAIN_PERMISSION]
                    if tg_user.id in settings.admin_ids_list
                    else []
                ),
            )

        # 2) Determina permissão requerida
        required = self._resolve_required(event)
        if required and not engine.has(required):
            await self._reject(event, MSG_NO_PERMISSION.format(perm=required))
            # Auditoria de tentativa negada
            audit(
                action="admin.permission_denied",
                admin_id=tg_user.id,
                details={"required": required, "event": type(event).__name__},
                category="security",
            )
            return None

        # 3) Injeta no contexto
        data["admin"] = admin_row
        data["admin_id"] = tg_user.id
        data["permission_engine"] = engine
        data["permission"] = engine

        # 4) Auditoria de entrada (apenas se for callback ou comando)
        try:
            if isinstance(event, CallbackQuery):
                audit(
                    action="admin.callback",
                    admin_id=tg_user.id,
                    details={"data": event.data},
                )
            elif isinstance(event, Message):
                audit(
                    action="admin.message",
                    admin_id=tg_user.id,
                    details={
                        "text": (event.text or "")[:100],
                        "chat_id": event.chat.id,
                    },
                )
        except Exception:
            pass

        return await handler(event, data)

    # --------------------------------------------------------
    async def _load_admin(
        self, tg_user: TgUser
    ) -> tuple[Optional[Admin], Optional[PermissionEngine]]:
        """
        Carrega Admin + Role do banco e devolve (admin_row, engine).
        """
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(Admin).where(Admin.telegram_id == tg_user.id)
                )
                admin_row: Optional[Admin] = result.scalar_one_or_none()

                if admin_row is None:
                    return None, None

                # Atualiza último login
                now = datetime.now(timezone.utc)
                if (
                    admin_row.last_login_at is None
                    or (now - admin_row.last_login_at).total_seconds() > 300
                ):
                    admin_row.last_login_at = now
                    session.add(admin_row)

                # Carrega role (se houver)
                role: Optional[Role] = None
                if admin_row.role_id:
                    rres = await session.execute(
                        select(Role).where(Role.id == admin_row.role_id)
                    )
                    role = rres.scalar_one_or_none()

                # Combina permissões
                perms: Set[str] = set()
                if role and role.permissions:
                    perms.update(self._to_list(role.permissions))
                if admin_row.permissions:
                    perms.update(self._to_list(admin_row.permissions))

                is_main = bool(
                    admin_row.is_main_admin
                    or tg_user.id == settings.MAIN_ADMIN_ID
                )

                engine = PermissionEngine(
                    is_main_admin=is_main,
                    permissions=perms,
                )
                return admin_row, engine
        except Exception as exc:
            logger.exception(f"Falha ao carregar admin {tg_user.id}: {exc}")
            return None, None

    # --------------------------------------------------------
    def _resolve_required(self, event: TelegramObject) -> Optional[str]:
        """
        Descobre qual permissão é necessária para o evento atual.
        Ordem de prioridade:
          1. required_permission definido no construtor.
          2. required_callback_prefix (para callbacks com prefixo conhecido).
          3. Prefixo do callback_data (ex.: "admin:products:create" → "products:create").
        """
        if self.required_permission:
            return self.required_permission

        if isinstance(event, CallbackQuery) and event.data:
            data = event.data
            if data.startswith("admin:"):
                # "admin:products:create" → "products:create"
                parts = data.split(":")
                if len(parts) >= 2:
                    return ":".join(parts[1:])
            if self.required_callback_prefix and data.startswith(
                self.required_callback_prefix
            ):
                return self.required_callback_prefix

        return None

    # --------------------------------------------------------
    async def _reject(self, event: TelegramObject, text: str) -> None:
        """Rejeita o acesso sem acumular mensagens."""
        try:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("⛔ Acesso negado.", show_alert=True)
                except Exception:
                    pass
                if event.message:
                    try:
                        await event.message.edit_text(text, parse_mode="HTML")
                    except Exception:
                        pass
            elif isinstance(event, Message):
                try:
                    await event.delete()
                except Exception:
                    pass
                await event.answer(text, parse_mode="HTML")
        except Exception as exc:
            logger.debug(f"Falha ao rejeitar admin: {exc}")

    # --------------------------------------------------------
    @staticmethod
    def _to_list(value: Any) -> List[str]:
        """Aceita lista, tupla ou string JSON."""
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value]
        if isinstance(value, str):
            # se for JSON string
            try:
                import json

                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed]
            except Exception:
                pass
            return [value]
        return []


# ============================================================
# 🎯 DECORATORS PRONTOS
# ============================================================
def require_permission(permission: str) -> AdminGuardMiddleware:
    """
    Retorna um middleware configurado para exigir permissão específica.
    Uso:
        router.message.middleware(require_permission("products:create"))
    """
    return AdminGuardMiddleware(required_permission=permission)


def require_admin() -> AdminGuardMiddleware:
    """
    Exige apenas ser admin (sem permissão específica).
    """
    return AdminGuardMiddleware(required_permission=None)


# ============================================================
# 🧩 REGISTRADOR
# ============================================================
def register_admin_guard(admin_router) -> None:
    """
    Registra o AdminGuardMiddleware no router administrativo.
    Deve ser chamado no router admin, NÃO no dispatcher global.
    """
    guard = AdminGuardMiddleware()
    admin_router.message.middleware(guard)
    admin_router.callback_query.middleware(guard)
    logger.info("Admin guard middleware registrado.")


__all__ = [
    "PermissionEngine",
    "AdminGuardMiddleware",
    "require_permission",
    "require_admin",
    "register_admin_guard",
]
