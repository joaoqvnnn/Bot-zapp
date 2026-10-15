/**
 * Larizinha Store — API TypeScript
 * Plugin de autenticação via Telegram `initData`.
 *
 * O WebApp envia o header `X-Telegram-Init-Data` com o `initData` assinado.
 * Este plugin:
 *   - Valida a assinatura HMAC-SHA256
 *   - Busca o usuário no Postgres (por telegram_id)
 *   - Bloqueia usuário BANNED/BLOCKED
 *   - Injeta `req.user` (User do Prisma) + `req.telegramUser`
 */
import fp from "fastify-plugin";
import type {
  FastifyInstance,
  FastifyPluginAsync,
  FastifyReply,
  FastifyRequest,
} from "fastify";

import env from "@/config/env";
import logger from "@/config/logger";
import prisma from "@/config/prisma";
import { getVerifiedUser, type TelegramInitUser } from "@/utils/telegram-auth";

// ============================================================
// 🧩 TIPAGEM
// ============================================================
export interface AuthUser {
  id: number;
  telegramId: bigint;
  username: string | null;
  firstName: string | null;
  lastName: string | null;
  status: string;
  balance: unknown;
  balanceLocked: unknown;
  affiliateActive: boolean;
  affiliateCode: string | null;
}

declare module "fastify" {
  interface FastifyRequest {
    authUser?: AuthUser;
    telegramUser?: TelegramInitUser;
  }

  interface FastifyInstance {
    /** Exige autenticação; retorna 401 se falhar. */
    requireAuth: (req: FastifyRequest, reply: FastifyReply) => Promise<void>;
    /** Tenta autenticar, mas não falha se não tiver initData. */
    tryAuth: (req: FastifyRequest, reply: FastifyReply) => Promise<void>;
  }
}

// ============================================================
// 🎯 HELPER: extrai initData do header ou do cookie
// ============================================================
function extractInitData(req: FastifyRequest): string | null {
  const header = req.headers["x-telegram-init-data"];
  if (typeof header === "string" && header.length > 0) return header;

  // Fallback: header em lowercase alternativo
  const alt = req.headers["X-Telegram-Init-Data" as keyof typeof req.headers];
  if (typeof alt === "string" && alt.length > 0) return alt;

  // Fallback: cookie (usado em alguns fluxos)
  const cookie = (req.cookies as Record<string, string> | undefined)
    ?.tg_init_data;
  if (cookie && cookie.length > 0) return cookie;

  // Fallback: query string (?initData=...)
  const query = req.query as Record<string, string> | undefined;
  if (query?.initData && query.initData.length > 0) return query.initData;

  return null;
}

