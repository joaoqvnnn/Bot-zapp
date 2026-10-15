/**
 * Larizinha Store — WebApp Next.js
 * Detalhe do Produto — imagem, descrição, preço, estoque e compra.
 */
"use client";

import * as React from "react";
import { useParams, useRouter } from "next/navigation";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronUp,
  Minus,
  Package,
  Plus,
  Shield,
  ShoppingCart,
  TrendingUp,
  Eye,
  Zap,
} from "lucide-react";

import { Topbar } from "@/components/topbar";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { PageLoader, ErrorState } from "@/components/loading";
import { useProduct, useMe, useBuy } from "@/hooks/useApi";
import { useTelegram } from "@/components/providers";
import { cn, formatBRL } from "@/lib/utils";
import { toast } from "sonner";

// ============================================================
// 📦 PRODUCT PAGE
// ============================================================
export default function ProductPage() {
  const params = useParams();
  const router = useRouter();
  const { hapticImpact, hapticSelection, haptic } = useTelegram();

  const productId = Number(params?.id || 0);

  const { data: me, mutate: mutateMe } = useMe();
  const { data: product, isLoading, error } = useProduct(productId);
  const { trigger: buy, isMutating: buying } = useBuy();

  const [quantity, setQuantity] = React.useState(1);
  const [showFullDescription, setShowFullDescription] = React.useState(false);

  // ---------- Preço efetivo ----------
  const price = product?.pricePromo ?? product?.price ?? 0;
  const hasPromo =
    product?.pricePromo != null &&
    product.pricePromo > 0 &&
    product.pricePromo < (product?.price ?? 0);
  const discountPercent =
    hasPromo && product?.price
      ? Math.round(((product.price - product.pricePromo!) / product.price) * 100)
      : 0;

  // ---------- Estoque ----------
  const isSoldOut =
    product?.soldOut || (!product?.unlimited && (product?.stock ?? 0) <= 0);
  const maxQuantity = product?.unlimited
    ? 99
    : Math.min(product?.stock ?? 1, 99);
  const isLowStock = !product?.unlimited && (product?.stock ?? 0) > 0 && (product?.stock ?? 0) <= 5;

  // ---------- Saldo suficiente? ----------
  const total = price * quantity;
  const balance = me?.balance ?? 0;
  const hasEnoughBalance = balance >= total;
  const missing = Math.max(0, total - balance);

  // ---------- Handlers ----------
  const handleQty = (delta: number) => {
    hapticSelection();
    setQuantity((q) => Math.max(1, Math.min(maxQuantity, q + delta)));
  };

  const handleBuy = async () => {
    if (!product || buying) return;
    if (isSoldOut) {
      haptic("error");
      return;
    }

    hapticImpact("medium");

    const res = await buy({
      productId: product.id,
      quantity,
      method: "BALANCE",
    });

    if (!res || !res.id) {
      haptic("error");
      toast.error("Não foi possível realizar a compra.");
      return;
    }

    haptic("success");
    toast.success("Compra realizada!");
    await mutateMe();
    router.push(`/orders/${res.id}`);
  };

  const handleRecharge = () => {
    hapticImpact("light");
    router.push("/wallet");
  };

  // ============================================================
  // ESTADOS
  // ============================================================
  if (isLoading) {
    return (
      <>
        <Topbar title="Produto" showBack showBalance={false} showAdd={false} />
        <PageLoader message="Carregando produto..." />
      </>
    );
  }

  if (error || !product) {
    return (
      <>
        <Topbar title="Produto" showBack showBalance={false} showAdd={false} />
        <ErrorState
          title="Produto não encontrado"
          message="Este produto pode ter sido removido."
          onRetry={() => router.push("/catalog")}
          retryLabel="Voltar ao catálogo"
        />
      </>
    );
  }

  // ============================================================
  // RENDER
  // ============================================================
  return (
    <>
      <Topbar title={product.name} showBack showBalance={false} showAdd={false} />

      <main className="pb-28">
        {/* ---------- Imagem principal ---------- */}
        <div className="relative aspect-video w-full overflow-hidden bg-secondary/40">
          {product.imageWebapp ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={product.imageWebapp}
              alt={product.name}
              className="h-full w-full object-cover"
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-muted-foreground">
              <Package className="h-12 w-12" />
            </div>
          )}

          {/* Promo badge */}
          {hasPromo && (
            <div className="absolute left-3 top-3 flex items-center gap-1 rounded-full bg-primary px-2.5 py-1 text-xs font-bold text-primary-foreground shadow-lg">
              <Zap className="h-3 w-3" />
              -{discountPercent}%
            </div>
          )}

          {/* Sold out overlay */}
          {isSoldOut && (
            <div className="absolute inset-0 flex items-center justify-center bg-background/70 backdrop-blur-sm">
              <span className="rounded-full border border-destructive/40 bg-destructive/20 px-4 py-1.5 text-sm font-bold uppercase tracking-wide text-destructive">
                Esgotado
              </span>
            </div>
          )}
        </div>

        <div className="px-4 pt-4">
          {/* ---------- Título + subtítulo ---------- */}
          <div>
            <h1 className="text-xl font-bold leading-tight">{product.name}</h1>
            {product.subtitle && (
              <p className="mt-1 text-sm text-muted-foreground">
                {product.subtitle}
              </p>
            )}
          </div>

          {/* ---------- Preço ---------- */}
          <div className="mt-3 flex items-baseline gap-2">
            {hasPromo && (
              <span className="text-sm text-muted-foreground line-through">
                {formatBRL(product.price)}
              </span>
            )}
            <span className="font-mono text-2xl font-bold text-primary">
              {formatBRL(price)}
            </span>
          </div>

          {/* ---------- Selos / Estatísticas ---------- */}
          <div className="mt-3 flex flex-wrap gap-1.5">
            {!isSoldOut &&
              (product.unlimited ? (
                <Pill icon={<Check className="h-3 w-3" />} variant="success">
                  Estoque ilimitado
                </Pill>
              ) : isLowStock ? (
                <Pill icon={<TrendingUp className="h-3 w-3" />} variant="warn">
                  Últimas {product.stock} unidades
                </Pill>
              ) : (
                <Pill icon={<Check className="h-3 w-3" />} variant="success">
                  {product.stock} em estoque
                </Pill>
              ))}

            {product.warrantyDays > 0 && (
              <Pill icon={<Shield className="h-3 w-3" />} variant="default">
                Garantia {product.warrantyDays}d
              </Pill>
            )}

            {product.soldCount > 0 && (
              <Pill icon={<Eye className="h-3 w-3" />} variant="default">
                {product.soldCount} vendidos
              </Pill>
            )}
          </div>

          {/* ---------- Quantidade ---------- */}
          {!isSoldOut && (
            <Card className="mt-4 flex items-center justify-between p-3">
              <div>
                <div className="text-sm font-medium">Quantidade</div>
                <div className="mt-0.5 text-xs text-muted-foreground">
                  Total: {formatBRL(total)}
                </div>
              </div>

              <div className="flex items-center gap-1 rounded-xl border border-border bg-secondary/40 p-1">
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => handleQty(-1)}
                  disabled={quantity <= 1}
                  aria-label="Diminuir quantidade"
                >
                  <Minus className="h-3.5 w-3.5" />
                </Button>
                <span className="min-w-[2.5rem] text-center font-mono text-base font-bold">
                  {quantity}
                </span>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => handleQty(+1)}
                  disabled={quantity >= maxQuantity}
                  aria-label="Aumentar quantidade"
                >
                  <Plus className="h-3.5 w-3.5" />
                </Button>
              </div>
            </Card>
          )}

          {/* ---------- Descrição ---------- */}
          {product.description && (
            <Card className="mt-4 p-3.5">
              <h2 className="text-sm font-semibold">Descrição</h2>
              <p
                className={cn(
                  "mt-1.5 whitespace-pre-line text-sm leading-relaxed text-foreground/80",
                  !showFullDescription && "line-clamp-4",
                )}
              >
                {product.description}
              </p>

              {product.description.length > 180 && (
                <button
                  type="button"
                  onClick={() => {
                    hapticImpact("light");
                    setShowFullDescription((s) => !s);
                  }}
                  className="mt-1 flex items-center gap-1 text-xs font-medium text-primary"
                >
                  {showFullDescription ? (
                    <>
                      Mostrar menos <ChevronUp className="h-3 w-3" />
                    </>
                  ) : (
                    <>
                      Mostrar mais <ChevronDown className="h-3 w-3" />
                    </>
                  )}
                </button>
              )}
            </Card>
          )}

          {/* ---------- Notas / Garantia ---------- */}
          {(product.noteProduct || product.noteActivation) && (
            <Card className="mt-4 p-3.5">
              {product.noteProduct && (
                <div>
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    Sobre o produto
                  </h3>
                  <p className="mt-1 whitespace-pre-line text-sm text-foreground/80">
                    {product.noteProduct}
                  </p>
                </div>
              )}

              {product.noteActivation && (
                <div
                  className={cn(
                    product.noteProduct && "mt-3 border-t border-border/60 pt-3",
                  )}
                >
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    Ativação
                  </h3>
                  <p className="mt-1 whitespace-pre-line text-sm text-foreground/80">
                    {product.noteActivation}
                  </p>
                </div>
              )}
            </Card>
          )}

          {/* ---------- Saldo insuficiente ---------- */}
          {!isSoldOut && !hasEnoughBalance && (
            <Card className="mt-4 border-yellow-500/30 bg-yellow-500/10 p-3.5">
              <div className="flex items-start gap-2.5">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-yellow-500" />
                <div className="flex-1">
                  <p className="text-sm font-semibold text-yellow-500">
                    Saldo insuficiente
                  </p>
                  <p className="mt-0.5 text-xs text-foreground/70">
                    Faltam {formatBRL(missing)} para completar a compra.
                  </p>
                </div>
                <Button variant="secondary" size="sm" onClick={handleRecharge}>
                  Recarregar
                </Button>
              </div>
            </Card>
          )}
        </div>
      </main>

      {/* ============================================================
          🛒 BARRA FIXA DE COMPRA
      ============================================================ */}
      <div
        className={cn(
          "fixed bottom-0 left-0 right-0 z-40 border-t border-border bg-card/95 backdrop-blur",
          "lariz-safe-bottom",
        )}
      >
        <div className="mx-auto flex max-w-md items-center gap-3 px-4 py-3">
          <div className="flex-1">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Total
            </div>
            <div className="font-mono text-lg font-bold text-primary">
              {formatBRL(total)}
            </div>
          </div>

          <Button
            variant={isSoldOut ? "secondary" : "default"}
            size="lg"
            loading={buying}
            disabled={isSoldOut || !hasEnoughBalance}
            onClick={hasEnoughBalance ? handleBuy : handleRecharge}
            leftIcon={
              hasEnoughBalance ? (
                <ShoppingCart className="h-4 w-4" />
              ) : (
                <AlertTriangle className="h-4 w-4" />
              )
            }
          >
            {isSoldOut
              ? "Esgotado"
              : hasEnoughBalance
                ? "Comprar"
                : "Recarregar"}
          </Button>
        </div>
      </div>
    </>
  );
}

// ============================================================
// 🏷️ PILL
// ============================================================
function Pill({
  icon,
  children,
  variant = "default",
}: {
  icon?: React.ReactNode;
  children: React.ReactNode;
  variant?: "default" | "success" | "warn" | "error";
}) {
  const variants = {
    default: "bg-secondary text-foreground/70 border-border",
    success: "bg-emerald-500/15 text-emerald-500 border-emerald-500/30",
    warn: "bg-yellow-500/15 text-yellow-500 border-yellow-500/30",
    error: "bg-destructive/15 text-destructive border-destructive/30",
  };

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
        variants[variant],
      )}
    >
      {icon}
      {children}
    </span>
  );
}
