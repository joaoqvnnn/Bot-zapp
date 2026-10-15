/**
 * Larizinha Store — WebApp Next.js
 * Meus Pedidos — listagem com filtro por status e detalhes.
 */
"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowRight,
  CheckCircle2,
  Clock,
  ListFilter,
  Package,
  ShoppingBag,
  XCircle,
} from "lucide-react";

import { Topbar } from "@/components/topbar";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { SkeletonList } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/loading";
import { useOrders } from "@/hooks/useApi";
import { useTelegram } from "@/components/providers";
import { cn, formatBRL, formatDate } from "@/lib/utils";
import type { Order } from "@/lib/api";

// ============================================================
// 🗂️ FILTROS
// ============================================================
type FilterKey = "all" | "delivered" | "pending" | "canceled";

const FILTERS: { key: FilterKey; label: string; Icon: React.ComponentType<{ className?: string }> }[] = [
  { key: "all", label: "Todos", Icon: ListFilter },
  { key: "delivered", label: "Entregues", Icon: CheckCircle2 },
  { key: "pending", label: "Pendentes", Icon: Clock },
  { key: "canceled", label: "Cancelados", Icon: XCircle },
];

// ============================================================
// 🛒 ORDERS PAGE
// ============================================================
export default function OrdersPage() {
  const router = useRouter();
  const { hapticSelection } = useTelegram();

  const [filter, setFilter] = React.useState<FilterKey>("all");
  const [page, setPage] = React.useState(1);
  const perPage = 20;

  const { data, isLoading, isValidating, error, mutate } = useOrders(page, perPage);

  const [items, setItems] = React.useState<Order[]>([]);
  const [hasMore, setHasMore] = React.useState(true);
  const [loadingMore, setLoadingMore] = React.useState(false);

  // ---------- Acumula páginas ----------
  React.useEffect(() => {
    if (!data) return;
    if (page === 1) {
      setItems(data);
      setHasMore(data.length === perPage);
    } else {
      setItems((prev) => [...prev, ...data]);
      setHasMore(data.length === perPage);
    }
    setLoadingMore(false);
  }, [data, page]);

  // ---------- Filtro local ----------
  const filtered = React.useMemo(() => {
    if (filter === "all") return items;

    return items.filter((o) => {
      const s = o.status.toUpperCase();
      if (filter === "delivered") return s === "DELIVERED" || s === "PAID";
      if (filter === "pending") return s === "PENDING";
      if (filter === "canceled")
        return s === "CANCELED" || s === "REFUNDED" || s === "EXPIRED";
      return true;
    });
  }, [items, filter]);

  // ---------- Handlers ----------
  const handleFilter = (key: FilterKey) => {
    hapticSelection();
    setFilter(key);
  };

  const handleLoadMore = () => {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    setPage((p) => p + 1);
  };

  const handleGoToCatalog = () => {
    router.push("/catalog");
  };

  const isFirstLoading = isLoading && page === 1 && items.length === 0;
  const isEmpty = !isLoading && !error && filtered.length === 0;

  return (
    <>
      <Topbar title="Meus Pedidos" />

      <main className="px-4 pb-28 pt-3">
        {/* =====================================================
            🗂️ FILTROS
        ===================================================== */}
        <div className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-1 scrollbar-hide">
          {FILTERS.map(({ key, label, Icon }) => {
            const active = filter === key;
            return (
              <button
                key={key}
                type="button"
                onClick={() => handleFilter(key)}
                className={cn(
                  "flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium",
                  "transition-all active:scale-95",
                  active
                    ? "border-primary bg-primary/15 text-primary"
                    : "border-border bg-card text-foreground/70 hover:border-primary/40",
                )}
              >
                <Icon className="h-3.5 w-3.5" />
                {label}
              </button>
            );
          })}
        </div>

        {/* =====================================================
            📊 CONTADOR
        ===================================================== */}
        <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
          <span>
            {isFirstLoading
              ? "Carregando..."
              : `${filtered.length} ${
                  filtered.length === 1 ? "pedido" : "pedidos"
                }`}
          </span>
          <button
            type="button"
            onClick={() => mutate()}
            className="text-primary hover:underline"
          >
            Atualizar
          </button>
        </div>

        {/* =====================================================
            📦 LISTA
        ===================================================== */}
        <div className="mt-3">
          {isFirstLoading ? (
            <SkeletonList count={4} />
          ) : error ? (
            <ErrorState
              title="Erro ao carregar pedidos"
              onRetry={() => mutate()}
            />
          ) : isEmpty ? (
            <EmptyState
              icon={<ShoppingBag className="h-7 w-7" />}
              title={
                filter === "all"
                  ? "Nenhum pedido ainda"
                  : "Nenhum pedido neste filtro"
              }
              description={
                filter === "all"
                  ? "Quando você fizer uma compra, ela aparecerá aqui."
                  : "Tente selecionar outro filtro."
              }
              action={
                filter === "all" ? (
                  <Button
                    variant="default"
                    size="sm"
                    onClick={handleGoToCatalog}
                    leftIcon={<Package className="h-3.5 w-3.5" />}
                  >
                    Ver catálogo
                  </Button>
                ) : null
              }
            />
          ) : (
            <>
              <div className="flex flex-col gap-3">
                {filtered.map((order) => (
                  <OrderRow key={order.id} order={order} />
                ))}
              </div>

              {/* Carregar mais */}
              {hasMore && filter === "all" && (
                <div className="mt-4 flex justify-center">
                  <Button
                    variant="secondary"
                    size="sm"
                    loading={loadingMore || (isValidating && page > 1)}
                    onClick={handleLoadMore}
                  >
                    Carregar mais
                  </Button>
                </div>
              )}

              {!hasMore && filtered.length > 0 && (
                <p className="mt-4 text-center text-xs text-muted-foreground">
                  — Fim da lista —
                </p>
              )}
            </>
          )}
        </div>
      </main>
    </>
  );
}

