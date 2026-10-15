"""
Larizinha Store — Configuração Central do Bot
Carrega e valida TODAS as variáveis de ambiente usadas pelo bot + worker.
Espelha 1:1 o arquivo .env.example da raiz do projeto.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configurações globais — carregadas do .env na raiz do projeto."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ==========================================================
    # 🔧 AMBIENTE / APLICAÇÃO
    # ==========================================================
    ENV: str = "production"
    DEBUG: bool = False
    APP_NAME: str = "Larizinha Store"
    APP_VERSION: str = "1.0.0"
    TIMEZONE: str = "America/Sao_Paulo"
    DEFAULT_LANGUAGE: str = "pt-BR"
    DEFAULT_CURRENCY: str = "BRL"

    # ==========================================================
    # 🤖 TELEGRAM
    # ==========================================================
    BOT_TOKEN: str = Field(..., min_length=20)
    BOT_USERNAME: str = ""
    BOT_ID: int = 0
    ADMIN_IDS: str = ""
    MAIN_ADMIN_ID: int = 0

    REQUIRED_CHANNEL_ID: int = 0
    REQUIRED_CHANNEL_USERNAME: str = ""
    REQUIRED_GROUP_ID: int = 0
    REQUIRED_GROUP_USERNAME: str = ""
    SUPPORT_SILENT_LINK: str = ""

    # ==========================================================
    # 🗄️ POSTGRESQL
    # ==========================================================
    DATABASE_URL: str = Field(..., min_length=10)
    DATABASE_URL_SYNC: str = ""
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_ECHO: bool = False

    # ==========================================================
    # ⚡ REDIS
    # ==========================================================
    REDIS_URL: str = Field(..., min_length=10)
    REDIS_PREFIX: str = "larizinha"
    REDIS_LOCK_TTL: int = 300
    REDIS_VIEW_TTL: int = 120
    REDIS_HEARTBEAT_TTL: int = 60

    # ==========================================================
    # 💳 MERCADO PAGO
    # ==========================================================
    MP_ACCESS_TOKEN: str = ""
    MP_PUBLIC_KEY: str = ""
    MP_CLIENT_ID: str = ""
    MP_CLIENT_SECRET: str = ""
    MP_WEBHOOK_SECRET: str = ""
    MP_WEBHOOK_URL: str = ""
    MP_PIX_EXPIRATION_MINUTES: int = 30
    MP_PIX_STATEMENT_DESCRIPTOR: str = "LARIZINHASTORE"

    # ==========================================================
    # 📧 SMTP
    # ==========================================================
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USE_TLS: bool = True
    SMTP_EMAIL: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_NAME: str = "Larizinha Store"
    SMTP_FROM_EMAIL: str = ""

    # ==========================================================
    # 🌐 WEBHOOK / API
    # ==========================================================
    WEBHOOK_HOST: str = "0.0.0.0"
    WEBHOOK_PORT: int = 8000
    WEBHOOK_PATH: str = "/webhook"
    WEBHOOK_SECRET_TOKEN: str = ""
    API_BASE_URL: str = "http://localhost:8000"
    TELEGRAM_WEBHOOK_URL: str = ""

    # ==========================================================
    # 🖥️ WEBAPP
    # ==========================================================
    WEBAPP_URL: str = ""
    WEBAPP_SECRET: str = ""
    WEBAPP_AUTH_MAX_AGE: int = 86400

    # ==========================================================
    # 💰 REGRAS FINANCEIRAS
    # ==========================================================
    MIN_DEPOSIT: float = 10.00
    MAX_DEPOSIT: float = 5000.00
    MIN_WITHDRAW: float = 20.00
    MAX_WITHDRAW: float = 5000.00
    WITHDRAW_FEE_PERCENT: float = 0.0
    WITHDRAW_DAILY_LIMIT: int = 3
    WITHDRAW_AUTO_APPROVE: bool = False
    WITHDRAW_PROCESSING_DAYS: int = 1
    DEPOSIT_BONUS_PERCENT: float = 0.0
    DEPOSIT_BONUS_MIN: float = 50.00
    INITIAL_BALANCE: float = 0.00

    # ==========================================================
    # 🎁 AFILIADOS
    # ==========================================================
    AFFILIATE_COMMISSION_PERCENT: float = 10.0
    AFFILIATE_COMMISSION_DEPOSIT_PERCENT: float = 5.0
    AFFILIATE_MIN_WITHDRAW: float = 20.00
    AFFILIATE_ENABLED: bool = True

    # ==========================================================
    # ⏱️ AUTOMAÇÕES
    # ==========================================================
    AUTO_PIX_EXPIRED: bool = True
    AUTO_PIX_EXPIRED_DELAY: int = 30

    AUTO_PIX_UNPAID: bool = True
    AUTO_PIX_UNPAID_DELAY: int = 60

    AUTO_CART_ABANDONED: bool = True
    AUTO_CART_ABANDONED_DELAY: int = 15

    AUTO_NEW_PROMO: bool = True
    AUTO_NEW_PROMO_DELAY: int = 5

    AUTO_LOW_STOCK: bool = True
    AUTO_LOW_STOCK_THRESHOLD: int = 5

    AUTO_HEARTBEAT: bool = True
    AUTO_HEARTBEAT_TIMEOUT: int = 120

    # ==========================================================
    # 🔒 TRAVAS / CONCORRÊNCIA
    # ==========================================================
    ENABLE_CONCURRENCY_LOCK: bool = True
    PRODUCT_LOCK_TTL: int = 300
    USER_VIEW_TTL: int = 120

    # ==========================================================
    # 🛡️ SEGURANÇA
    # ==========================================================
    ANTIFRAUD_ENABLED: bool = True
    ANTIFRAUD_MAX_ACCOUNTS_PER_IP: int = 3
    ANTIFRAUD_SUSPICIOUS_AMOUNT: float = 1000.00
    THROTTLE_RATE: float = 0.5
    TEMP_BLOCK_MINUTES: int = 15

    # ==========================================================
    # 📊 LOGS
    # ==========================================================
    LOG_LEVEL: str = "INFO"
    LOG_TO_FILE: bool = True
    LOG_FILE_PATH: str = "./logs/bot.log"
    LOG_ROTATION: str = "10 MB"
    LOG_RETENTION: str = "7 days"
    SENTRY_DSN: str = ""

    # ==========================================================
    # 💾 BACKUP
    # ==========================================================
    AUTO_BACKUP_ENABLED: bool = True
    AUTO_BACKUP_INTERVAL_HOURS: int = 24
    BACKUP_RETENTION_DAYS: int = 7
    BACKUP_PATH: str = "./backups"

    # ==========================================================
    # 🚀 RENDER
    # ==========================================================
    RENDER: bool = False
    RENDER_EXTERNAL_URL: str = ""
    PORT: int = 8000

    # ==========================================================
    # ✅ VALIDADORES
    # ==========================================================
    @field_validator("BOT_TOKEN")
    @classmethod
    def _validate_bot_token(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError("BOT_TOKEN inválido (formato: 0000:AAAA...)")
        return v

    @field_validator("TIMEZONE")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"TIMEZONE inválido: {v}") from exc
        return v

    # ==========================================================
    # 🧮 PROPRIEDADES DERIVADAS
    # ==========================================================
    @property
    def admin_ids_list(self) -> List[int]:
        """Lista de IDs de admins (parseado de ADMIN_IDS)."""
        raw = (self.ADMIN_IDS or "").strip()
        if not raw:
            return []
        result: List[int] = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                result.append(int(chunk))
        if self.MAIN_ADMIN_ID and self.MAIN_ADMIN_ID not in result:
            result.insert(0, self.MAIN_ADMIN_ID)
        return result

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() == "production"

    @property
    def is_development(self) -> bool:
        return self.ENV.lower() in ("development", "dev", "local")

    @property
    def logs_dir(self) -> Path:
        return Path(self.LOG_FILE_PATH).parent

    @property
    def backups_dir(self) -> Path:
        return Path(self.BACKUP_PATH)

    @property
    def db_url_async(self) -> str:
        """Garante driver asyncpg."""
        url = self.DATABASE_URL
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def db_url_sync(self) -> str:
        """URL sync para migrações/backup."""
        if self.DATABASE_URL_SYNC:
            return self.DATABASE_URL_SYNC
        url = self.DATABASE_URL
        if url.startswith("postgresql+asyncpg://"):
            url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
        return url

    def __repr__(self) -> str:
        return (
            f"<Settings env={self.ENV} bot=@{self.BOT_USERNAME} "
            f"admins={len(self.admin_ids_list)} debug={self.DEBUG}>"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Retorna a instância única (singleton) das configurações.
    Cacheada via lru_cache — só carrega o .env uma vez.
    """
    return Settings()


# Instância global para import direto: `from bot.config import settings`
settings: Settings = get_settings()