// ============================================================
// 🔐 CORE: carrega o usuário a partir do initData
// ============================================================
async function loadUserFromInitData(
  req: FastifyRequest,
): Promise<{ ok: boolean; reason?: string }> {
  const initData = extractInitData(req);

  if (!initData) {
    return { ok: false, reason: "MISSING_INIT_DATA" };
  }

  // ---------- Valida assinatura ----------
  const tgUser = getVerifiedUser(
    initData,
    env.BOT_TOKEN,
    env.WEBAPP_AUTH_MAX_AGE,
  );

  if (!tgUser) {
    logger.warn(
      { ip: req.ip, url: req.url },
      "initData com assinatura inválida ou expirada",
    );
    return { ok: false, reason: "INVALID_SIGNATURE" };
  }

  // ---------- Busca no banco ----------
  let user = await prisma.user.findUnique({
    where: { telegramId: BigInt(tgUser.id) },
  });

  // ---------- Cria se não existir ----------
  if (!user) {
    try {
      user = await prisma.user.create({
        data: {
          telegramId: BigInt(tgUser.id),
          username: tgUser.username ?? null,
          firstName: tgUser.first_name ?? null,
          lastName: tgUser.last_name ?? null,
          languageCode: tgUser.language_code ?? null,
          isPremium: Boolean(tgUser.is_premium),
          photoUrl: tgUser.photo_url ?? null,
          status: "ACTIVE",
          lastSeenAt: new Date(),
        },
      });
      logger.info(
        { userId: user.id, telegramId: tgUser.id },
        "Novo usuário criado via WebApp",
      );
    } catch (err) {
      logger.error({ err }, "Falha ao criar usuário via WebApp");
      return { ok: false, reason: "CREATE_FAILED" };
    }
  } else {
    // ---------- Atualiza dados do Telegram (leve) ----------
    try {
      const shouldUpdate =
        user.username !== (tgUser.username ?? null) ||
        user.firstName !== (tgUser.first_name ?? null) ||
        user.lastName !== (tgUser.last_name ?? null) ||
        user.photoUrl !== (tgUser.photo_url ?? null);

      if (shouldUpdate) {
        user = await prisma.user.update({
          where: { id: user.id },
          data: {
            username: tgUser.username ?? null,
            firstName: tgUser.first_name ?? null,
            lastName: tgUser.last_name ?? null,
            photoUrl: tgUser.photo_url ?? null,
            lastSeenAt: new Date(),
          },
        });
      } else {
        // Ainda atualiza lastSeen
        await prisma.user.update({
          where: { id: user.id },
          data: { lastSeenAt: new Date() },
        });
      }
    } catch (err) {
      logger.warn({ err }, "Falha ao atualizar dados do usuário via WebApp");
    }
  }

  // ---------- Verifica status ----------
  if (user.status === "BANNED") {
    return { ok: false, reason: "USER_BANNED" };
  }
  if (user.status === "BLOCKED") {
    return { ok: false, reason: "USER_BLOCKED" };
  }

  // ---------- Injeta no request ----------
  req.telegramUser = tgUser;
  req.authUser = {
    id: user.id,
    telegramId: user.telegramId,
    username: user.username,
    firstName: user.firstName,
    lastName: user.lastName,
    status: user.status,
    balance: user.balance,
    balanceLocked: user.balanceLocked,
    affiliateActive: user.affiliateActive,
    affiliateCode: user.affiliateCode,
  };

  return { ok: true };
}

// ============================================================
// 🧩 PLUGIN
// ============================================================
const authPlugin: FastifyPluginAsync = async (app: FastifyInstance) => {
  // ---------- requireAuth ----------
  app.decorate(
    "requireAuth",
    async (req: FastifyRequest, reply: FastifyReply): Promise<void> => {
      const result = await loadUserFromInitData(req);

      if (!result.ok) {
        const messages: Record<string, string> = {
          MISSING_INIT_DATA:
            "Abra esta tela novamente através do bot no Telegram.",
          INVALID_SIGNATURE:
            "Sessão expirada. Abra o WebApp novamente pelo Telegram.",
          USER_BANNED: "Sua conta foi banida.",
          USER_BLOCKED: "Sua conta está bloqueada.",
          CREATE_FAILED: "Falha ao carregar sua conta. Tente novamente.",
        };

        const status = result.reason === "USER_BANNED" ? 403 : 401;

        reply.status(status).send({
          ok: false,
          error: {
            code: result.reason ?? "UNAUTHORIZED",
            message:
              messages[result.reason ?? "INVALID_SIGNATURE"] ??
              "Não autorizado.",
          },
        });
        return reply;
      }
    },
  );

  // ---------- tryAuth ----------
  app.decorate(
    "tryAuth",
    async (req: FastifyRequest, _reply: FastifyReply): Promise<void> => {
      await loadUserFromInitData(req);
    },
  );

  // ---------- Hook opcional: autentica automaticamente se houver header ----------
  app.addHook("onRequest", async (req) => {
    const initData = extractInitData(req);
    if (!initData) return;

    // Só tenta carregar se o header existir; NÃO bloqueia
    const result = await loadUserFromInitData(req);
    if (!result.ok) {
      logger.debug(
        { reason: result.reason, url: req.url },
        "Autenticação opcional falhou (rota pública?)",
      );
    }
  });
};

// ============================================================
// 📌 EXPORTS
// ============================================================
export default fp(authPlugin, { name: "auth-plugin" });
export { loadUserFromInitData };
