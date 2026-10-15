/**
 * Larizinha Store — WebApp Next.js
 * Estados de loading / erro / vazio reutilizáveis.
 */
"use client";

import * as React from "react";
import { AlertTriangle, Loader2, RefreshCw, WifiOff, Package } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// ============================================================
// 🔄 FULL PAGE LOADER
// ============================================================
export function PageLoader({
  message = "Carregando...",
  className,
}: {
  message?: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex min-h-[50vh] flex-col items-center justify-center gap-3 p-8 text-center",
        className,
      )}
    >
      <Loader2 className="h-8 w-8 animate-spin text-primary" />
      <p className="text-sm text-muted-foreground">{message}</p>
    </div>
  );
}

// ============================================================
// ⚠️ ERROR STATE
// ============================================================
interface ErrorStateProps {
  title?: string;
  message?: string;
  onRetry?: () => void;
  retryLabel?: string;
  variant?: "error" | "offline";
  className?: string;
}

export function ErrorState({
  title,
  message,
  onRetry,
  retryLabel = "Tentar novamente",
  variant = "error",
  className,
}: ErrorStateProps) {
  const isOffline = variant === "offline";
  const Icon = isOffline ? WifiOff : AlertTriangle;

  const defaultTitle = isOffline ? "Sem conexão" : "Algo deu errado";
  const defaultMessage = isOffline
    ? "Verifique sua internet e tente novamente."
    : "Não foi possível carregar as informações agora.";

  return (
    <div
      className={cn(
        "flex min-h-[40vh] flex-col items-center justify-center gap-3 p-6 text-center",
        className,
      )}
    >
      <div
        className={cn(
          "flex h-14 w-14 items-center justify-center rounded-full",
          isOffline
            ? "bg-yellow-500/10 text-yellow-500"
            : "bg-destructive/10 text-destructive",
        )}
      >
        <Icon className="h-6 w-6" />
      </div>

      <h3 className="text-base font-semibold">{title ?? defaultTitle}</h3>
      <p className="max-w-xs text-sm text-muted-foreground">
        {message ?? defaultMessage}
      </p>

      {onRetry && (
        <Button
          variant="default"
          size="sm"
          onClick={onRetry}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          className="mt-2"
        >
          {retryLabel}
        </Button>
      )}
    </div>
  );
}

// ============================================================
// 📭 EMPTY STATE
// ============================================================
interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex min-h-[40vh] flex-col items-center justify-center gap-3 p-6 text-center",
        className,
      )}
    >
      <div className="flex h-16 w-16 items-center justify-center rounded-full bg-secondary/60 text-muted-foreground">
        {icon ?? <Package className="h-7 w-7" />}
      </div>
      <h3 className="text-base font-semibold">{title}</h3>
      {description && (
        <p className="max-w-xs text-sm text-muted-foreground">{description}</p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

// ============================================================
// 🎬 SKELETON PAGE (mistura de skeletons)
// ============================================================
export function SkeletonPage() {
  return (
    <div className="space-y-4 p-4">
      <div className="h-32 w-full animate-pulse rounded-2xl bg-secondary/60" />
      <div className="grid grid-cols-2 gap-3">
        <div className="h-44 animate-pulse rounded-2xl bg-secondary/60" />
        <div className="h-44 animate-pulse rounded-2xl bg-secondary/60" />
        <div className="h-44 animate-pulse rounded-2xl bg-secondary/60" />
        <div className="h-44 animate-pulse rounded-2xl bg-secondary/60" />
      </div>
    </div>
  );
}

// ============================================================
// 🎯 LOADING OVERLAY (in-place)
// ============================================================
export function LoadingOverlay({
  visible,
  message,
}: {
  visible: boolean;
  message?: string;
}) {
  if (!visible) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm">
      <div className="flex flex-col items-center gap-3 rounded-2xl border border-border bg-card p-6 shadow-2xl">
        <Loader2 className="h-6 w-6 animate-spin text-primary" />
        {message && (
          <p className="text-sm text-muted-foreground">{message}</p>
        )}
      </div>
    </div>
  );
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export default PageLoader;
