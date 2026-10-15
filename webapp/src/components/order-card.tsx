/**
 * Larizinha Store — WebApp Next.js
 * OrderCard — card de pedido (usado em "Meus Pedidos" e detalhes).
 */
"use client";

import * as React from "react";
import Link from "next/link";
import {
  CheckCircle2,
  Clock,
  XCircle,
  RotateCcw,
  AlertTriangle,
  Calendar,
  ShoppingBag,
  ChevronRight,
} from "lucide-react";

import { cn, formatBRL, formatDate } from "@/lib/utils";
import type { Order } from "@/lib/api";
import { useTelegram } from "@/components/providers";

// ============================================================
// 🎛️ PROPS
// ============================================================
interface OrderCardProps {
  order: Order;
  variant?: "compact" | "full";
  showActions?: boolean;
  className?: string;
  onClick?: (order: Order) => void;
}

// ============================================================
// 🏷️ STATUS
// ============================================================
type OrderStatus =
  | "PENDING"
  | "PAID"
  | "DELIVERED"
  | "CANCELED"
  | "REFUNDED"
  | "EXPIRED";

const STATUS_MAP: Record<
  OrderStatus,
  {
    label: string;
    emoji: string;
    Icon: React.ComponentType<{ className?: string }>;
    className: string;
    dot: string;
  }
> = {
  PENDING: {
    label: "Pendente",
    emoji: "⏳",
    Icon: Clock,
    className: "text-yellow-500 bg-yellow-500/10 border-yellow-500/30",
    dot: "bg-yellow-500",
  },
  PAID: {
    label: "Pago",
    emoji: "💰",
    Icon: CheckCircle2,
    className: "text-emerald-500 bg-emerald-500/10 border-emerald-500/30",
    dot: "bg-emerald-500",
  },
  DELIVERED: {
    label: "Entregue",
    emoji: "✅",
    Icon: CheckCircle2,
    className: "text-emerald-500 bg-emerald-500/10 border-emerald-500/30",
    dot: "bg-emerald-500",
  },
  CANCELED: {
    label: "Cancelado",
    emoji: "❌",
    Icon: XCircle,
    className: "text-muted-foreground bg-secondary/60 border-border",
    dot: "bg-muted-foreground",
  },
  REFUNDED: {
    label: "Reembolsado",
    emoji: "♻️",
    Icon: RotateCcw,
    className: "text-blue-500 bg-blue-500/10 border-blue-500/30",
    dot: "bg-blue-500",
  },
  EXPIRED: {
    label: "Expirado",
    emoji: "⌛️",
    Icon: AlertTriangle,
    className: "text-orange-500 bg-orange-500/10 border-orange-500/30",
    dot: "bg-orange-500",
  },
};

function StatusBadge({ status }: { status: OrderStatus }) {
  const meta = STATUS_MAP[status] ?? STATUS_MAP.PENDING;
  const { Icon } = meta;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide",
        meta.className,
      )}
    >
      <Icon className="h-3 w-3" />
      {meta.label}
    </span>
  );
}

// ============================================================
// 🛒 ORDER CARD
// ============================================================
export function OrderCard({
  order,
  variant = "full",
  showActions = true,
  className,
  onClick,
}: OrderCardProps) {
  const { hapticSelection } = useTelegram();

  const itemsCount = order.items.reduce((acc, it) => acc + it.quantity, 0);
  const firstItem = order.items[0];
  const extraItems = order.items.length - 1;

  const handleClick = () => {
    hapticSelection();
    onClick?.(order);
  };

  const content = (
    <div
      className={cn(
        "group relative flex flex-col gap-3 rounded-2xl border border-border bg-card p-3.5",
        "transition-all duration-200",
        "hover:border-primary/40 hover:shadow-lg hover:shadow-primary/5",
        className,
      )}
    >
      {/* ---------- Cabeçalho ---------- */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-secondary/60">
            <ShoppingBag className="h-4 w-4 text-foreground/70" />
          </div>
          <div>
            <div className="text-sm font-semibold">
              Pedido <span className="font-mono">#{order.id}</span>
            </div>
            <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
              <Calendar className="h-3 w-3" />
              {formatDate(order.createdAt, { withTime: true })}
            </div>
          </div>
        </div>

        <StatusBadge status={order.status} />
      </div>

      {/* ---------- Produto(s) ---------- */}
      {variant === "full" && firstItem && (
        <div className="flex items-center justify-between gap-3 rounded-xl bg-secondary/40 p-2.5">
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">
              {firstItem.productName}
            </div>
            <div className="mt-0.5 text-xs text-muted-foreground">
              {firstItem.quantity}x · {formatBRL(firstItem.unitPrice)}
            </div>
          </div>
          {extraItems > 0 && (
            <span className="shrink-0 rounded-full bg-background/80 px-2 py-0.5 text-[10px] font-bold text-muted-foreground">
              +{extraItems}
            </span>
          )}
        </div>
      )}

      {/* ---------- Rodapé ---------- */}
      <div className="flex items-center justify-between border-t border-border/60 pt-3">
        <div className="flex flex-col">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Total
          </span>
          <span className="font-mono text-base font-bold text-primary">
            {formatBRL(order.finalTotal)}
          </span>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">
            {itemsCount} {itemsCount === 1 ? "item" : "itens"}
          </span>
          {showActions && (
            <ChevronRight className="h-4 w-4 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
          )}
        </div>
      </div>
    </div>
  );

  // Se tem onClick customizado, usa div + onClick
  if (onClick) {
    return (
      <button
        type="button"
        onClick={handleClick}
        className="block w-full text-left"
      >
        {content}
      </button>
    );
  }

  // Caso contrário, navega para o pedido
  return (
    <Link
      href={`/orders/${order.id}`}
      onClick={handleClick}
      className="block text-left"
    >
      {content}
    </Link>
  );
}

// ============================================================
// 📦 ORDER LIST (com estado vazio)
// ============================================================
interface OrderListProps {
  orders: Order[];
  emptyState?: React.ReactNode;
  className?: string;
}

export function OrderList({
  orders,
  emptyState,
  className,
}: OrderListProps) {
  if (orders.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center">
        {emptyState ?? (
          <>
            <div className="flex h-16 w-16 items-center justify-center rounded-full bg-secondary/60">
              <ShoppingBag className="h-7 w-7 text-muted-foreground" />
            </div>
            <p className="text-sm font-medium">Nenhum pedido ainda</p>
            <p className="text-xs text-muted-foreground">
              Quando você fizer uma compra, ela aparecerá aqui.
            </p>
          </>
        )}
      </div>
    );
  }

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {orders.map((order) => (
        <OrderCard key={order.id} order={order} />
      ))}
    </div>
  );
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export { StatusBadge, STATUS_MAP };
export default OrderCard;
