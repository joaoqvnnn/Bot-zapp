/**
 * Larizinha Store — WebApp Next.js
 * BottomNav — barra de navegação inferior fixa.
 * Sincroniza com a rota atual e dá feedback tátil no Telegram.
 */
"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Home,
  ShoppingBag,
  Receipt,
  Wallet,
  User,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { useTelegram } from "@/components/providers";

// ============================================================
// 🗺️ ITENS DA NAVEGAÇÃO
// ============================================================
interface NavItem {
  href: string;
  label: string;
  Icon: LucideIcon;
  matchExact?: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Início", Icon: Home, matchExact: true },
  { href: "/catalog", label: "Catálogo", Icon: ShoppingBag },
  { href: "/orders", label: "Pedidos", Icon: Receipt },
  { href: "/wallet", label: "Carteira", Icon: Wallet },
  { href: "/profile", label: "Perfil", Icon: User },
];

// ============================================================
// 🧮 MATCH DE ROTA ATIVA
// ============================================================
function isActive(pathname: string, item: NavItem): boolean {
  if (item.matchExact) return pathname === item.href;
  if (item.href === "/") return pathname === "/";
  return pathname === item.href || pathname.startsWith(item.href + "/");
}

// ============================================================
// 🏠 BOTTOM NAV
// ============================================================
interface BottomNavProps {
  className?: string;
}

export function BottomNav({ className }: BottomNavProps) {
  const pathname = usePathname() || "/";
  const { hapticSelection } = useTelegram();

  const handleClick = React.useCallback(() => {
    hapticSelection();
  }, [hapticSelection]);

  // ---- Esconde quando dentro do fluxo de senha ou produto ----
  const hidden =
    pathname.startsWith("/password") ||
    pathname.startsWith("/product/");

  if (hidden) return null;

  return (
    <nav
      role="navigation"
      aria-label="Navegação principal"
      className={cn(
        "fixed bottom-0 left-0 right-0 z-40",
        "lariz-glass border-t border-border/60",
        "lariz-safe-bottom",
        className,
      )}
    >
      <ul className="mx-auto grid max-w-md grid-cols-5">
        {NAV_ITEMS.map((item) => {
          const active = isActive(pathname, item);
          const { Icon } = item;

          return (
            <li key={item.href}>
              <Link
                href={item.href}
                onClick={handleClick}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "relative flex flex-col items-center justify-center gap-0.5",
                  "px-1 py-2.5 text-[10px] font-medium",
                  "transition-colors duration-150",
                  active
                    ? "text-primary"
                    : "text-muted-foreground hover:text-foreground/80",
                )}
              >
                {/* Indicador superior */}
                {active && (
                  <span className="absolute -top-px left-1/2 h-0.5 w-8 -translate-x-1/2 rounded-full bg-primary" />
                )}

                <span
                  className={cn(
                    "flex h-7 w-7 items-center justify-center rounded-lg transition-transform",
                    active && "scale-105",
                  )}
                >
                  <Icon
                    className={cn(
                      "h-[18px] w-[18px] transition-all",
                      active && "stroke-[2.4]",
                    )}
                  />
                </span>

                <span className="leading-none">{item.label}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export default BottomNav;
