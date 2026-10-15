/**
 * Larizinha Store — API TypeScript
 * Webhook do Mercado Pago — recebe notificações de pagamento.
 *
 * URL pública para configurar no painel do MP:
 *   https://sua-api.onrender.com/webhook/mercadopago
 *
 * O MP envia POST com { type: "payment", data: { id: "123..." } }.
 * Consultamos o pagamento na API e marcamos como pago.
 */
import type { FastifyInstance, FastifyPluginAsync } from "fastify";

import logger from "@/config/logger";
import { syncPixStatus } from "@/services/pix-service";

// ============================================================
// 🛣️ ROTAS
// ============================================================
const webhookMpRoutes: FastifyPluginAsync = async (app: FastifyInstance) => {
  // ---------- POST /webhook/mercadopago ----------
  app.post("/mercadopago", async (req, reply) => {
    const body = req.body as Record<string, unknown> | undefined;

    logger.info(
      { body, query: req.query, ip: req.ip },
      "Webhook Mercado Pago recebido",
    );

    try {
      // O MP envia diferentes formatos:
      // 1) { type: "payment", data: { id: "123" } }
      // 2) { action: "payment.created", data: { id: "123" } }
      // 3) Query string: ?type=payment&data.id=123
      const query = req.query as Record<string, string>;
      const type = String(body?.type ?? query?.type ?? "").toLowerCase();
      const dataId =
        (body?.data as { id?: string } | undefined)?.id ??
        query?.["data.id"] ??
        query?.id;

      if (type !== "payment" || !dataId) {
        // Ignora outros tipos (merchant_order etc.)
        return reply.status(200).send({ received: true, ignored: true });
      }

      // ---------- Busca o pagamento no MP ----------
      const { default: env } = await import("@/config/env");
      const res = await fetch(
        `https://api.mercadopago.com/v1/payments/${dataId}`,
        {
          headers: {
            Authorization: `Bearer ${env.MP_ACCESS_TOKEN}`,
          },
        },
      );

      if (!res.ok) {
        logger.warn(
          { dataId, status: res.status },
          "Falha ao consultar pagamento no MP",
        );
        // Retorna 200 mesmo assim para o MP não ficar reenviando
        return reply.status(200).send({ received: true, error: "fetch_failed" });
      }

      const mpPayment = (await res.json()) as {
        id: number;
        status: string;
        external_reference?: string;
      };

      const txid = mpPayment.external_reference;
      if (!txid) {
        logger.warn({ mpPayment }, "Pagamento MP sem external_reference");
        return reply.status(200).send({ received: true, error: "no_txid" });
      }

      // ---------- Sincroniza o status ----------
      await syncPixStatus(txid);

      logger.info({ txid, mpStatus: mpPayment.status }, "Webhook MP processado");

      return reply.status(200).send({ received: true });
    } catch (err) {
      logger.error({ err }, "Erro ao processar webhook MP");
      // Sempre responde 200 para o MP não reenviar
      return reply.status(200).send({ received: true, error: "internal" });
    }
  });

  // ---------- GET /webhook/mercadopago (healthcheck) ----------
  app.get("/mercadopago", async () => {
    return { ok: true, message: "Webhook Mercado Pago ativo." };
  });
};

// ============================================================
// 📌 EXPORTS
// ============================================================
export default webhookMpRoutes;
