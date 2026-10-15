/**
 * Larizinha Store — API TypeScript
 * Logger (Pino) — logs estruturados em JSON.
 * Em desenvolvimento, usa pino-pretty para colorir.
 */
import pino from "pino";

import env, { isProduction } from "./env";

// ============================================================
// 🔧 CONFIGURAÇÃO
// ============================================================
const baseOptions: pino.LoggerOptions = {
  level: env.LOG_LEVEL,
  base: {
    app: env.APP_NAME,
    env: env.ENV,
    version: env.APP_VERSION,
  },
  timestamp: pino.stdTimeFunctions.isoTime,
  formatters: {
    level: (label) => ({ level: label }),
  },
  redact: {
    paths: [
      // Nunca loga segredos
      "*.password",
      "*.token",
      "*.secret",
      "*.accessToken",
      "*.access_token",
      "*.mpAccessToken",
      "*.WEBHOOK_SECRET_TOKEN",
      "*.BOT_TOKEN",
      "*.MP_ACCESS_TOKEN",
    ],
    censor: "[REDACTED]",
  },
};

// ============================================================
// 🚀 INSTÂNCIA
// ============================================================
export const logger = isProduction
  ? pino(baseOptions)
  : pino({
      ...baseOptions,
      transport: {
        target: "pino-pretty",
        options: {
          colorize: true,
          translateTime: "SYS:yyyy-mm-dd HH:MM:ss.l",
          ignore: "pid,hostname,app,env,version",
          messageFormat: "{level}: {msg}",
        },
      },
    });

// ============================================================
// 🧩 HELPERS
// ============================================================
export function childLogger(bindings: Record<string, unknown>) {
  return logger.child(bindings);
}

export function logApiRequest(data: {
  method: string;
  url: string;
  status: number;
  durationMs: number;
  ip?: string;
  userId?: number | string | null;
}) {
  logger.info(
    {
      type: "http",
      ...data,
    },
    `${data.method} ${data.url} → ${data.status} (${data.durationMs}ms)`,
  );
}

export function logApiError(data: {
  method: string;
  url: string;
  error: string;
  stack?: string;
  userId?: number | string | null;
}) {
  logger.error({ type: "http-error", ...data }, data.error);
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export default logger;
