"""
Larizinha Store — Logger & Auditoria
- Loguru configurado (console colorido + arquivo rotativo + retenção)
- Integração opcional com Sentry
- Helpers de auditoria para o painel admin (grava em `logs` no Postgres)
- Contexto global (user_id / admin_id / action) via `bind`
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger as _loguru_logger

from bot.config import settings


# ============================================================
# 📁 PREPARAÇÃO DE DIRETÓRIOS
# ============================================================
def _ensure_dirs() -> None:
    logs_dir: Path = settings.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)


# ============================================================
# 🎨 FORMATOS
# ============================================================
_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "<level>{message}</level>"
    "{exception}"
)

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level: <8} | "
    "{name}:{function}:{line} | "
    "{message}"
    "{exception}"
)

_AUDIT_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "AUDIT | {extra[category]} | {message}"
)


# ============================================================
# 🧩 SINKS PERSONALIZADOS
# ============================================================
def _console_sink(message: Any) -> None:
    """Console — vai para stdout (Render captura)."""
    sys.stdout.write(str(message))


def _error_sink(message: Any) -> None:
    """Console de erro — vai para stderr."""
    sys.stderr.write(str(message))


# ============================================================
# ⚙️ CONFIGURAÇÃO DO LOGURU
# ============================================================
def setup_logger() -> None:
    """Configura o logger global (console + arquivos rotativos)."""
    _ensure_dirs()

    # Remove handlers padrão
    _loguru_logger.remove()

    # --------------------------------------------
    # Console
    # --------------------------------------------
    if sys.stdout.isatty() or settings.is_development:
        # terminal interativo → cores
        _loguru_logger.add(
            _console_sink,
            level=settings.LOG_LEVEL,
            format=_CONSOLE_FORMAT,
            colorize=True,
            backtrace=True,
            diagnose=settings.is_development,
            enqueue=True,
        )
    else:
        # produção (Render) → sem cores
        _loguru_logger.add(
            _console_sink,
            level=settings.LOG_LEVEL,
            format=_FILE_FORMAT,
            colorize=False,
            backtrace=True,
            diagnose=False,
            enqueue=True,
        )

    # --------------------------------------------
    # Arquivo principal (todos os logs)
    # --------------------------------------------
    if settings.LOG_TO_FILE:
        _loguru_logger.add(
            settings.LOG_FILE_PATH,
            level=settings.LOG_LEVEL,
            format=_FILE_FORMAT,
            rotation=settings.LOG_ROTATION,
            retention=settings.LOG_RETENTION,
            compression="zip",
            encoding="utf-8",
            enqueue=True,
            backtrace=True,
            diagnose=False,
        )

        # Erros separados
        error_path = Path(settings.LOG_FILE_PATH).parent / "errors.log"
        _loguru_logger.add(
            str(error_path),
            level="ERROR",
            format=_FILE_FORMAT,
            rotation=settings.LOG_ROTATION,
            retention=settings.LOG_RETENTION,
            compression="zip",
            encoding="utf-8",
            enqueue=True,
            backtrace=True,
            diagnose=False,
        )

        # Auditoria (ações administrativas e financeiras)
        audit_path = Path(settings.LOG_FILE_PATH).parent / "audit.log"
        _loguru_logger.add(
            str(audit_path),
            level="INFO",
            format=_AUDIT_FORMAT,
            rotation="20 MB",
            retention="30 days",
            compression="zip",
            encoding="utf-8",
            filter=lambda record: "audit" in record["extra"].get("category", ""),
            enqueue=True,
        )

        # Pagamentos (PIX, saques, comissões)
        payments_path = Path(settings.LOG_FILE_PATH).parent / "payments.log"
        _loguru_logger.add(
            str(payments_path),
            level="INFO",
            format=_AUDIT_FORMAT,
            rotation="20 MB",
            retention="60 days",
            compression="zip",
            encoding="utf-8",
            filter=lambda record: record["extra"].get("category") == "payments",
            enqueue=True,
        )

        # Segurança (antifraude, bloqueios, tentativas)
        security_path = Path(settings.LOG_FILE_PATH).parent / "security.log"
        _loguru_logger.add(
            str(security_path),
            level="WARNING",
            format=_AUDIT_FORMAT,
            rotation="20 MB",
            retention="90 days",
            compression="zip",
            encoding="utf-8",
            filter=lambda record: record["extra"].get("category") == "security",
            enqueue=True,
        )

    # --------------------------------------------
    # Sentry (opcional)
    # --------------------------------------------
    if settings.SENTRY_DSN:
        try:
            import sentry_sdk  # type: ignore
            from sentry_sdk.integrations.logging import (  # type: ignore
                LoggingIntegration,
            )

            sentry_sdk.init(  # type: ignore
                dsn=settings.SENTRY_DSN,
                environment=settings.ENV,
                release=settings.APP_VERSION,
                traces_sample_rate=0.1 if settings.is_production else 1.0,
                integrations=[LoggingIntegration(level=None, event_level="ERROR")],
                before_send=lambda event, hint: event,
            )
            _loguru_logger.info("Sentry inicializado.")
        except Exception as exc:  # pragma: no cover
            _loguru_logger.warning(f"Falha ao inicializar Sentry: {exc}")


# ============================================================
# 🧵 LOGGER GLOBAL
# ============================================================
logger = _loguru_logger


# ============================================================
# 🏷️ CONTEXTO — user / admin / action
# ============================================================
def with_context(
    user_id: Optional[int] = None,
    admin_id: Optional[int] = None,
    action: Optional[str] = None,
    category: Optional[str] = None,
    **extra: Any,
):
    """
    Retorna um logger "bindado" com contexto extra.
    Ex.: `log = with_context(user_id=123, action='buy'); log.info('...')`
    """
    data: dict[str, Any] = {}
    if user_id is not None:
        data["user_id"] = user_id
    if admin_id is not None:
        data["admin_id"] = admin_id
    if action is not None:
        data["action"] = action
    if category is not None:
        data["category"] = category
    data.update(extra)
    return logger.bind(**data)


# ============================================================
# 📝 HELPERS DE AUDITORIA
# ============================================================
def audit(
    action: str,
    *,
    admin_id: Optional[int] = None,
    user_id: Optional[int] = None,
    details: Optional[dict] = None,
    category: str = "audit",
) -> None:
    """
    Registra uma ação de auditoria (vai para audit.log e, opcionalmente, DB).
    Use para TODA ação administrativa e financeira.
    """
    payload = {
        "action": action,
        "admin_id": admin_id,
        "user_id": user_id,
        "details": details or {},
        "ts": datetime.utcnow().isoformat(),
    }
    logger.bind(category=category).info(f"{action} | {payload}")


def log_payment(
    action: str,
    *,
    user_id: Optional[int] = None,
    amount: Optional[float] = None,
    status: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Log especializado para pagamentos (PIX, saques, comissões)."""
    payload = {
        "user_id": user_id,
        "amount": amount,
        "status": status,
        "details": details or {},
        "ts": datetime.utcnow().isoformat(),
    }
    logger.bind(category="payments").info(f"{action} | {payload}")


