/**
 * Larizinha Store — WebApp Next.js
 * Layout raiz com:
 * - Metadata (title, description, OpenGraph)
 * - Fontes otimizadas (next/font)
 * - Providers (tema, toasts, Telegram SDK)
 * - Estrutura HTML base
 * - Script de inicialização do Telegram WebApp
 */
import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import Script from "next/script";

import "./globals.css";
import { Providers } from "@/components/providers";
import { Toaster } from "@/components/ui/toaster";
import { cn } from "@/lib/utils";

// ============================================================
// 🔤 FONTES
// ============================================================
const fontSans = Inter({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
  weight: ["400", "500", "600", "700", "800"],
});

const fontMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
  weight: ["400", "500", "700"],
});

// ============================================================
// 📋 METADATA
// ============================================================
const APP_NAME = process.env.NEXT_PUBLIC_STORE_NAME || "Larizinha Store";
const APP_DESCRIPTION =
  "Loja de streamings com entrega 100% automática. Compre, receba, ative.";

export const metadata: Metadata = {
  title: {
    default: APP_NAME,
    template: `%s · ${APP_NAME}`,
  },
  description: APP_DESCRIPTION,
  applicationName: APP_NAME,
  generator: "Next.js",
  keywords: [
    "streaming",
    "loja",
    "streamings",
    "netflix",
    "spotify",
    "combos",
    "telegram",
    "automação",
  ],
  authors: [{ name: APP_NAME }],
  creator: APP_NAME,
  publisher: APP_NAME,
  robots: {
    index: false,
    follow: false,
  },
  formatDetection: {
    email: false,
    address: false,
    telephone: false,
  },
  openGraph: {
    type: "website",
    locale: "pt_BR",
    title: APP_NAME,
    description: APP_DESCRIPTION,
    siteName: APP_NAME,
  },
  icons: {
    icon: [
      { url: "/favicon.ico" },
      { url: "/favicon-32.png", sizes: "32x32", type: "image/png" },
    ],
    apple: "/apple-touch-icon.png",
  },
};

// ============================================================
// 📱 VIEWPORT (mobile-first + Telegram WebApp)
// ============================================================
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#0f1115" },
  ],
  colorScheme: "dark light",
};

// ============================================================
// 🏠 ROOT LAYOUT
// ============================================================
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="pt-BR"
      className={cn(fontSans.variable, fontMono.variable)}
      suppressHydrationWarning
    >
      <head>
        {/* ---------- Favicon ---------- */}
        <link rel="icon" href="/favicon.ico" sizes="any" />

        {/* ---------- Preconnect (performance) ---------- */}
        <link rel="preconnect" href="https://telegram.org" />
        <link rel="dns-prefetch" href="https://telegram.org" />

        {/* ---------- Telegram WebApp SDK ---------- */}
        <Script
          src="https://telegram.org/js/telegram-web-app.js"
          strategy="beforeInteractive"
        />
      </head>

      <body
        className={cn(
          "min-h-screen bg-background font-sans text-foreground antialiased",
          "overscroll-none",
        )}
        suppressHydrationWarning
      >
        {/* ---------- Script inline: aplica tema antes do React montar ---------- */}
        <Script id="tg-theme-bootstrap" strategy="beforeInteractive">
          {`
            (function() {
              try {
                var tg = window.Telegram && window.Telegram.WebApp;
                if (!tg) return;
                // Respeita o tema do Telegram
                var scheme = tg.colorScheme || "dark";
                var root = document.documentElement;
                if (scheme === "light") {
                  root.classList.remove("dark");
                } else {
                  root.classList.add("dark");
                }
                // Expande o viewport ao máximo
                try { tg.expand(); } catch (e) {}
                // Desativa o swipe-back vertical
                try { tg.disableVerticalSwipes && tg.disableVerticalSwipes(); } catch (e) {}
              } catch (e) {}
            })();
          `}
        </Script>

        {/* ---------- Providers ---------- */}
        <Providers>
          <div className="relative flex min-h-dvh flex-col">
            {/* ---------- Conteúdo ---------- */}
            <main className="flex-1">{children}</main>

            {/* ---------- Toasts (feedback) ---------- */}
            <Toaster
              position="top-center"
              toastOptions={{
                duration: 3000,
                style: {
                  background: "hsl(var(--card))",
                  color: "hsl(var(--card-foreground))",
                  border: "1px solid hsl(var(--border))",
                  borderRadius: "0.75rem",
                  fontSize: "14px",
                },
              }}
            />
          </div>
        </Providers>
      </body>
    </html>
  );
}