// ============================================================
// 📦 ORDER ROW
// ============================================================
function OrderRow({ order }: { order: Order }) {
  const status = order.status.toUpperCase();
  const itemsCount = order.items.reduce((a, i) => a + i.quantity, 0);
  const firstItem = order.items[0];

  const statusMeta = getStatusMeta(status);

  return (
    <Link
      href={`/orders/${order.id}`}
      className={cn(
        "group flex flex-col gap-3 rounded-2xl border border-border bg-card p-3.5",
        "transition-all duration-200",
        "hover:border-primary/40 hover:shadow-lg hover:shadow-primary/5",
        "active:scale-[0.99]",
      )}
    >
      {/* Cabeçalho */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-secondary/60">
            <ShoppingBag className="h-4 w-4 text-foreground/70" />
          </div>
          <div>
            <div className="text-sm font-semibold">
              Pedido <span className="font-mono">#{order.id}</span>
            </div>
            <div className="text-[11px] text-muted-foreground">
              {formatDate(order.createdAt, { withTime: true })}
            </div>
          </div>
        </div>

        <span
          className={cn(
            "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide",
            statusMeta.className,
          )}
        >
          {statusMeta.label}
        </span>
      </div>

      {/* Item principal */}
      {firstItem && (
        <div className="flex items-center justify-between gap-3 rounded-xl bg-secondary/40 p-2.5">
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">
              {firstItem.productName}
            </div>
            <div className="mt-0.5 text-xs text-muted-foreground">
              {firstItem.quantity}x · {formatBRL(firstItem.unitPrice)}
            </div>
          </div>
          {order.items.length > 1 && (
            <span className="shrink-0 rounded-full bg-background/80 px-2 py-0.5 text-[10px] font-bold text-muted-foreground">
              +{order.items.length - 1}
            </span>
          )}
        </div>
      )}

      {/* Rodapé */}
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
          <ArrowRight className="h-4 w-4 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
        </div>
      </div>
    </Link>
  );
}

// ============================================================
// 🏷️ STATUS META
// ============================================================
function getStatusMeta(status: string): {
  label: string;
  className: string;
} {
  const map: Record<string, { label: string; className: string }> = {
    PENDING: {
      label: "Pendente",
      className:
        "text-yellow-500 bg-yellow-500/10 border-yellow-500/30",
    },
    PAID: {
      label: "Pago",
      className:
        "text-emerald-500 bg-emerald-500/10 border-emerald-500/30",
    },
    DELIVERED: {
      label: "Entregue",
      className:
        "text-emerald-500 bg-emerald-500/10 border-emerald-500/30",
    },
    CANCELED: {
      label: "Cancelado",
      className:
        "text-muted-foreground bg-secondary/60 border-border",
    },
    REFUNDED: {
      label: "Reembolsado",
      className:
        "text-blue-500 bg-blue-500/10 border-blue-500/30",
    },
    EXPIRED: {
      label: "Expirado",
      className:
        "text-orange-500 bg-orange-500/10 border-orange-500/30",
    },
  };

  return (
    map[status] ?? {
      label: status,
      className:
        "text-muted-foreground bg-secondary/60 border-border",
    }
  );
}
