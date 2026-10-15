/**
 * Larizinha Store — WebApp Next.js
 * Catálogo — grade de produtos com filtro por categoria, busca e paginação.
 */
"use client";

import * as React from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Package, Search, SlidersHorizontal, X } from "lucide-react";

import { Topbar } from "@/components/topbar";
import { ProductCard } from "@/components/product-card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SkeletonProductGrid } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/loading";
import { useCategories, useProducts, type ProductsFilters } from "@/hooks/useApi";
import { useTelegram } from "@/components/providers";
import { cn } from "@/lib/utils";

// ============================================================
// 🏪 CATALOG PAGE
// ============================================================
export default function CatalogPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { hapticSelection, hapticImpact } = useTelegram();

  // ---------- Estado dos filtros ----------
  const initialCategory = searchParams?.get("categoryId");
  const [categoryId, setCategoryId] = React.useState<number | null>(
    initialCategory ? Number(initialCategory) : null,
  );
  const [search, setSearch] = React.useState("");
  const [searchInput, setSearchInput] = React.useState("");
  const [page, setPage] = React.useState(1);
  const [items, setItems] = React.useState<Awaited<ReturnType<typeof useProducts>>["data"] extends infer T ? (T extends Array<infer U> ? U[] : never) : never[]>([]);
  const [hasMore, setHasMore] = React.useState(true);
  const [loadingMore, setLoadingMore] = React.useState(false);

  const perPage = 20;

  // ---------- Debounce da busca ----------
  React.useEffect(() => {
    const t = setTimeout(() => {
      setSearch(searchInput.trim());
      setPage(1);
    }, 400);
    return () => clearTimeout(t);
  }, [searchInput]);

  // ---------- Filtros ativos ----------
  const filters: ProductsFilters = React.useMemo(
    () => ({
      categoryId: categoryId ?? undefined,
      search: search || undefined,
      page,
      perPage,
    }),
    [categoryId, search, page],
  );

  const { data, isLoading, isValidating, error } = useProducts(filters);
  const { data: categories, isLoading: catsLoading } = useCategories();

  // ---------- Acumula páginas ----------
  React.useEffect(() => {
    if (!data) return;

    if (page === 1) {
      setItems(data as never[]);
      setHasMore(data.length === perPage);
    } else {
      setItems((prev) => [...prev, ...(data as never[])]);
      setHasMore(data.length === perPage);
    }
    setLoadingMore(false);
  }, [data, page]);

  // ---------- Reset ao trocar filtro ----------
  React.useEffect(() => {
    setPage(1);
    setHasMore(true);
  }, [categoryId, search]);

  // ---------- Handlers ----------
  const handleCategory = (id: number | null) => {
    hapticSelection();
    setCategoryId(id);
  };

  const handleClearSearch = () => {
    hapticImpact("light");
    setSearchInput("");
    setSearch("");
  };

  const handleLoadMore = () => {
    if (loadingMore || !hasMore) return;
    hapticImpact("light");
    setLoadingMore(true);
    setPage((p) => p + 1);
  };

  const isFirstLoading = isLoading && page === 1 && items.length === 0;
  const isEmpty = !isLoading && !error && items.length === 0;

  return (
    <>
      <Topbar title="Catálogo" showBack />

      <main className="px-4 pb-28 pt-3">
        {/* ---------- Busca ---------- */}
        <div className="flex items-center gap-2">
          <Input
            placeholder="Buscar produtos..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            leftIcon={<Search className="h-4 w-4" />}
            rightIcon={
              searchInput ? (
                <button
                  type="button"
                  onClick={handleClearSearch}
                  aria-label="Limpar busca"
                  className="pointer-events-auto"
                >
                  <X className="h-4 w-4" />
                </button>
              ) : undefined
            }
            className="flex-1"
          />
        </div>

        {/* ---------- Categorias (chips) ---------- */}
        <div className="mt-3 -mx-4 flex gap-2 overflow-x-auto px-4 pb-1 scrollbar-hide">
          <CategoryChip
            active={categoryId === null}
            label="Todos"
            emoji="✨"
            onClick={() => handleCategory(null)}
          />

          {catsLoading
            ? Array.from({ length: 3 }).map((_, i) => (
                <div
                  key={i}
                  className="h-8 w-20 shrink-0 animate-pulse rounded-full bg-secondary/60"
                />
              ))
            : categories?.map((cat) => (
                <CategoryChip
                  key={cat.id}
                  active={categoryId === cat.id}
                  label={cat.name}
                  emoji={cat.emoji || "📦"}
                  onClick={() => handleCategory(cat.id)}
                />
              ))}
        </div>

        {/* ---------- Header da lista ---------- */}
        <div className="mt-4 flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <SlidersHorizontal className="h-3.5 w-3.5" />
            <span>
              {isFirstLoading
                ? "Carregando..."
                : `${items.length} ${items.length === 1 ? "produto" : "produtos"}`}
            </span>
            {search && (
              <span className="rounded-full bg-secondary px-2 py-0.5 text-[10px] font-medium">
                "{search}"
              </span>
            )}
          </div>
        </div>

        {/* ---------- Grade ---------- */}
        <div className="mt-3">
          {isFirstLoading ? (
            <SkeletonProductGrid count={6} />
          ) : error ? (
            <ErrorState
              title="Erro ao carregar"
              onRetry={() => window.location.reload()}
            />
          ) : isEmpty ? (
            <EmptyState
              icon={<Package className="h-7 w-7" />}
              title="Nenhum produto encontrado"
              description={
                search
                  ? "Tente outra busca ou explore o catálogo completo."
                  : "Nenhum produto disponível no momento."
              }
              action={
                search ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={handleClearSearch}
                  >
                    Limpar busca
                  </Button>
                ) : null
              }
            />
          ) : (
            <>
              <div className="grid grid-cols-2 gap-3">
                {items.map((p) => (
                  <ProductCard key={p.id} product={p} />
                ))}
              </div>

              {/* ---------- Carregar mais ---------- */}
              {hasMore && (
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

              {!hasMore && items.length > 0 && (
                <p className="mt-4 text-center text-xs text-muted-foreground">
                  — Fim do catálogo —
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
// 🏷️ CATEGORY CHIP
// ============================================================
function CategoryChip({
  active,
  label,
  emoji,
  onClick,
}: {
  active: boolean;
  label: string;
  emoji: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium",
        "transition-all active:scale-95",
        active
          ? "border-primary bg-primary/15 text-primary"
          : "border-border bg-card text-foreground/70 hover:border-primary/40",
      )}
    >
      <span className="text-sm leading-none">{emoji}</span>
      {label}
    </button>
  );
}