def log_security(
    action: str,
    *,
    user_id: Optional[int] = None,
    severity: str = "warning",
    details: Optional[dict] = None,
) -> None:
    """Log especializado para segurança (antifraude, bloqueios)."""
    payload = {
        "user_id": user_id,
        "severity": severity,
        "details": details or {},
        "ts": datetime.utcnow().isoformat(),
    }
    log = logger.bind(category="security")
    if severity in ("critical", "error"):
        log.error(f"{action} | {payload}")
    elif severity == "warning":
        log.warning(f"{action} | {payload}")
    else:
        log.info(f"{action} | {payload}")


def log_error(
    exc: BaseException,
    *,
    context: Optional[dict] = None,
    user_id: Optional[int] = None,
    admin_id: Optional[int] = None,
) -> None:
    """Registra exceção com contexto."""
    logger.bind(
        user_id=user_id,
        admin_id=admin_id,
        context=context or {},
    ).exception(exc)


# ============================================================
# 🎯 ATALHOS
# ============================================================
def debug(msg: str, **kw: Any) -> None:
    logger.bind(**kw).debug(msg)


def info(msg: str, **kw: Any) -> None:
    logger.bind(**kw).info(msg)


def warning(msg: str, **kw: Any) -> None:
    logger.bind(**kw).warning(msg)


def error(msg: str, **kw: Any) -> None:
    logger.bind(**kw).error(msg)


def critical(msg: str, **kw: Any) -> None:
    logger.bind(**kw).critical(msg)


def exception(msg: str, **kw: Any) -> None:
    logger.bind(**kw).exception(msg)


# ============================================================
# 🧹 HELPERS DE LIMPEZA
# ============================================================
def cleanup_old_logs(days: int = 30) -> int:
    """
    Remove arquivos .zip / .log mais antigos que `days`.
    Retorna quantidade removida.
    """
    logs_dir: Path = settings.logs_dir
    if not logs_dir.exists():
        return 0
    cutoff = datetime.now().timestamp() - days * 86400
    removed = 0
    for f in logs_dir.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".log", ".zip", ".gz"):
            continue
        if f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except Exception:
                pass
    if removed:
        logger.info(f"Limpeza de logs: {removed} arquivos antigos removidos.")
    return removed


# ============================================================
# 🚀 INICIALIZAÇÃO AUTOMÁTICA
# ============================================================
setup_logger()


__all__ = [
    "logger",
    "setup_logger",
    "with_context",
    "audit",
    "log_payment",
    "log_security",
    "log_error",
    "debug",
    "info",
    "warning",
    "error",
    "critical",
    "exception",
    "cleanup_old_logs",
]
