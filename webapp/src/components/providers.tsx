/**
 * Larizinha Store — WebApp Next.js
 * Providers globais:
 * - ThemeProvider (next-themes): alterna dark/light, respeita Telegram
 * - TelegramProvider: expõe o SDK do Telegram WebApp (WebApp, initData, user)
 * - QueryProvider (SWR config): cache global + revalidação
 * - AuthProvider: guarda o usuário autenticado pelo initData
 */
"use client";

import * as React from "react";
import { ThemeProvider as NextThemesProvider } from "next-themes";
import { SWRConfig } from "swr";

import { fetcher } from "@/lib/api";

// ============================================================
// 📱 TIPAGEM DO TELEGRAM WEBAPP
// ============================================================
export interface TelegramUser {
  id: number;
  first_name?: string;
  last_name?: string;
  username?: string;
  language_code?: string;
  is_premium?: boolean;
  photo_url?: string;
}

export interface TelegramWebApp {
  initData: string;
  initDataUnsafe: {
    user?: TelegramUser;
    auth_date?: number;
    hash?: string;
    start_param?: string;
  };
  version: string;
  platform: string;
  colorScheme: "light" | "dark";
  themeParams: Record<string, string>;
  isExpanded: boolean;
  viewportHeight: number;
  viewportStableHeight: number;
  headerColor: string;
  backgroundColor: string;
  ready: () => void;
  expand: () => void;
  close: () => void;
  showAlert: (message: string, callback?: () => void) => void;
  showConfirm: (message: string, callback?: (confirmed: boolean) => void) => void;
  showPopup: (params: Record<string, unknown>, callback?: (id: string) => void) => void;
  HapticFeedback?: {
    impactOccurred: (style: "light" | "medium" | "heavy" | "rigid" | "soft") => void;
    notificationOccurred: (type: "error" | "success" | "warning") => void;
    selectionChanged: () => void;
  };
  openLink: (url: string, options?: { try_instant_view?: boolean }) => void;
  openTelegramLink: (url: string) => void;
  MainButton: {
    text: string;
    color: string;
    textColor: string;
    isVisible: boolean;
    isActive: boolean;
    isProgressVisible: boolean;
    setText: (text: string) => void;
    onClick: (cb: () => void) => void;
    offClick: (cb: () => void) => void;
    show: () => void;
    hide: () => void;
    enable: () => void;
    disable: () => void;
    showProgress: (leaveActive?: boolean) => void;
    hideProgress: () => void;
    setParams: (params: Record<string, unknown>) => void;
  };
  BackButton: {
    isVisible: boolean;
    onClick: (cb: () => void) => void;
    offClick: (cb: () => void) => void;
    show: () => void;
    hide: () => void;
  };
  disableVerticalSwipes?: () => void;
  enableClosingConfirmation?: () => void;
}

declare global {
  interface Window {
    Telegram?: {
      WebApp: TelegramWebApp;
    };
  }
}

// ============================================================
// 🎨 PROVIDER DE TEMA
// ============================================================
interface ThemeProviderProps {
  children: React.ReactNode;
}

function ThemeProvider({ children }: ThemeProviderProps) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="dark"
      enableSystem
      disableTransitionOnChange
      storageKey="larizinha-theme"
    >
      {children}
    </NextThemesProvider>
  );
}

// ============================================================
// 📱 PROVIDER DO TELEGRAM
// ============================================================
interface TelegramContextValue {
  webApp: TelegramWebApp | null;
  user: TelegramUser | null;
  initData: string;
  isReady: boolean;
  colorScheme: "light" | "dark";
  haptic: (type: "success" | "error" | "warning") => void;
  hapticImpact: (style?: "light" | "medium" | "heavy") => void;
  hapticSelection: () => void;
  close: () => void;
  showAlert: (msg: string) => void;
  showConfirm: (msg: string) => Promise<boolean>;
  openLink: (url: string) => void;
  mainButton: TelegramWebApp["MainButton"] | null;
  backButton: TelegramWebApp["BackButton"] | null;
}

const TelegramContext = React.createContext<TelegramContextValue | null>(null);

