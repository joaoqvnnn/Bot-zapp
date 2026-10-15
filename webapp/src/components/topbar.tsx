/**
 * Larizinha Store — WebApp Next.js
 * Topbar — cabeçalho fixo com saldo, avatar, botão voltar e ação de recarga.
 * Sincroniza com o Telegram BackButton quando disponível.
 */
"use client";

import * as React from "react";
import { useRouter, usePathname } from "next/navigation";
import { ArrowLeft, Plus, Wallet, User } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useMe } from "@/hooks/useApi";
import { formatBRL, cn } from "@/lib/utils";
import { useTelegram } from "@/components/providers";

// ============================================================
// 🎛️ PROPS
// ============================================================
interface TopbarProps {
  title?: string;
  showBack?: boolean;
  showBalance?: boolean;
  showAvatar?: boolean;
  showAdd?: boolean;
  rightSlot?: React.ReactNode;
  className?: string;
}

// ============================================================
// 🏠 COMPONENTE
// ============================================================
export function Topbar({
  title,
  showBack = false,
  showBalance = true,
  showAvatar = true,
  showAdd = true,
  rightSlot,
  className,
}: TopbarProps) {
  const router = useRouter();
  const pathname = usePathname();
  const { backButton, hapticImpact, user: tgUser } = useTelegram();
  const { data: me, isLoading } = useMe();

  const isHome = pathname === "/";

  // ============================================================
  // ⬅️ BOTÃO VOLTAR (nativo do Telegram)
  // ============================================================
  React.useEffect(() => {
    if (!backButton) return;

    const shouldShow = showBack && !isHome;

    if (shouldShow) {
      const handler = () => {
        hapticImpact("light");
        router.back();
      };
      backButton.onClick(handler);
      backButton.show();
      return () => {
        try {
          backButton.offClick(handler);
          backButton.hide();
        } catch {
          // ignora
        }
      };
    } else {
      try {
        backButton.hide();
      } catch {
        // ignora
      }
    }
  }, [backButton, showBack, isHome, router, hapticImpact]);

  // ============================================================
  // 🖐️ HANDLERS
  // ============================================================
  const handleBack = React.useCallback(() => {
    hapticImpact("light");
    if (window.history.length > 1) router.back();
    else router.push("/");
  }, [router, hapticImpact]);

  const handleAdd = React.useCallback(() => {
    hapticImpact("medium");
    router.push("/wallet");
  }, [router, hapticImpact]);

  const handleAvatar = React.useCallback(() => {
    hapticImpact("light");
    // Por enquanto, sem tela de perfil dedicada — mantém apenas feedback
  }, [hapticImpact]);

  // ============================================================
  // 🧮 DADOS DO USUÁRIO
  // ============================================================
  const initials = React.useMemo(() => {
    const name =
      me?.firstName ||
      tgUser?.first_name ||
      me?.username ||
      tgUser?.username ||
      "?";
    return name.trim().charAt(0).toUpperCase();
  }, [me, tgUser]);

  const photoUrl =
    me?.photoUrl || tgUser?.photo_url || null;

  // ============================================================
  // 🎨 RENDER
  // ============================================================
  return (
    <header
      className={cn(
        "sticky top-0 z-40 flex items-center gap-2 px-3 py-2.5",
        "lariz-glass border-b border-border/60",
        "lariz-safe-top",
        className,
      )}
    >
      {/* ---------- Botão voltar ---------- */}
      {showBack && !isHome ? (
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={handleBack}
          aria-label="Voltar"
          className="shrink-0"
        >
          <ArrowLeft className="h-5 w-5" />
        </Button>
      ) : null}

      {/* ---------- Título / Avatar ---------- */}
      <div className="flex min-w-0 flex-1 items-center gap-2">
        {showAvatar && photoUrl ? (
          <button
            type="button"
            onClick={handleAvatar}
            className="h-9 w-9 shrink-0 overflow-hidden rounded-full border border-border"
            aria-label="Perfil"
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={photoUrl}
              alt="Avatar"
              className="h-full w-full object-cover"
            />
          </button>
        ) : showAvatar ? (
          <button
            type="button"
            onClick={handleAvatar}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-border bg-secondary text-sm font-semibold"
            aria-label="Perfil"
          >
            {initials}
          </button>
        ) : null}

        {title ? (
          <h1 className="truncate text-base font-semibold">{title}</h1>
        ) : null}
      </div>

      {/* ---------- Saldo ---------- */}
      {showBalance && (
        <div className="flex items-center gap-1.5 rounded-full border border-border/60 bg-card/60 px-3 py-1.5">
          <Wallet className="h-3.5 w-3.5 text-primary" />
          {isLoading ? (
            <Skeleton className="h-4 w-16" />
          ) : (
            <span className="font-mono text-sm font-semibold tabular-nums">
              {formatBRL(me?.balance ?? 0)}
            </span>
          )}
        </div>
      )}

      {/* ---------- Botão "+" (recarga) ---------- */}
      {showAdd && (
        <Button
          variant="default"
          size="icon-sm"
          onClick={handleAdd}
          aria-label="Adicionar saldo"
          className="shrink-0"
        >
          <Plus className="h-4 w-4" />
        </Button>
      )}

      {/* ---------- Slot customizado ---------- */}
      {rightSlot}
    </header>
  );
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export default Topbar;
