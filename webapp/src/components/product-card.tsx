/**
 * Larizinha Store — WebApp Next.js
 * ProductCard — card de produto (usado na Home e no Catálogo).
 */
"use client";

import * as React from "react";
import Image from "next/image";
import Link from "next/link";
import { Flame, Package, Star, Zap } from "lucide-react";

import { cn, formatBRL } from "@/lib/utils";
import type { ProductListItem } from "@/lib/api";
import { useTelegram } from "@/components/providers";

// ============================================================
// 🎛️ PROPS
// ============================================================
interface ProductCardProps {
  product: ProductListItem;
  variant?: "grid" | "list";
  className?: string;
  onClick?: (product: ProductListItem) => void;
}

// ============================================================
// 🖼️ IMAGEM COM FALLBACK
// ============================================================
function ProductImage({
  src,
  alt,
  variant,
}: {
  src: string | null;
  alt: string;
  variant: "grid" | "list";
}) {
  const [errored, setErrored] = React.useState(false);

  const wrapperClass =
    variant === "grid"
      ? "aspect-video w-full overflow-hidden rounded-xl bg-secondary/40"
      : "h-16 w-16 shrink-0 overflow-hidden rounded-lg bg-secondary/40";

  if (!src || errored) {
    return (
      <div
        className={cn(
          wrapperClass,
          "flex items-center justify-center text-muted-foreground",
        )}
      >
        <Package className={variant === "grid" ? "h-8 w-8" : "h-6 w-6"} />
      </div>
    );
  }

  return (
    <div className={wrapperClass}>
      <Image
        src={src}
        alt={alt}
        width={variant === "grid" ? 400 : 64}
        height={variant === "grid" ? 240 : 64}
        className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-105"
        onError={() => setErrored(true)}
        unoptimized
        priority={false}
      />
    </div>
  );
}

// ============================================================
// 🏷️ BADGES
// ============================================================
function Badge({
  children,
  variant = "default",
  className,
}: {
  children: React.ReactNode;
  variant?: "default" | "success" | "warn" | "error" | "accent";
  className?: string;
}) {
  const variants = {
    default: "bg-secondary text-foreground/80 border-border",
    success: "bg-emerald-500/15 text-emerald-500 border-emerald-500/30",
    warn: "bg-yellow-500/15 text-yellow-500 border-yellow-500/30",
    error: "bg-destructive/15 text-destructive border-destructive/30",
    accent: "bg-primary/15 text-primary border-primary/30",
  };

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide",
        variants[variant],
        className,
      )}
    >
      {children}
    </span>
  );
}

// ============================================================
// 🃏 PRODUCT CARD
// ============================================================
export function ProductCard({
  product,
  variant = "grid",
  className,
  onClick,
}: ProductCardProps) {
  const { hapticSelection, hapticImpact } = useTelegram();

  // ---------- Preço efetivo ----------
  const price = product.pricePromo ?? product.price;
  const hasPromo =
    product.pricePromo !== null &&
    product.pricePromo !== undefined &&
    product.pricePromo > 0 &&
    product.pricePromo < product.price;

  const discountPercent = hasPromo
    ? Math.round(((product.price - price) / product.price) * 100)
    : 0;

  // ---------- Estoque ----------
  const isSoldOut = product.soldOut || (!product.unlimited && product.stock <= 0);
  const isLowStock =
    !product.unlimited && !isSoldOut && product.stock > 0 && product.stock <= 5;

  // ---------- Handlers ----------
  const handleClick = (e: React.MouseEvent) => {
    if (isSoldOut) {
      e.preventDefault();
      hapticImpact("heavy");
      return;
    }
    hapticSelection();
    onClick?.(product);
  };

  // ---------- Wrapper ----------
  const wrapperClass = cn(
    "group relative overflow-hidden rounded-2xl border border-border bg-card",
    "transition-all duration-200",
    !isSoldOut && "hover:border-primary/40 hover:shadow-lg hover:shadow-primary/5 active:scale-[0.99]",
    isSoldOut && "opacity-60",
    variant === "grid" ? "flex flex-col p-2.5" : "flex items-center gap-3 p-3",
    className,
  );

  return (
    <Link
      href={isSoldOut ? "#" : `/product/${product.id}`}
      onClick={handleClick}
      className={wrapperClass}
      aria-disabled={isSoldOut}
    >
      {/* ---------- GRID ---------- */}
      {variant === "grid" && (
        <>
          <div className="relative">
            <ProductImage
              src={product.imageWebapp}
              alt={product.name}
              variant="grid"
            />

            {/* Badges flutuantes */}
            <div className="absolute left-2 top-2 flex flex-col gap-1">
              {hasPromo && (
                <Badge variant="accent">
                  <Zap className="h-3 w-3" />
                  -{discountPercent}%
                </Badge>
              )}
              {product.featured && (
                <Badge variant="warn">
                  <Star className="h-3 w-3" />
                  Destaque
                </Badge>
              )}
            </div>

            {/* Sold out overlay */}
            {isSoldOut && (
              <div className="absolute inset-0 flex items-center justify-center rounded-xl bg-background/70 backdrop-blur-sm">
                <span className="rounded-full border border-destructive/40 bg-destructive/20 px-3 py-1 text-xs font-bold uppercase tracking-wide text-destructive">
                  Esgotado
                </span>
              </div>
            )}
          </div>

          {/* Título + subtítulo */}
          <div className="mt-2 flex flex-1 flex-col">
            <h3 className="line-clamp-2 text-sm font-semibold leading-tight text-foreground">
              {product.name}
            </h3>
            {product.subtitle ? (
              <p className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">
                {product.subtitle}
              </p>
            ) : null}

            {/* Preço + estoque */}
            <div className="mt-auto pt-2">
              <div className="flex items-baseline gap-1.5">
                {hasPromo && (
                  <span className="text-xs text-muted-foreground line-through">
                    {formatBRL(product.price)}
                  </span>
                )}
                <span className="font-mono text-base font-bold text-primary">
                  {formatBRL(price)}
                </span>
              </div>

              {!isSoldOut && (isLowStock || product.unlimited) && (
                <div className="mt-1">
                  {isLowStock ? (
                    <Badge variant="warn">
                      <Flame className="h-3 w-3" />
                      Últimas {product.stock} un.
                    </Badge>
                  ) : product.unlimited ? (
                    <Badge variant="success">Estoque ilimitado</Badge>
                  ) : null}
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {/* ---------- LIST ---------- */}
      {variant === "list" && (
        <>
          <ProductImage
            src={product.imageWebapp}
            alt={product.name}
            variant="list"
          />

          <div className="flex min-w-0 flex-1 items-center justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <h3 className="truncate text-sm font-semibold">
                  {product.name}
                </h3>
                {hasPromo && (
                  <Badge variant="accent">
                    -{discountPercent}%
                  </Badge>
                )}
              </div>
              {product.subtitle ? (
                <p className="mt-0.5 truncate text-xs text-muted-foreground">
                  {product.subtitle}
                </p>
              ) : null}

              {isSoldOut ? (
                <Badge variant="error" className="mt-1">
                  Esgotado
                </Badge>
              ) : isLowStock ? (
                <Badge variant="warn" className="mt-1">
                  Últimas {product.stock}
                </Badge>
              ) : null}
            </div>

            <div className="text-right">
              {hasPromo && (
                <div className="text-xs text-muted-foreground line-through">
                  {formatBRL(product.price)}
                </div>
              )}
              <div className="font-mono text-sm font-bold text-primary">
                {formatBRL(price)}
              </div>
            </div>
          </div>
        </>
      )}
    </Link>
  );
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export default ProductCard;
