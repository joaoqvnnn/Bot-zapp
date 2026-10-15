"""
Larizinha Store — Camada de Banco de Dados (Bot)
Espelha 1:1 o schema Prisma (src/prisma/schema.prisma).
Prisma é a fonte de verdade das MIGRATIONS — este arquivo só DECLARA os models
para o bot Python poder ler/escrever nas mesmas tabelas/colunas.
"""
from __future__ import annotations

import enum
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bot.config import settings


# ============================================================
# 🧱 BASE DECLARATIVA
# ============================================================
class Base(DeclarativeBase):
    """Base declarativa SQLAlchemy 2.x."""


# ============================================================
# 🎛️ ENUMS (espelham os enums do Prisma)
# ============================================================
class UserStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    BANNED = "BANNED"
    DELETED = "DELETED"


class PaymentMethod(str, enum.Enum):
    BALANCE = "BALANCE"
    PIX = "PIX"
    GIFT_CARD = "GIFT_CARD"
    MANUAL = "MANUAL"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    DELIVERED = "DELIVERED"
    CANCELED = "CANCELED"
    REFUNDED = "REFUNDED"
    EXPIRED = "EXPIRED"


class PixStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"
    REFUNDED = "REFUNDED"


class TransactionType(str, enum.Enum):
    DEPOSIT = "DEPOSIT"
    WITHDRAW = "WITHDRAW"
    PURCHASE = "PURCHASE"
    REFUND = "REFUND"
    COMMISSION = "COMMISSION"
    GIFT = "GIFT"
    ADMIN_CREDIT = "ADMIN_CREDIT"
    ADMIN_DEBIT = "ADMIN_DEBIT"
    BONUS = "BONUS"


class WithdrawStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    APPROVED = "APPROVED"
    PAID = "PAID"
    REFUSED = "REFUSED"
    CANCELED = "CANCELED"


class CouponType(str, enum.Enum):
    FIXED = "FIXED"
    PERCENT = "PERCENT"
    BONUS = "BONUS"


class CommissionType(str, enum.Enum):
    PURCHASE = "PURCHASE"
    DEPOSIT = "DEPOSIT"
    LEVEL_BONUS = "LEVEL_BONUS"


class CommissionStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    PAID = "PAID"
    REVERSED = "REVERSED"


class ChannelType(str, enum.Enum):
    CHANNEL = "CHANNEL"
    GROUP = "GROUP"


class BroadcastStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    CANCELED = "CANCELED"
    FAILED = "FAILED"


# ============================================================
# 👥 USERS
# ============================================================
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column("telegram_id", BigInteger, unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column("first_name", String, nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column("last_name", String, nullable=True)
    language_code: Mapped[Optional[str]] = mapped_column("language_code", String, nullable=True)
    is_premium: Mapped[bool] = mapped_column("is_premium", Boolean, default=False)
    photo_url: Mapped[Optional[str]] = mapped_column("photo_url", String, nullable=True)

    balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    balance_locked: Mapped[float] = mapped_column("balance_locked", Numeric(12, 2), default=0)
    total_spent: Mapped[float] = mapped_column("total_spent", Numeric(12, 2), default=0)
    total_deposited: Mapped[float] = mapped_column("total_deposited", Numeric(12, 2), default=0)
    total_withdrawn: Mapped[float] = mapped_column("total_withdrawn", Numeric(12, 2), default=0)
    total_gifts: Mapped[float] = mapped_column("total_gifts", Numeric(12, 2), default=0)
    purchases_count: Mapped[int] = mapped_column("purchases_count", Integer, default=0)

    whatsapp: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    pix_key: Mapped[Optional[str]] = mapped_column("pix_key", String, nullable=True)
    pix_key_type: Mapped[Optional[str]] = mapped_column("pix_key_type", String, nullable=True)

    withdraw_password: Mapped[Optional[str]] = mapped_column("withdraw_password", String, nullable=True)
    recovery_email: Mapped[Optional[str]] = mapped_column("recovery_email", String, nullable=True)

    referred_by_id: Mapped[Optional[int]] = mapped_column(
        "referred_by_id", BigInteger, ForeignKey("users.id"), nullable=True
    )

    affiliate_code: Mapped[Optional[str]] = mapped_column("affiliate_code", String, unique=True, nullable=True)
    affiliate_active: Mapped[bool] = mapped_column("affiliate_active", Boolean, default=False)
    affiliate_level: Mapped[str] = mapped_column("affiliate_level", String, default="iniciante")
    affiliate_balance: Mapped[float] = mapped_column("affiliate_balance", Numeric(12, 2), default=0)

    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, name="UserStatus"), default=UserStatus.ACTIVE
    )
    ban_reason: Mapped[Optional[str]] = mapped_column("ban_reason", String, nullable=True)
    block_reason: Mapped[Optional[str]] = mapped_column("block_reason", String, nullable=True)
    alerts_enabled: Mapped[dict] = mapped_column("alerts_enabled", JSONB, default=list)

    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_seen_at: Mapped[Optional["DateTime"]] = mapped_column(
        "last_seen_at", DateTime(timezone=True), nullable=True
    )
    last_admin_msg_at: Mapped[Optional["DateTime"]] = mapped_column(
        "last_admin_msg_at", DateTime(timezone=True), nullable=True
    )

    # Relacionamentos
    referrals: Mapped[list["User"]] = relationship(
        "User", back_populates="referred_by", remote_side="User.referred_by_id"
    )
    referred_by: Mapped[Optional["User"]] = relationship(
        "User", back_populates="referrals", remote_side="User.id"
    )

    __table_args__ = (
        Index("ix_users_telegram_id", "telegram_id"),
        Index("ix_users_status", "status"),
        Index("ix_users_affiliate_code", "affiliate_code"),
        Index("ix_users_referred_by_id", "referred_by_id"),
    )


