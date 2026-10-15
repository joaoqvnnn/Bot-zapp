/**
 * Larizinha Store — API TypeScript
 * Plugin Rate-Limit — previne abuso de endpoints.
 */
import rateLimit from "@fastify/rate-limit";
import fp from "fastify-plugin";

import { redis } from "@/config/redis";
import { isProduction } from "@/config/env";

async function plugin(
  app: import("fastify").FastifyInstance,
): Promise<void> {
  await app.register(rateLimit, {
    global: true,
    max: isProduction ? 120 : 1000,
    timeWindow: "1 minute",
    cache: 10000,
    allowList: [],
    redis,
    skipOnError: true,
    keyGenerator: (req) => {
      // Prefere ID do usuário (se autenticado), senão IP
      const userId = (req as unknown as { userId?: number }).userId;
      if (userId) return `user:${userId}`;
      return `ip:${req.ip}`;
    },
    errorResponseBuilder: (_req, context) => ({
      ok: false,
      error: {
        code: "RATE_LIMITED",
        message: `Muitas requisições. Aguarde ${Math.ceil(context.ttl / 1000)}s.`,
      },
    }),
  });
}

export default fp(plugin, { name: "rate-limit-plugin" });