function TelegramProvider({ children }: { children: React.ReactNode }) {
  const [webApp, setWebApp] = React.useState<TelegramWebApp | null>(null);
  const [user, setUser] = React.useState<TelegramUser | null>(null);
  const [initData, setInitData] = React.useState<string>("");
  const [isReady, setIsReady] = React.useState(false);
  const [colorScheme, setColorScheme] = React.useState<"light" | "dark">("dark");

  // ---- Inicialização ----
  React.useEffect(() => {
    let mounted = true;

    function bootstrap() {
      const tg = typeof window !== "undefined" ? window.Telegram?.WebApp : null;

      if (!tg) {
        // Roda fora do Telegram (browser normal — dev)
        // Usa valores fictícios para desenvolvimento local
        if (mounted) {
          setIsReady(true);
          setColorScheme("dark");
        }
        return;
      }

      try {
        tg.ready();
        tg.expand();
        tg.disableVerticalSwipes?.();
      } catch {
        // ignora — algumas versões não têm certos métodos
      }

      if (!mounted) return;

      setWebApp(tg);
      setUser(tg.initDataUnsafe?.user || null);
      setInitData(tg.initData || "");
      setColorScheme(tg.colorScheme || "dark");
      setIsReady(true);
    }

    // O SDK do Telegram pode carregar depois
    if (window.Telegram?.WebApp) {
      bootstrap();
    } else {
      const interval = setInterval(() => {
        if (window.Telegram?.WebApp) {
          clearInterval(interval);
          bootstrap();
        }
      }, 50);
      // Timeout de segurança (2s)
      const timeout = setTimeout(() => {
        clearInterval(interval);
        if (mounted) setIsReady(true);
      }, 2000);

      return () => {
        mounted = false;
        clearInterval(interval);
        clearTimeout(timeout);
      };
    }

    return () => {
      mounted = false;
    };
  }, []);

  // ---- Haptic Feedback ----
  const haptic = React.useCallback(
    (type: "success" | "error" | "warning") => {
      try {
        webApp?.HapticFeedback?.notificationOccurred(type);
      } catch {}
    },
    [webApp],
  );

  const hapticImpact = React.useCallback(
    (style: "light" | "medium" | "heavy" = "light") => {
      try {
        webApp?.HapticFeedback?.impactOccurred(style);
      } catch {}
    },
    [webApp],
  );

  const hapticSelection = React.useCallback(() => {
    try {
      webApp?.HapticFeedback?.selectionChanged();
    } catch {}
  }, [webApp]);

  // ---- Helpers ----
  const close = React.useCallback(() => {
    try {
      webApp?.close();
    } catch {}
  }, [webApp]);

  const showAlert = React.useCallback(
    (msg: string) => {
      try {
        webApp?.showAlert(msg);
      } catch {
        // Fallback browser
        if (typeof window !== "undefined") window.alert(msg);
      }
    },
    [webApp],
  );

  const showConfirm = React.useCallback(
    (msg: string): Promise<boolean> => {
      return new Promise((resolve) => {
        try {
          if (webApp?.showConfirm) {
            webApp.showConfirm(msg, (confirmed: boolean) => resolve(confirmed));
          } else {
            resolve(window.confirm(msg));
          }
        } catch {
          resolve(false);
        }
      });
    },
    [webApp],
  );

  const openLink = React.useCallback(
    (url: string) => {
      try {
        if (webApp?.openLink) {
          webApp.openLink(url);
        } else {
          window.open(url, "_blank", "noopener,noreferrer");
        }
      } catch {
        window.open(url, "_blank", "noopener,noreferrer");
      }
    },
    [webApp],
  );

  // ---- Contexto ----
  const value = React.useMemo<TelegramContextValue>(
    () => ({
      webApp,
      user,
      initData,
      isReady,
      colorScheme,
      haptic,
      hapticImpact,
      hapticSelection,
      close,
      showAlert,
      showConfirm,
      openLink,
      mainButton: webApp?.MainButton ?? null,
      backButton: webApp?.BackButton ?? null,
    }),
    [
      webApp,
      user,
      initData,
      isReady,
      colorScheme,
      haptic,
      hapticImpact,
      hapticSelection,
      close,
      showAlert,
      showConfirm,
      openLink,
    ],
  );

  return (
    <TelegramContext.Provider value={value}>{children}</TelegramContext.Provider>
  );
}

// ============================================================
// 🪝 HOOK DE ACESSO AO TELEGRAM
// ============================================================
export function useTelegram(): TelegramContextValue {
  const ctx = React.useContext(TelegramContext);
  if (!ctx) {
    throw new Error("useTelegram deve ser usado dentro de <Providers>");
  }
  return ctx;
}

// ============================================================
// 🌐 PROVIDER DE FETCH (SWR)
// ============================================================
function QueryProvider({ children }: { children: React.ReactNode }) {
  return (
    <SWRConfig
      value={{
        fetcher,
        revalidateOnFocus: false,
        revalidateOnReconnect: true,
        shouldRetryOnError: false,
        errorRetryCount: 2,
        dedupingInterval: 5000,
      }}
    >
      {children}
    </SWRConfig>
  );
}

// ============================================================
// 🔗 PROVIDERS (composição final)
// ============================================================
export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider>
      <TelegramProvider>
        <QueryProvider>{children}</QueryProvider>
      </TelegramProvider>
    </ThemeProvider>
  );
}

// ============================================================
// 📌 EXPORTS ADICIONAIS
// ============================================================
export { TelegramContext };
export type { TelegramContextValue };
