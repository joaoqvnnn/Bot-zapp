/**
 * Larizinha Store — API TypeScript
 * Carrega e valida TODAS as variáveis de ambiente via Zod.
 * Falha rápido (throw) se algo obrigatório estiver faltando.
 */
import { z } from "zod";
import dotenv from "dotenv";

// Carrega .env da raiz do projeto
dotenv.config();

// ============================================================
// 🧩 SCHEMA
// ============================================================
const EnvSchema = z.object({
  // ---------- Aplicação ----------
  NODE_ENV: z
    .enum(["development", "production", "test"])
    .default("production"),
  ENV: z.string().default("production"),
  APP_NAME: z.string().default("Larizinha Store"),
  APP_VERSION: z.string().default("1.0.0"),
  TZ: z.string().default("America/Sao_Paulo"),

  // ---------- Servidor ----------
  PORT: z.coerce.number().int().positive().default(8000),
  HOST: z.string().default("0.0.0.0"),

  // ---------- Database ----------
  DATABASE_URL: z.string().min(10, "DATABASE_URL obrigatória"),
  DATABASE_URL_SYNC: z.string().optional(),

  // ---------- Redis ----------
  REDIS_URL: z.string().min(10, "REDIS_URL obrigatória"),
  REDIS_PREFIX: z.string().default("larizinha"),

  // ---------- Telegram ----------
  BOT_TOKEN: z.string().min(20, "BOT_TOKEN obrigatório"),
  BOT_USERNAME: z.string().optional().default(""),
  MAIN_ADMIN_ID: z.coerce.number().int().default(0),
  ADMIN_IDS: z.string().optional().default(""),

  // ---------- Webhooks ----------
  WEBHOOK_SECRET_TOKEN: z.string().default(""),
  MP_WEBHOOK_URL: z.string().optional().default(""),

  // ---------- Mercado Pago ----------
  MP_ACCESS_TOKEN: z.string().optional().default(""),
  MP_PUBLIC_KEY: z.string().optional().default(""),
  MP_WEBHOOK_SECRET: z.string().optional().default(""),

  // ---------- WebApp ----------
  WEBAPP_URL: z.string().optional().default(""),
  WEBAPP_SECRET: z.string().optional().default(""),
  WEBAPP_AUTH_MAX_AGE: z.coerce.number().int().default(86400),

  // ---------- Logs ----------
  LOG_LEVEL: z
    .enum(["fatal", "error", "warn", "info", "debug", "trace"])
    .default("info"),

  // ---------- CORS ----------
  CORS_ORIGINS: z.string().optional().default("*"),

  // ---------- Features ----------
  ENABLE_CONCURRENCY_LOCK: z
    .string()
    .transform((v) => v.toLowerCase() === "true")
    .default("true"),
  PRODUCT_LOCK_TTL: z.coerce.number().int().default(300),
});

// ============================================================
// 🚀 CARREGA E VALIDA
// ============================================================
let env: z.infer<typeof EnvSchema>;

try {
  env = EnvSchema.parse(process.env);
} catch (err) {
  if (err instanceof z.ZodError) {
    // eslint-disable-next-line no-console
    console.error("\n❌ Erro ao carregar variáveis de ambiente:\n");
    for (const issue of err.issues) {
      // eslint-disable-next-line no-console
      console.error(`   • ${issue.path.join(".")}: ${issue.message}`);
    }
    // eslint-disable-next-line no-console
    console.error("\n   Verifique seu .env ou as Environment Variables do Render.\n");
  } else {
    // eslint-disable-next-line no-console
    console.error("❌ Erro inesperado ao carregar env:", err);
  }
  process.exit(1);
}

// ============================================================
// 🧮 HELPERS
// ============================================================
export const adminIdsList: number[] = (() => {
  const raw = (env.ADMIN_IDS || "").trim();
  const list: number[] = [];
  if (raw) {
    for (const chunk of raw.split(",")) {
      const n = chunk.trim();
      if (/^\d+$/.test(n)) list.push(Number(n));
    }
  }
  if (env.MAIN_ADMIN_ID && !list.includes(env.MAIN_ADMIN_ID)) {
    list.unshift(env.MAIN_ADMIN_ID);
  }
  return list;
})();

export const corsOrigins: string[] | true = (() => {
  const raw = env.CORS_ORIGINS.trim();
  if (raw === "*" || raw === "") return true;
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
})();

export const isProduction = env.NODE_ENV === "production";
export const isDevelopment = env.NODE_ENV === "development";

// ============================================================
// 📌 EXPORTS
// ============================================================
export { env };
export default env;
