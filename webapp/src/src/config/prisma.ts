/**
 * Larizinha Store — API TypeScript
 * Prisma Client singleton (evita reconexões no hot-reload).
 */
import { PrismaClient } from "@prisma/client";

import env, { isProduction } from "./env";
import logger from "./logger";

// ============================================================
// 🧩 SINGLETON
// ============================================================
const globalForPrisma = globalThis as unknown as {
  __prisma: PrismaClient | undefined;
};

function createClient(): PrismaClient {
  const client = new PrismaClient({
    log: isProduction
      ? [
          { emit: "event", level: "error" },
          { emit: "event", level: "warn" },
        ]
      : [
          { emit: "event", level: "query" },
          { emit: "event", level: "error" },
          { emit: "event", level: "warn" },
        ],
    errorFormat: isProduction ? "minimal" : "pretty",
  });

  // ---------- Eventos ----------
  client.$on("error" as never, (e: unknown) => {
    logger.error({ type: "prisma-error", e }, "Erro no Prisma");
  });

  client.$on("warn" as never, (e: unknown) => {
    logger.warn({ type: "prisma-warn", e }, "Aviso no Prisma");
  });

  if (!isProduction) {
    client.$on("query" as never, (e: unknown) => {
      const ev = e as { duration: number; query: string };
      logger.debug(
        { type: "prisma-query", durationMs: ev.duration },
        ev.query.replace(/\s+/g, " ").slice(0, 400),
      );
    });
  }

  return client;
}

export const prisma: PrismaClient = globalForPrisma.__prisma ?? createClient();

if (!isProduction) {
  globalForPrisma.__prisma = prisma;
}

// ============================================================
// 🌱 CONEXÃO + DESCONEXÃO
// ============================================================
export async function connectPrisma(): Promise<void> {
  try {
    await prisma.$connect();
    logger.info("🗄️  Prisma conectado ao PostgreSQL.");
  } catch (err) {
    logger.error({ err }, "Falha ao conectar no PostgreSQL via Prisma");
    throw err;
  }
}

export async function disconnectPrisma(): Promise<void> {
  try {
    await prisma.$disconnect();
    logger.info("🗄️  Prisma desconectado.");
  } catch (err) {
    logger.warn({ err }, "Erro ao desconectar Prisma");
  }
}

export async function prismaHealthcheck(): Promise<boolean> {
  try {
    await prisma.$queryRaw`SELECT 1`;
    return true;
  } catch {
    return false;
  }
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export default prisma;
