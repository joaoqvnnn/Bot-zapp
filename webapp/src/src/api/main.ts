/**
 * Larizinha Store — API TypeScript
 * Servidor Fastify principal.
 *
 * Responsabilidades:
 *   - Registrar plugins (CORS, helmet, rate-limit, sensible, cookie, jwt)
 *   - Registrar rotas (/health, /webhook/*, /webapp/*, /admin/*)
 *   - Graceful shutdown (SIGTERM/SIGINT)
 *   - Conectar/desconectar Postgres e Redis
 *   - Expor healthcheck para o Render
 */
import Fastify, { type FastifyInstance } from "fastify";

import env, { corsOrigins, isProduction } from "@/config/env";
import logger from "@/config/logger";
import { connectPrisma, disconnectPrisma, prismaHealthcheck } from "@/config/prisma";
import { connectRedis, disconnectRedis, redisHealthcheck } from "@/config/redis";

// ---------- Plugins ----------
import corsPlugin from "@/api/plugins/cors";
import helmetPlugin from "@/api/plugins/helmet";
import rateLimitPlugin from "@/api/plugins/rate-limit";
import sensiblePlugin from "@/api/plugins/sensible";
import cookiePlugin from "@/api/plugins/cookie";

// ---------- Rotas ----------
import healthRoutes from "@/api/routes/health";
import webappRoutes from "@/api/routes/webapp";
import webhookMpRoutes from "@/api/routes/webhook/mercadopago";
import webhookTelegramRoutes from "@/api/routes/webhook/telegram";

// ============================================================
// 🚀 BUILD APP
// ============================================================
export async function buildApp(): Promise<FastifyInstance> {
  const app = Fastify({
    logger: false, // usamos nosso pino customizado
    trustProxy: true,
    bodyLimit: 2 * 1024 * 1024, // 2 MB
    disableRequestLogging: true,
    requestIdHeader: "x-request-id",
    requestIdLogLabel: "reqId",
    ajv: {
      customOptions: {
        removeAdditional: "all",
        coerceTypes: true,
        allErrors: false,
      },
    },
  });

  // ==========================================================
  // 📌 HOOKS DE LOG (usando nosso pino)
  // ==========================================================
  app.addHook("onRequest", async (req) => {
    (req as unknown as { startTime: number }).startTime = Date.now();
  });

  app.addHook("onResponse", async (req, reply) => {
    const startTime = (req as unknown as { startTime: number }).startTime ?? Date.now();
    const durationMs = Date.now() - startTime;
    logger.info(
      {
        type: "http",
        method: req.method,
        url: req.url,
        status: reply.statusCode,
        durationMs,
        ip: req.ip,
        userId: (req as unknown as { userId?: number }).userId ?? null,
      },
      `${req.method} ${req.url} → ${reply.statusCode} (${durationMs}ms)`,
    );
  });

  app.addHook("onError", async (req, reply, error) => {
    logger.error(
      {
        type: "http-error",
        method: req.method,
        url: req.url,
        status: reply.statusCode,
        error: error.message,
        stack: isProduction ? undefined : error.stack,
      },
      `Erro em ${req.method} ${req.url}: ${error.message}`,
    );
  });

  // ==========================================================
  // 🧩 PLUGINS
  // ==========================================================
  await app.register(corsPlugin, { origins: corsOrigins });
  await app.register(helmetPlugin);
  await app.register(sensiblePlugin);
  await app.register(cookiePlugin);
  await app.register(rateLimitPlugin);

  // ==========================================================
  // 🛣️ ROTAS
  // ==========================================================
  await app.register(healthRoutes, { prefix: "/" });
  await app.register(webappRoutes, { prefix: "/webapp" });
  await app.register(webhookMpRoutes, { prefix: "/webhook" });
  await app.register(webhookTelegramRoutes, { prefix: "/webhook" });

  // ==========================================================
  // 🚫 HANDLER 404 (JSON)
  // ==========================================================
  app.setNotFoundHandler((req, reply) => {
    reply.status(404).send({
      ok: false,
      error: {
        code: "NOT_FOUND",
        message: `Rota ${req.method} ${req.url} não encontrada.`,
      },
    });
  });

  // ==========================================================
  // 🚨 HANDLER DE ERRO GLOBAL
  // ==========================================================
  app.setErrorHandler((error, req, reply) => {
    const status = error.statusCode ?? 500;
    const message =
      status >= 500 && isProduction
        ? "Erro interno do servidor"
        : error.message;

    if (status >= 500) {
      logger.error(
        { err: error, url: req.url, method: req.method },
        "Erro não tratado",
      );
    }

    reply.status(status).send({
      ok: false,
      error: {
        code: error.code ?? "INTERNAL_ERROR",
        message,
        details: isProduction ? null : (error.validation ?? null),
      },
    });
  });

  return app;
}

// ============================================================
// 🎯 BOOTSTRAP
// ============================================================
async function start(): Promise<void> {
  logger.info(
    `🚀 Iniciando ${env.APP_NAME} API v${env.APP_VERSION} (env=${env.ENV})`,
  );

  // ---------- Conexões ----------
  try {
    await connectPrisma();
  } catch (err) {
    logger.error({ err }, "Falha ao conectar no Postgres");
    process.exit(1);
  }

  try {
    await connectRedis();
  } catch (err) {
    logger.error({ err }, "Falha ao conectar no Redis");
    process.exit(1);
  }

  // ---------- Fastify ----------
  const app = await buildApp();

  // ---------- Healthcheck inicial ----------
  const [dbOk, redisOk] = await Promise.all([
    prismaHealthcheck(),
    redisHealthcheck(),
  ]);
  logger.info(`🗄️  Postgres: ${dbOk ? "OK" : "FALHOU"}`);
  logger.info(`⚡ Redis:    ${redisOk ? "OK" : "FALHOU"}`);

  // ---------- Escuta ----------
  try {
    await app.listen({
      host: env.HOST,
      port: env.PORT,
    });
    logger.info(`🌐 Servidor ouvindo em http://${env.HOST}:${env.PORT}`);
    logger.info(`💚 Healthcheck em http://${env.HOST}:${env.PORT}/health`);
  } catch (err) {
    logger.error({ err }, "Falha ao iniciar servidor");
    process.exit(1);
  }

  // ==========================================================
  // 🛑 GRACEFUL SHUTDOWN
  // ==========================================================
  const shutdown = async (signal: string) => {
    logger.info(`🛑 Recebido sinal ${signal} — encerrando API...`);

    try {
      await app.close();
      logger.info("🛑 Fastify encerrado.");
    } catch (err) {
      logger.warn({ err }, "Erro ao fechar Fastify");
    }

    await disconnectRedis();
    await disconnectPrisma();

    logger.info("✅ API encerrada.");
    process.exit(0);
  };

  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));

  // Captura erros não tratados
  process.on("unhandledRejection", (reason) => {
    logger.error({ reason }, "unhandledRejection");
  });
  process.on("uncaughtException", (err) => {
    logger.error({ err }, "uncaughtException");
  });
}

// ============================================================
// 🚀 ENTRYPOINT
// ============================================================
if (require.main === module) {
  start().catch((err) => {
    logger.error({ err }, "Falha fatal no bootstrap");
    process.exit(1);
  });
}

export { start };
export default buildApp;
