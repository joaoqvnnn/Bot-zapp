/**
 * Larizinha Store — WebApp Next.js
 * Home (Loja) — tela principal do WebApp.
 */
"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowRight,
  Flame,
  Gift,
  Headphones,
  Package,
  Percent,
  Sparkles,
  Users,
  Wallet,
  Zap,
} from "lucide-react";

import { Topbar } from "@/components/topbar";
import { ProductCard } from "@/components/product-card";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { SkeletonProductGrid } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/loading";
import { useCategories, useMe, useProducts } from "@/hooks/useApi";
import { useTelegram } from "@/components/providers";
import { cn, formatBRL } from "@/lib/utils";

// ============================================================
// 🏠 HOME
// ============================================================
export default function HomePage() {
  const router = useRouter();
  const { user: tgUser, hapticImpact } = useTelegram();
  const { data: me, isLoading: meLoading, error: meError } = useMe();
  const { data: categories, isLoading: catsLoading } = useCategories();
  const { data: featured, isLoading: featLoading, error: featError } =
    useProducts({ perPage: 8 });

  const firstName =
    me?.firstName || tgUser?.first_name || "Cliente";

  const handleQuickAction = React.useCallback(
    (path: string) => {
      hapticImpact("light");
      router.push(path);
    },
    [router, hapticImpact],
  );

  // Erro crítico do usuário
  if (meError && !meLoading) {
    return (
      <>
        <Topbar />
        <ErrorState
          title="Não foi possível carregar"
          message="Abra o WebApp novamente pelo Telegram."
          onRetry={() => window.location.reload()}
        />
      </>
    );
  }

  return (
    <>
      <Topbar />

      <main className="px-4 pb-28 pt-3">
        {/* =====================================================
            👋 BOAS-VINDAS
        ===================================================== */}
        <section className="lariz-animate-in">
          <div className="flex items-baseline justify-between">
            <div>
              <p className="text-xs uppercase tracking-wide text-muted-foreground">
                Olá,
              </p>
              <h1 className="text-xl font-bold leading-tight">
                {firstName} <span className="ml-0.5">👋</span>
              </h1>
            </div>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            Encontre seu streaming favorito com entrega automática.
          </p>
        </section>

        {/* =====================================================
            💰 CARD DE SALDO
        ===================================================== */}
        <section className="mt-4">
          <Card className="relative overflow-hidden border-none bg-gradient-to-br from-primary/15 via-primary/5 to-accent/10 p-4">
            {/* brilho decorativo */}
            <div className="pointer-events-none absolute -right-6 -top-6 h-24 w-24 rounded-full bg-primary/20 blur-3xl" />

            <div className="relative flex items-center justify-between">
              <div>
                <div className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  <Wallet className="h-3.5 w-3.5" />
                  Saldo disponível
                </div>
                <div className="mt-1 font-mono text-2xl font-bold tabular-nums text-primary">
                  {meLoading ? "—" : formatBRL(me?.balance ?? 0)}
                </div>
              </div>

              <Button
                variant="default"
                size="sm"
                onClick={() => handleQuickAction("/wallet")}
                rightIcon={<ArrowRight className="h-3.5 w-3.5" />}
              >
                Recarregar
              </Button>
            </div>
          </Card>
        </section>

        {/* =====================================================
            ⚡ ATALHOS RÁPIDOS
        ===================================================== */}
        <section className="mt-4 grid grid-cols-4 gap-2">
          <QuickAction
            icon={<Wallet className="h-5 w-5" />}
            label="Carteira"
            onClick={() => handleQuickAction("/wallet")}
          />
          <QuickAction
            icon={<Package className="h-5 w-5" />}
            label="Pedidos"
            onClick={() => handleQuickAction("/orders")}
          />
          <QuickAction
            icon={<Users className="h-5 w-5" />}
            label="Afiliados"
            onClick={() => handleQuickAction("/affiliate")}
          />
          <QuickAction
            icon={<Headphones className="h-5 w-5" />}
            label="Suporte"
            onClick={() => handleQuickAction("/support")}
          />
        </section>

        {/* =====================================================
            🏷️ CATEGORIAS
        ===================================================== */}
        <section className="mt-6">
          <SectionHeader
            title="Categorias"
            icon={<Sparkles className="h-4 w-4 text-primary" />}
            actionLabel="Ver tudo"
            onAction={() => handleQuickAction("/catalog")}
          />

          <div className="mt-3 flex gap-2 overflow-x-auto pb-1 scrollbar-hide">
            {catsLoading ? (
              <>
                {Array.from({ length: 5 }).map((_, i) => (
                  <div
                    key={i}
                    className="h-16 w-24 shrink-0 animate-pulse rounded-2xl bg-secondary/60"
                  />
                ))}
              </>
            ) : categories && categories.length > 0 ? (
              categories.map((cat) => (
                <button
                  key={cat.id}
                  type="button"
                  onClick={() =>
                    handleQuickAction(`/catalog?categoryId=${cat.id}`)
                  }
                  className={cn(
                    "flex h-16 w-24 shrink-0 flex-col items-center justify-center gap-1 rounded-2xl",
                    "border border-border bg-card px-2 py-2 text-center",
                    "transition-all hover:border-primary/40 active:scale-95",
                  )}
                >
                  <span className="text-xl leading-none">
                    {cat.emoji || "📦"}
                  </span>
                  <span className="truncate text-[11px] font-medium leading-tight">
                    {cat.name}
                  </span>
                </button>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">
                Nenhuma categoria disponível.
              </p>
            )}
          </div>
        </section>

        {/* =====================================================
            🔥 DESTAQUES
        ===================================================== */}
        <section className="mt-6">
          <SectionHeader
            title="Destaques"
            icon={<Flame className="h-4 w-4 text-primary" />}
            actionLabel="Catálogo"
            onAction={() => handleQuickAction("/catalog")}
          />

          <div className="mt-3">
            {featLoading ? (
              <SkeletonProductGrid count={4} />
            ) : featError ? (
              <ErrorState
                title="Erro ao carregar produtos"
                onRetry={() => window.location.reload()}
              />
            ) : featured && featured.length > 0 ? (
              <div className="grid grid-cols-2 gap-3">
                {featured.map((p) => (
                  <ProductCard key={p.id} product={p} />
                ))}
              </div>
            ) : (
              <Card className="p-4 text-center text-sm text-muted-foreground">
                Nenhum produto disponível no momento.
              </Card>
            )}
          </div>
        </section>

        {/* =====================================================
            🎁 BANNER PROMO
        ===================================================== */}
        <section className="mt-6">
          <Card className="relative overflow-hidden border-primary/30 bg-gradient-to-r from-primary/10 to-accent/10 p-4">
            <div className="pointer-events-none absolute -right-4 -top-4 h-20 w-20 rounded-full bg-primary/20 blur-2xl" />

            <div className="relative flex items-start gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary/15 text-primary">
                <Percent className="h-5 w-5" />
              </div>
              <div className="flex-1">
                <div className="flex items-center gap-1.5 text-sm font-semibold">
                  <Zap className="h-3.5 w-3.5 text-primary" />
                  Programa de Afiliados
                </div>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Indique amigos e ganhe comissão em cada recarga.
                </p>
              </div>
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={() => handleQuickAction("/affiliate")}
                aria-label="Abrir afiliados"
              >
                <ArrowRight className="h-4 w-4" />
              </Button>
            </div>
          </Card>
        </section>

        {/* =====================================================
            💠 CTA FINAL
        ===================================================== */}
        <section className="mt-6">
          <Button
            variant="default"
            size="lg"
            fullWidth
            onClick={() => handleQuickAction("/catalog")}
            leftIcon={<Gift className="h-4 w-4" />}
          >
            Ver catálogo completo
          </Button>
        </section>

        {/* =====================================================
            📌 FOOTER
        ===================================================== */}
        <footer className="mt-6 pb-2 text-center text-[11px] text-muted-foreground">
          <p>© {new Date().getFullYear()} Larizinha Store</p>
          <p className="mt-0.5">Entrega automática · 24/7</p>
        </footer>
      </main>
    </>
  );
}

// ============================================================
// ⚡ QUICK ACTION
// ============================================================
function QuickAction({
  icon,
  label,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex flex-col items-center gap-1.5 rounded-xl border border-border bg-card",
        "px-1 py-2.5 text-[11px] font-medium text-foreground/80",
        "transition-all hover:border-primary/40 hover:text-foreground",
        "active:scale-95",
      )}
    >
      <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-secondary/60 text-foreground/80">
        {icon}
      </span>
      {label}
    </button>
  );
}

// ============================================================
// 📌 SECTION HEADER
// ============================================================
function SectionHeader({
  title,
  icon,
  actionLabel,
  onAction,
}: {
  title: string;
  icon?: React.ReactNode;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className="flex items-center justify-between">
      <div className="flex items-center gap-1.5">
        {icon}
        <h2 className="text-sm font-semibold uppercase tracking-wide">
          {title}
        </h2>
      </div>
      {actionLabel && onAction && (
        <button
          type="button"
          onClick={onAction}
          className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
        >
          {actionLabel}
          <ArrowRight className="h-3 w-3" />
        </button>
      )}
    </div>
  );
}