# ============================================================
# 👑 ADMINS & ROLES
# ============================================================
class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    permissions: Mapped[dict] = mapped_column(JSONB, default=list)
    is_system: Mapped[bool] = mapped_column("is_system", Boolean, default=False)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column("telegram_id", BigInteger, unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column("first_name", String, nullable=True)
    is_main_admin: Mapped[bool] = mapped_column("is_main_admin", Boolean, default=False)
    role_id: Mapped[Optional[int]] = mapped_column(
        "role_id", BigInteger, ForeignKey("roles.id"), nullable=True
    )
    permissions: Mapped[dict] = mapped_column(JSONB, default=list)
    last_login_at: Mapped[Optional["DateTime"]] = mapped_column(
        "last_login_at", DateTime(timezone=True), nullable=True
    )
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ============================================================
# 📦 CATÁLOGO
# ============================================================
class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    emoji: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    subtitle: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    category_id: Mapped[Optional[int]] = mapped_column(
        "category_id", BigInteger, ForeignKey("categories.id"), nullable=True
    )

    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    price_promo: Mapped[Optional[float]] = mapped_column("price_promo", Numeric(12, 2), nullable=True)
    cost: Mapped[float] = mapped_column(Numeric(12, 2), default=0)

    stock: Mapped[int] = mapped_column(Integer, default=0)
    stock_min: Mapped[int] = mapped_column("stock_min", Integer, default=0)
    unlimited: Mapped[bool] = mapped_column(Boolean, default=False)
    per_user_limit: Mapped[int] = mapped_column("per_user_limit", Integer, default=0)

    order: Mapped[int] = mapped_column(Integer, default=0)
    featured: Mapped[bool] = mapped_column(Boolean, default=False)
    sold_out: Mapped[bool] = mapped_column("sold_out", Boolean, default=False)
    visible: Mapped[bool] = mapped_column(Boolean, default=True)

    image_telegram: Mapped[Optional[str]] = mapped_column("image_telegram", String, nullable=True)
    image_webapp: Mapped[Optional[str]] = mapped_column("image_webapp", String, nullable=True)
    file_url: Mapped[Optional[str]] = mapped_column("file_url", String, nullable=True)

    note_product: Mapped[Optional[str]] = mapped_column("note_product", String, nullable=True)
    note_activation: Mapped[Optional[str]] = mapped_column("note_activation", String, nullable=True)
    activation_link: Mapped[Optional[str]] = mapped_column("activation_link", String, nullable=True)
    warranty_days: Mapped[int] = mapped_column("warranty_days", Integer, default=0)
    expiration_days: Mapped[int] = mapped_column("expiration_days", Integer, default=0)

    auto_delivery: Mapped[bool] = mapped_column("auto_delivery", Boolean, default=True)
    human_delivery: Mapped[bool] = mapped_column("human_delivery", Boolean, default=False)

    views_count: Mapped[int] = mapped_column("views_count", Integer, default=0)
    sold_count: Mapped[int] = mapped_column("sold_count", Integer, default=0)

    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_products_category_id", "category_id"),
        Index("ix_products_visible_sold_out", "visible", "sold_out"),
    )


