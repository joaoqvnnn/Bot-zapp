/**
 * Larizinha Store — API TypeScript
 * Cliente Redis (ioredis) + LockManager (travas de concorrência).
 */
import Redis, { type Redis as RedisType } from "ioredis";

import env, { isProduction } from "./env";
import logger from "./logger";

// ============================================================
// 🧩 SINGLETON
// ============================================================
const globalForRedis = globalThis as unknown as {
  __redis: RedisType | undefined;
};

function createClient(): RedisType {
  const client = new Redis(env.REDIS_URL, {
    keyPrefix: "", // usamos prefix manual via helper
    maxRetriesPerRequest: 3,
    enableReadyCheck: true,
    lazyConnect: false,
    retryStrategy(times) {
      const delay = Math.min(times * 200, 3000);
      logger.warn({ times, delay }, "🔁 Redis reconectando...");
      return delay;
    },
    reconnectOnError(err) {
      const targetErrors = ["READONLY", "ECONNRESET", "ETIMEDOUT"];
      return targetErrors.some((t) => err.message.includes(t));
    },
  });

  client.on("connect", () => {
    logger.info("⚡ Redis conectado.");
  });

  client.on("ready", () => {
    logger.info("⚡ Redis pronto.");
  });

  client.on("error", (err) => {
    logger.error({ err }, "⚡ Redis erro");
  });

  client.on("close", () => {
    logger.warn("⚡ Redis conexão fechada.");
  });

  if (!isProduction) {
    client.on("reconnecting", () => {
      logger.debug("⚡ Redis reconectando...");
    });
  }

  return client;
}

export const redis: RedisType = globalForRedis.__redis ?? createClient();

if (!isProduction) {
  globalForRedis.__redis = redis;
}

// ============================================================
// 🔑 HELPERS DE CHAVE
// ============================================================
export const keys = {
  prefix: env.REDIS_PREFIX,
  productLock: (pid: number | string) => `${env.REDIS_PREFIX}:product:${pid}:lock`,
  userView: (uid: number | string) => `${env.REDIS_PREFIX}:user:${uid}:view`,
  cartPending: `${env.REDIS_PREFIX}:cart:pending`,
  cartItem: (uid: number | string) => `${env.REDIS_PREFIX}:cart:item:${uid}`,
  tempBlock: (uid: number | string) => `${env.REDIS_PREFIX}:security:tempblock:${uid}`,
  apiRequests: `${env.REDIS_PREFIX}:api:requests`,
  /** Qualquer chave customizada ganha o prefixo */
  custom: (suffix: string) => `${env.REDIS_PREFIX}:${suffix}`,
} as const;

// ============================================================
// 🔒 LOCK MANAGER
// ============================================================
export const lockManager = {
  /**
   * Reserva uma trava de estoque para um produto.
   * Retorna o token (string) se conseguiu; null se já estava travado.
   */
  async reserveStock(
    productId: number,
    userId: number,
    quantity = 1,
    ttlSec = env.PRODUCT_LOCK_TTL,
  ): Promise<string | null> {
    const key = keys.productLock(productId);
    const token = `${userId}:${Date.now()}:${Math.random().toString(36).slice(2, 10)}`;
    const payload = JSON.stringify({
      token,
      user_id: userId,
      quantity,
      created_at: Math.floor(Date.now() / 1000),
    });

    const ok = await redis.set(key, payload, "EX", ttlSec, "NX");
    if (ok === "OK") return token;

    // Já existe — verifica se é do mesmo usuário (reentrada)
    const existing = await redis.get(key);
    if (existing) {
      try {
        const data = JSON.parse(existing) as { user_id: number; token: string };
        if (Number(data.user_id) === userId) return data.token;
      } catch {
        return null;
      }
    }
    return null;
  },

  async release(productId: number, userId?: number): Promise<boolean> {
    const key = keys.productLock(productId);

    if (userId === undefined) {
      const deleted = await redis.del(key);
      return deleted > 0;
    }

    const raw = await redis.get(key);
    if (!raw) return false;

    try {
      const data = JSON.parse(raw) as { user_id: number };
      if (Number(data.user_id) !== userId) return false;
      const deleted = await redis.del(key);
      return deleted > 0;
    } catch {
      return false;
    }
  },

  async markView(userId: number, productId: number, ttlSec = 120): Promise<void> {
    await redis.set(keys.userView(userId), String(productId), "EX", ttlSec);
  },

  async clearView(userId: number): Promise<void> {
    await redis.del(keys.userView(userId));
  },

  async tempBlock(userId: number, seconds = 900, reason = "abuse"): Promise<void> {
    await redis.set(keys.tempBlock(userId), reason, "EX", seconds);
  },

  async isTempBlocked(userId: number): Promise<boolean> {
    const ttl = await redis.ttl(keys.tempBlock(userId));
    return ttl > 0;
  },

  async unblock(userId: number): Promise<void> {
    await redis.del(keys.tempBlock(userId));
  },
};

// ============================================================
// 🌱 CONEXÃO / DESCONEXÃO
// ============================================================
export async function connectRedis(): Promise<void> {
  if (redis.status === "ready" || redis.status === "connecting") return;
  await redis.connect().catch(() => {
    /* já logado via eventos */
  });
}

export async function disconnectRedis(): Promise<void> {
  try {
    await redis.quit();
    logger.info("⚡ Redis desconectado.");
  } catch (err) {
    logger.warn({ err }, "Erro ao desconectar Redis");
  }
}

export async function redisHealthcheck(): Promise<boolean> {
  try {
    const pong = await redis.ping();
    return pong === "PONG";
  } catch {
    return false;
  }
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export default redis;
