/**
 * Larizinha Store — WebApp Next.js
 * Utilitários compartilhados (formatação, helpers, validações).
 */
import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

// ============================================================
// 🎨 TAILWIND — cn()
// ============================================================
/**
 * Junta classes do Tailwind resolvendo conflitos.
 * Uso: cn("px-2", condition && "px-4") → "px-4"
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

// ============================================================
// 💰 FORMATAÇÃO DE MOEDA (BRL)
// ============================================================
/**
 * Formata um número em Real Brasileiro.
 * 1234.56 → "R$ 1.234,56"
 */
export function formatBRL(
  value: number | string | null | undefined,
  options?: { compact?: boolean; showSymbol?: boolean },
): string {
  const num = typeof value === "string" ? parseFloat(value) : (value ?? 0);
  const safe = Number.isFinite(num) ? num : 0;

  const showSymbol = options?.showSymbol !== false;

  if (options?.compact && Math.abs(safe) >= 1000) {
    const formatted = new Intl.NumberFormat("pt-BR", {
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(safe);
    return showSymbol ? `R$ ${formatted}` : formatted;
  }

  const formatted = new Intl.NumberFormat("pt-BR", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(safe);

  return showSymbol ? `R$ ${formatted}` : formatted;
}

// ============================================================
// 🔢 FORMATAÇÃO DE NÚMEROS
// ============================================================
export function formatNumber(
  value: number | string | null | undefined,
  decimals = 0,
): string {
  const num = typeof value === "string" ? parseFloat(value) : (value ?? 0);
  const safe = Number.isFinite(num) ? num : 0;
  return new Intl.NumberFormat("pt-BR", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(safe);
}

/** 1234 → "1,2 mil" · 1_200_000 → "1,2 mi" */
export function formatCompact(value: number | string | null | undefined): string {
  const num = typeof value === "string" ? parseFloat(value) : (value ?? 0);
  const safe = Number.isFinite(num) ? num : 0;
  if (Math.abs(safe) < 1000) return String(safe);
  return new Intl.NumberFormat("pt-BR", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(safe);
}

// ============================================================
// 📅 FORMATAÇÃO DE DATA
// ============================================================
export function formatDate(
  value: string | Date | null | undefined,
  opts?: { withTime?: boolean; short?: boolean },
): string {
  if (!value) return "—";
  const d = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return "—";

  const withTime = opts?.withTime ?? false;
  const short = opts?.short ?? false;

  if (short && !withTime) {
    return new Intl.DateTimeFormat("pt-BR", {
      day: "2-digit",
      month: "2-digit",
    }).format(d);
  }

  if (withTime) {
    return new Intl.DateTimeFormat("pt-BR", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(d);
  }

  return new Intl.DateTimeFormat("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(d);
}

/** "há 5 min", "há 2h", "há 3 dias" */
export function timeAgo(value: string | Date | null | undefined): string {
  if (!value) return "—";
  const d = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return "—";

  const diff = Math.floor((Date.now() - d.getTime()) / 1000);

  if (diff < 0) return "agora";
  if (diff < 60) return `há ${diff}s`;
  if (diff < 3600) return `há ${Math.floor(diff / 60)}min`;
  if (diff < 86400) return `há ${Math.floor(diff / 3600)}h`;
  if (diff < 604800) return `há ${Math.floor(diff / 86400)}d`;
  return formatDate(d, { short: true });
}

// ============================================================
// 📝 TEXTO
// ============================================================
export function truncate(
  text: string | null | undefined,
  max = 40,
  suffix = "...",
): string {
  if (!text) return "";
  const s = String(text).trim();
  if (s.length <= max) return s;
  return s.slice(0, Math.max(0, max - suffix.length)) + suffix;
}

export function getInitials(name: string | null | undefined): string {
  if (!name) return "?";
  const parts = String(name).trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0]?.toUpperCase() ?? "").join("") || "?";
}

export function maskSecret(value: string | null | undefined, keep = 4): string {
  if (!value) return "—";
  const s = String(value);
  if (s.length <= keep * 2) return "*".repeat(s.length);
  return "*".repeat(s.length - keep) + s.slice(-keep);
}

export function capitalize(value: string | null | undefined): string {
  if (!value) return "";
  return String(value).charAt(0).toUpperCase() + String(value).slice(1);
}

// ============================================================
// 🧠 OBJETOS / JSON
// ============================================================
export function safeJsonParse<T = unknown>(
  value: string | null | undefined,
  fallback: T | null = null,
): T | null {
  if (!value) return fallback;
  try {
    return JSON.parse(value) as T;
  } catch {
    return fallback;
  }
}

export function safeNumber(
  value: unknown,
  fallback = 0,
): number {
  const n = typeof value === "number" ? value : parseFloat(String(value));
  return Number.isFinite(n) ? n : fallback;
}

// ============================================================
// ⏱️ TIMERS / ASYNC
// ============================================================
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function debounce<T extends (...args: unknown[]) => void>(
  fn: T,
  wait = 300,
): (...args: Parameters<T>) => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return (...args: Parameters<T>) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

export function throttle<T extends (...args: unknown[]) => void>(
  fn: T,
  wait = 200,
): (...args: Parameters<T>) => void {
  let last = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  return (...args: Parameters<T>) => {
    const now = Date.now();
    const remaining = wait - (now - last);
    if (remaining <= 0) {
      last = now;
      fn(...args);
    } else if (!timer) {
      timer = setTimeout(() => {
        last = Date.now();
        timer = null;
        fn(...args);
      }, remaining);
    }
  };
}

// ============================================================
// 📋 CLIPBOARD
// ============================================================
export async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    // Fallback para HTTP / navegadores antigos
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    textarea.style.pointerEvents = "none";
    document.body.appendChild(textarea);
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(textarea);
    return ok;
  } catch {
    return false;
  }
}

// ============================================================
// ✅ VALIDAÇÕES
// ============================================================
export function isValidEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
}

export function isValidPhone(phone: string): boolean {
  const digits = phone.replace(/\D/g, "");
  return digits.length >= 10 && digits.length <= 13;
}

export function isValidCpf(cpf: string): boolean {
  const digits = cpf.replace(/\D/g, "");
  return digits.length === 11;
}

export function isValidPixKey(key: string): boolean {
  const k = key.trim();
  if (!k) return false;
  if (/^\d{11}$/.test(k)) return true; // CPF
  if (/^\d{14}$/.test(k)) return true; // CNPJ
  if (/^\+?\d{10,13}$/.test(k)) return true; // telefone
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(k)) return true; // e-mail
  if (/^[0-9a-fA-F-]{32,45}$/.test(k)) return true; // aleatória
  return false;
}

// ============================================================
// 💵 PARSER DE VALOR (aceita "10,50" ou "10.50")
// ============================================================
export function parseMoney(input: string): number | null {
  if (!input) return null;
  const cleaned = String(input).trim().replace(/[^\d,.-]/g, "");
  // Se tiver vírgula, assume BR: troca por ponto
  const normalized =
    cleaned.includes(",") && cleaned.lastIndexOf(",") > cleaned.lastIndexOf(".")
      ? cleaned.replace(/\./g, "").replace(",", ".")
      : cleaned;
  const n = parseFloat(normalized);
  return Number.isFinite(n) ? n : null;
}

// ============================================================
// 📊 PORCENTAGEM / PROGRESSO
// ============================================================
export function percent(value: number, total: number): number {
  if (!total || total <= 0) return 0;
  const p = (value / total) * 100;
  return Math.max(0, Math.min(100, p));
}

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

// ============================================================
// 🎨 CORES / RARIDADE
// ============================================================
export function rarityColor(rarity: string | null | undefined): string {
  const s = String(rarity ?? "").toLowerCase();
  const map: Record<string, string> = {
    comum: "text-zinc-400 border-zinc-400/30 bg-zinc-400/10",
    raro: "text-blue-400 border-blue-400/30 bg-blue-400/10",
    epico: "text-purple-400 border-purple-400/30 bg-purple-400/10",
    lendario: "text-yellow-400 border-yellow-400/30 bg-yellow-400/10",
    mitico: "text-red-400 border-red-400/30 bg-red-400/10",
  };
  return map[s] ?? map.comum;
}

export function rarityEmoji(rarity: string | null | undefined): string {
  const s = String(rarity ?? "").toLowerCase();
  const map: Record<string, string> = {
    comum: "⚪️",
    raro: "🔵",
    epico: "🟣",
    lendario: "🟡",
    mitico: "🔴",
  };
  return map[s] ?? "🎫";
}

// ============================================================
// 📱 DETECÇÃO DE AMBIENTE
// ============================================================
export function isTelegramWebApp(): boolean {
  if (typeof window === "undefined") return false;
  return Boolean(window.Telegram?.WebApp?.initData);
}

export function isMobile(): boolean {
  if (typeof window === "undefined") return false;
  return /Android|iPhone|iPad|iPod|Opera Mini|IEMobile|WPDesktop/i.test(
    navigator.userAgent,
  );
}

// ============================================================
// 🔒 HASH SIMPLES (para caching client-side)
// ============================================================
export function quickHash(input: string): string {
  let hash = 0;
  for (let i = 0; i < input.length; i++) {
    const chr = input.charCodeAt(i);
    hash = (hash << 5) - hash + chr;
    hash |= 0;
  }
  return Math.abs(hash).toString(36);
}

// ============================================================
// 📌 EXPORTS
// ============================================================
const utils = {
  cn,
  formatBRL,
  formatNumber,
  formatCompact,
  formatDate,
  timeAgo,
  truncate,
  getInitials,
  maskSecret,
  capitalize,
  safeJsonParse,
  safeNumber,
  sleep,
  debounce,
  throttle,
  copyToClipboard,
  isValidEmail,
  isValidPhone,
  isValidCpf,
  isValidPixKey,
  parseMoney,
  percent,
  clamp,
  rarityColor,
  rarityEmoji,
  isTelegramWebApp,
  isMobile,
  quickHash,
};

export default utils;
