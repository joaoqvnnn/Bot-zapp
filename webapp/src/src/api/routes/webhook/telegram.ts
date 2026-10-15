/**
 * Larizinha Store — API TypeScript
 * Webhook do Telegram — recebe updates e repassa para o bot Python.
 *
 * Estratégia:
 *   O bot Python (Aiogram) já registra seu PRÓPRIO webhook direto no Telegram.
 *   Este arquivo serve como FALLBACK/ponte caso o webhook do bot aponte para a API.
 *
 *   → Se o bot já estiver com webhook direto, este endpoint apenas responde 200.
 */
import type { FastifyInstance, FastifyPluginAsync } from "fastify";

import env from "@/config/env";
import logger from "@/config/logger";

const webhookTelegramRoutes: FastifyPluginAsync = async (
  app: FastifyInstance,
) => {
  // ---------- POST /webhook/telegram ----------
  app.post("/telegram", async (req, reply) => {
    // Verifica o secret token
    const secret = req.headers["x-telegram-bot-api-secret-token"];
    if (
      env.WEBHOOK_SECRET_TOKEN &&
      secret !== env.WEBHOOK_SECRET_TOKEN
    ) {
      logger.warn(
        { ip: req.ip },
        "Webhook Telegram com secret inválido",
      );
      return reply.status(403).send({ ok: false });
    }

    const update = req.body as Record<string, unknown> | undefined;
    logger.debug({ update }, "Update do Telegram recebido na API");

    // O bot Python responde diretamente pelo próprio webhook.
    // Aqui apenas confirmamos a recepção para o Telegram não reenviar.
    return reply.status(200).send({ ok: true });
  });

  // ---------- GET /webhook/telegram (healthcheck) ----------
  app.get("/telegram", async () => {
    return { ok: true, message: "Webhook Telegram ativo (ponte)." };
  });
};

export default webhookTelegramRoutes;