class ProductImage(Base):
    __tablename__ = "product_images"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        "product_id", BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String, nullable=False)
    order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


class ProductFile(Base):
    __tablename__ = "product_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        "product_id", BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column("mime_type", String, nullable=True)
    size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


class ProductCredential(Base):
    __tablename__ = "product_credentials"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        "product_id", BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    payload: Mapped[str] = mapped_column(String, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    used_at: Mapped[Optional["DateTime"]] = mapped_column(
        "used_at", DateTime(timezone=True), nullable=True
    )
    order_id: Mapped[Optional[int]] = mapped_column("order_id", BigInteger, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_product_credentials_product_used", "product_id", "used"),)


# ============================================================
# 🛒 ORDERS
# ============================================================
class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        "user_id", BigInteger, ForeignKey("users.id"), nullable=False
    )
    total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    discount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    final_total: Mapped[float] = mapped_column("final_total", Numeric(12, 2), nullable=False)
    coupon_id: Mapped[Optional[int]] = mapped_column(
        "coupon_id", BigInteger, ForeignKey("coupons.id"), nullable=True
    )
    method: Mapped[PaymentMethod] = mapped_column(SAEnum(PaymentMethod, name="PaymentMethod"))
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="OrderStatus"), default=OrderStatus.PENDING
    )
    delivered_at: Mapped[Optional["DateTime"]] = mapped_column(
        "delivered_at", DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional["DateTime"]] = mapped_column(
        "expires_at", DateTime(timezone=True), nullable=True
    )
    refunded_at: Mapped[Optional["DateTime"]] = mapped_column(
        "refunded_at", DateTime(timezone=True), nullable=True
    )
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_orders_user_id", "user_id"),
        Index("ix_orders_status", "status"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        "order_id", BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        "product_id", BigInteger, ForeignKey("products.id"), nullable=False
    )
    product_name: Mapped[str] = mapped_column("product_name", String, nullable=False)
    unit_price: Mapped[float] = mapped_column("unit_price", Numeric(12, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    credentials: Mapped[dict] = mapped_column(JSONB, default=list)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


# ============================================================
# 💳 PIX / TRANSAÇÕES
# ============================================================
class PixCharge(Base):
    __tablename__ = "pix_charges"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    txid: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column("user_id", BigInteger, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    bonus: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    qr_code: Mapped[Optional[str]] = mapped_column("qr_code", Text, nullable=True)
    qr_code_base64: Mapped[Optional[str]] = mapped_column("qr_code_base64", Text, nullable=True)
    copy_paste: Mapped[Optional[str]] = mapped_column("copy_paste", Text, nullable=True)
    mp_payment_id: Mapped[Optional[str]] = mapped_column("mp_payment_id", String, nullable=True)
    status: Mapped[PixStatus] = mapped_column(
        SAEnum(PixStatus, name="PixStatus"), default=PixStatus.PENDING
    )
    expires_at: Mapped["DateTime"] = mapped_column("expires_at", DateTime(timezone=True), nullable=False)
    paid_at: Mapped[Optional["DateTime"]] = mapped_column(
        "paid_at", DateTime(timezone=True), nullable=True
    )
    order_id: Mapped[Optional[int]] = mapped_column("order_id", BigInteger, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_pix_charges_user_id", "user_id"),
        Index("ix_pix_charges_status", "status"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        "user_id", BigInteger, ForeignKey("users.id"), nullable=False
    )
    type: Mapped[TransactionType] = mapped_column(
        SAEnum(TransactionType, name="TransactionType")
    )
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    balance_after: Mapped[float] = mapped_column("balance_after", Numeric(12, 2), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reference: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_transactions_user_id", "user_id"),
        Index("ix_transactions_type", "type"),
    )


# ============================================================
# 💸 WITHDRAWS
# ============================================================
class Withdraw(Base):
    __tablename__ = "withdraws"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        "user_id", BigInteger, ForeignKey("users.id"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    net_amount: Mapped[float] = mapped_column("net_amount", Numeric(12, 2), nullable=False)
    pix_key: Mapped[str] = mapped_column("pix_key", String, nullable=False)
    pix_key_type: Mapped[str] = mapped_column("pix_key_type", String, nullable=False)
    status: Mapped[WithdrawStatus] = mapped_column(
        SAEnum(WithdrawStatus, name="WithdrawStatus"), default=WithdrawStatus.PENDING
    )
    approved_by_id: Mapped[Optional[int]] = mapped_column("approved_by_id", BigInteger, nullable=True)
    receipt_url: Mapped[Optional[str]] = mapped_column("receipt_url", String, nullable=True)
    receipt_sent_at: Mapped[Optional["DateTime"]] = mapped_column(
        "receipt_sent_at", DateTime(timezone=True), nullable=True
    )
    observation: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    processed_at: Mapped[Optional["DateTime"]] = mapped_column(
        "processed_at", DateTime(timezone=True), nullable=True
    )
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_withdraws_user_id", "user_id"),
        Index("ix_withdraws_status", "status"),
    )


# ============================================================
# 🎁 GIFT CARDS
# ============================================================
class GiftCard(Base):
    __tablename__ = "gift_cards"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    uses_max: Mapped[int] = mapped_column("uses_max", Integer, default=1)
    uses_count: Mapped[int] = mapped_column("uses_count", Integer, default=0)
    expires_at: Mapped[Optional["DateTime"]] = mapped_column(
        "expires_at", DateTime(timezone=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_id: Mapped[Optional[int]] = mapped_column("created_by_id", BigInteger, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


class GiftCardRedemption(Base):
    __tablename__ = "gift_card_redemptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gift_card_id: Mapped[int] = mapped_column(
        "gift_card_id", BigInteger, ForeignKey("gift_cards.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        "user_id", BigInteger, ForeignKey("users.id"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_gift_card_redemptions_user_id", "user_id"),)


# ============================================================
# 🎟️ CUPONS
# ============================================================
class Coupon(Base):
    __tablename__ = "coupons"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    type: Mapped[CouponType] = mapped_column(SAEnum(CouponType, name="CouponType"))
    value: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    min_purchase: Mapped[float] = mapped_column("min_purchase", Numeric(12, 2), default=0)
    uses_max: Mapped[int] = mapped_column("uses_max", Integer, default=0)
    uses_count: Mapped[int] = mapped_column("uses_count", Integer, default=0)
    per_user_limit: Mapped[int] = mapped_column("per_user_limit", Integer, default=1)
    expires_at: Mapped[Optional["DateTime"]] = mapped_column(
        "expires_at", DateTime(timezone=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CouponUsage(Base):
    __tablename__ = "coupon_usages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    coupon_id: Mapped[int] = mapped_column(
        "coupon_id", BigInteger, ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        "user_id", BigInteger, ForeignKey("users.id"), nullable=False
    )
    order_id: Mapped[Optional[int]] = mapped_column("order_id", BigInteger, nullable=True)
    discount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


# ============================================================
# 👥 COMISSÕES
# ============================================================
class Commission(Base):
    __tablename__ = "commissions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    earner_id: Mapped[int] = mapped_column(
        "earner_id", BigInteger, ForeignKey("users.id"), nullable=False
    )
    source_user_id: Mapped[int] = mapped_column(
        "source_user_id", BigInteger, ForeignKey("users.id"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    percent: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    type: Mapped[CommissionType] = mapped_column(
        SAEnum(CommissionType, name="CommissionType")
    )
    status: Mapped[CommissionStatus] = mapped_column(
        SAEnum(CommissionStatus, name="CommissionStatus"), default=CommissionStatus.PENDING
    )
    reference: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    paid_at: Mapped[Optional["DateTime"]] = mapped_column(
        "paid_at", DateTime(timezone=True), nullable=True
    )
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_commissions_earner_id", "earner_id"),
        Index("ix_commissions_status", "status"),
    )


# ============================================================
# 📡 CANAIS / PROMOÇÕES
# ============================================================
class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column("chat_id", BigInteger, unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[ChannelType] = mapped_column(SAEnum(ChannelType, name="ChannelType"))
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    invite_link: Mapped[Optional[str]] = mapped_column("invite_link", String, nullable=True)
    auto_post_buy: Mapped[bool] = mapped_column("auto_post_buy", Boolean, default=True)
    auto_post_stock: Mapped[bool] = mapped_column("auto_post_stock", Boolean, default=True)
    auto_post_promo: Mapped[bool] = mapped_column("auto_post_promo", Boolean, default=True)
    message_key: Mapped[Optional[str]] = mapped_column("message_key", String, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Promotion(Base):
    __tablename__ = "promotions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[Optional[int]] = mapped_column("product_id", BigInteger, nullable=True)
    message_key: Mapped[Optional[str]] = mapped_column("message_key", String, nullable=True)
    target_type: Mapped[str] = mapped_column("target_type", String, nullable=False)
    target_value: Mapped[Optional[str]] = mapped_column("target_value", String, nullable=True)
    scheduled_at: Mapped["DateTime"] = mapped_column("scheduled_at", DateTime(timezone=True), nullable=False)
    recurrence: Mapped[str] = mapped_column(String, default="none")
    status: Mapped[str] = mapped_column(String, default="pending")
    sent_at: Mapped[Optional["DateTime"]] = mapped_column(
        "sent_at", DateTime(timezone=True), nullable=True
    )
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


# ============================================================
# 📨 BROADCASTS
# ============================================================
class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_key: Mapped[Optional[str]] = mapped_column("message_key", String, nullable=True)
    content: Mapped[dict] = mapped_column(JSONB, default=dict)
    target_filter: Mapped[str] = mapped_column("target_filter", String, nullable=False)
    status: Mapped[BroadcastStatus] = mapped_column(
        SAEnum(BroadcastStatus, name="BroadcastStatus"), default=BroadcastStatus.PENDING
    )
    total: Mapped[int] = mapped_column(Integer, default=0)
    delivered: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_at: Mapped[Optional["DateTime"]] = mapped_column(
        "scheduled_at", DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[Optional["DateTime"]] = mapped_column(
        "started_at", DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional["DateTime"]] = mapped_column(
        "finished_at", DateTime(timezone=True), nullable=True
    )
    created_by_id: Mapped[Optional[int]] = mapped_column("created_by_id", BigInteger, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


# ============================================================
# ✏️ MENSAGENS E BOTÕES EDITÁVEIS
# ============================================================
class MessageTemplate(Base):
    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    screen: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[Optional[str]] = mapped_column("media_type", String, nullable=True)
    media_url: Mapped[Optional[str]] = mapped_column("media_url", String, nullable=True)
    parse_mode: Mapped[str] = mapped_column("parse_mode", String, default="HTML")
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MessageTemplateVersion(Base):
    __tablename__ = "message_template_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        "template_id",
        BigInteger,
        ForeignKey("message_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_id: Mapped[Optional[int]] = mapped_column("created_by_id", BigInteger, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


class ButtonTemplate(Base):
    __tablename__ = "button_templates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    screen: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    emoji: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    type: Mapped[str] = mapped_column(String, default="callback")
    callback_data: Mapped[Optional[str]] = mapped_column("callback_data", String, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    webapp_url: Mapped[Optional[str]] = mapped_column("webapp_url", String, nullable=True)
    row: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[int] = mapped_column(Integer, default=0)
    order: Mapped[int] = mapped_column(Integer, default=0)
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("screen", "key", name="uq_button_templates_screen_key"),
        Index("ix_button_templates_screen_visible", "screen", "visible"),
    )


# ============================================================
# ⚙️ SETTINGS (KV global)
# ============================================================
class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String, default="string")
    category: Mapped[str] = mapped_column(String, default="general")
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ============================================================
# 🤖 AUTOMAÇÕES
# ============================================================
class Automation(Base):
    __tablename__ = "automations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    delay_min: Mapped[Optional[int]] = mapped_column("delay_min", Integer, nullable=True)
    threshold: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    message_key: Mapped[Optional[str]] = mapped_column("message_key", String, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    updated_at: Mapped["DateTime"] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ============================================================
# 🔒 LOCKS (persistência além do Redis)
# ============================================================
class Lock(Base):
    __tablename__ = "locks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    product_id: Mapped[Optional[int]] = mapped_column("product_id", BigInteger, nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column("user_id", BigInteger, nullable=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped["DateTime"] = mapped_column("expires_at", DateTime(timezone=True), nullable=False)
    released_at: Mapped[Optional["DateTime"]] = mapped_column(
        "released_at", DateTime(timezone=True), nullable=True
    )
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_locks_expires_at", "expires_at"),)


# ============================================================
# 🚫 BLACKLIST
# ============================================================
class Blacklist(Base):
    __tablename__ = "blacklist"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column("telegram_id", BigInteger, unique=True, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    added_by_id: Mapped[Optional[int]] = mapped_column("added_by_id", BigInteger, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


# ============================================================
# 🎫 TICKETS
# ============================================================
class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    rarity: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    rewards: Mapped[dict] = mapped_column(JSONB, default=list)
    odds: Mapped[dict] = mapped_column(JSONB, default=dict)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    per_user_limit: Mapped[int] = mapped_column("per_user_limit", Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


class UserTicket(Base):
    __tablename__ = "user_tickets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        "ticket_id", BigInteger, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        "user_id", BigInteger, ForeignKey("users.id"), nullable=False
    )
    reward: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


# ============================================================
# 🎮 JOGOS
# ============================================================
class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cost: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    rewards: Mapped[dict] = mapped_column(JSONB, default=list)
    limits: Mapped[dict] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


class GameSession(Base):
    __tablename__ = "game_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        "game_id", BigInteger, ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column("user_id", BigInteger, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    cost: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    reward: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_game_sessions_user_id", "user_id"),)


# ============================================================
# 🛡️ SEGURANÇA / ALERTAS
# ============================================================
class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, default="info")
    user_id: Mapped[Optional[int]] = mapped_column("user_id", BigInteger, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_security_events_type", "type"),)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, default="info")
    title: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[Optional["DateTime"]] = mapped_column(
        "resolved_at", DateTime(timezone=True), nullable=True
    )
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_alerts_category_resolved", "category", "resolved"),)


# ============================================================
# 📜 LOGS
# ============================================================
class Log(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    admin_id: Mapped[Optional[int]] = mapped_column(
        "admin_id", BigInteger, ForeignKey("admins.id"), nullable=True
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        "user_id", BigInteger, ForeignKey("users.id"), nullable=True
    )
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_logs_category", "category"),
        Index("ix_logs_admin_id", "admin_id"),
    )


# ============================================================
# 💾 BACKUPS
# ============================================================
class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String, default="auto")
    status: Mapped[str] = mapped_column(String, default="ok")
    created_at: Mapped["DateTime"] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now()
    )


# ============================================================
# ⚙️ ENGINE / SESSION FACTORY
# ============================================================
_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine() -> AsyncEngine:
    """Retorna a engine assíncrona (singleton)."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.db_url_async,
            echo=settings.DB_ECHO,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_timeout=settings.DB_POOL_TIMEOUT,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Retorna a factory de sessões (singleton)."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager transacional.

    Uso:
        async with session_scope() as session:
            ...
    """
    factory = get_session_factory()
    session: AsyncSession = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency-injection style (FastAPI/Aiogram middlewares)."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """
    Inicialização: apenas valida conexão e cria enums/tabelas que ainda
    não existam. Em produção as MIGRATIONS do Prisma são a fonte de verdade.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))


async def close_db() -> None:
    """Fecha engine e descarta cache de sessão."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def healthcheck() -> bool:
    """Retorna True se a conexão com o banco está OK."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


__all__ = [
    # Base
    "Base",
    "get_engine",
    "get_session_factory",
    "get_session",
    "session_scope",
    "init_db",
    "close_db",
    "healthcheck",
    # Enums
    "UserStatus",
    "PaymentMethod",
    "OrderStatus",
    "PixStatus",
    "TransactionType",
    "WithdrawStatus",
    "CouponType",
    "CommissionType",
    "CommissionStatus",
    "ChannelType",
    "BroadcastStatus",
    # Models
    "User",
    "Admin",
    "Role",
    "Category",
    "Product",
    "ProductImage",
    "ProductFile",
    "ProductCredential",
    "Order",
    "OrderItem",
    "PixCharge",
    "Transaction",
    "Withdraw",
    "GiftCard",
    "GiftCardRedemption",
    "Coupon",
    "CouponUsage",
    "Commission",
    "Channel",
    "Promotion",
    "Broadcast",
    "MessageTemplate",
    "MessageTemplateVersion",
    "ButtonTemplate",
    "Setting",
    "Automation",
    "Lock",
    "Blacklist",
    "Ticket",
    "UserTicket",
    "Game",
    "GameSession",
    "SecurityEvent",
    "Alert",
    "Log",
    "Backup",
]
