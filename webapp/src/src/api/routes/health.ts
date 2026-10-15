/**
 * Larizinha Store — API TypeScript
 * Rotas de healthcheck — usadas pelo Render e monitoramento externo.
 *
 * Endpoints:
 *   GET /health   → liveness + verificação de dependências (200/503)
 *   GET /ready    → readiness (só DB/Redis)
 *   GET /         → raiz (JSON com metadados)
 */
import type { FastifyInstance, FastifyPluginAsync } from "fastify";

import env from "@/config/env";
import { prismaHealthcheck } from "@/config/prisma";
import { redisHealthcheck } from "@/config/redis";

// ============================================================
// 🧩 HELPERS
// ============================================================
async function checkDependencies() {
  const [dbOk, redisOk] = await Promise.all([
    prismaHealthcheck(),
    redisHealthcheck(),
  ]);

  return {
    db: dbOk,
    redis: redisOk,
    allOk: dbOk && redisOk,
  };
}

// ============================================================
// 🛣️ ROTAS
// ============================================================
const healthRoutes: FastifyPluginAsync = async (app: FastifyInstance) => {
  // ---------- GET /health ----------
  app.get("/health", async (_req, reply) => {
    const deps = await checkDependencies();
    const status = deps.allOk ? "ok" : "degraded";
    const httpStatus = deps.allOk ? 200 : 503;

    return reply.status(httpStatus).send({
      status,
      app: env.APP_NAME,
      version: env.APP_VERSION,
      env: env.ENV,
      db: deps.db,
      redis: deps.redis,
      uptime: Math.floor(process.uptime()),
      timestamp: new Date().toISOString(),
    });
  });

  // ---------- GET /ready ----------
  app.get("/ready", async (_req, reply) => {
    const deps = await checkDependencies();
    const httpStatus = deps.allOk ? 200 : 503;
    return reply.status(httpStatus).send({
      ready: deps.allOk,
      db: deps.db,
      redis: deps.redis,
    });
  });

  // ---------- GET / ----------
  app.get("/", async (_req, reply) => {
    return reply.status(200).send({
      ok: true,
      app: env.APP_NAME,
      version: env.APP_VERSION,
      env: env.ENV,
      endpoints: {
        health: "/health",
        ready: "/ready",
        webapp: "/webapp/*",
        webhookMp: "/webhook/mercadopago",
        webhookTelegram: "/webhook/telegram",
      },
      timestamp: new Date().toISOString(),
    });
  });

  // ---------- HEAD /health (para uptime tools) ----------
  app.head("/health", async (_req, reply) => {
    const deps = await checkDependencies();
    return reply.status(deps.allOk ? 200 : 503).send();
  });
};

// ============================================================
// 📌 EXPORTS
// ============================================================
export default healthRoutes;
