/**
 * Larizinha Store — API TypeScript
 * Plugin CORS — permite chamadas do WebApp e do painel admin.
 */
import cors from "@fastify/cors";
import fp from "fastify-plugin";

import { isProduction } from "@/config/env";

interface CorsPluginOptions {
  origins: string[] | true;
}

async function plugin(
  app: import("fastify").FastifyInstance,
  opts: CorsPluginOptions,
): Promise<void> {
  await app.register(cors, {
    origin: (origin, cb) => {
      // Sem origin (requisições server-side / curl) → libera
      if (!origin) {
        cb(null, true);
        return;
      }

      // Wildcard → libera tudo
      if (opts.origins === true) {
        cb(null, true);
        return;
      }

      // Lista específica
      if (opts.origins.includes(origin)) {
        cb(null, true);
        return;
      }

      // Em produção, permite t.me e *.onrender.com
      if (isProduction) {
        if (
          origin.endsWith(".onrender.com") ||
          origin.startsWith("https://t.me") ||
          origin.startsWith("https://web.telegram.org")
        ) {
          cb(null, true);
          return;
        }
      }

      cb(new Error("Origem não permitida pelo CORS"), false);
    },
    credentials: true,
    methods: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allowedHeaders: [
      "Content-Type",
      "Authorization",
      "X-Telegram-Init-Data",
      "X-Request-Id",
      "X-Webhook-Secret",
    ],
    exposedHeaders: ["X-Request-Id"],
    maxAge: 86400,
  });
}

export default fp(plugin, { name: "cors-plugin" });
