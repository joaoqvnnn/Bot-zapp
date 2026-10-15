/**
 * Larizinha Store — API TypeScript
 * PixService — integração real com o Mercado Pago (PIX).
 *
 * Responsabilidades:
 *   - Criar cobrança PIX (QR Code + Copia e Cola + txid interno)
 *   - Consultar status do pagamento na API do MP
 *   - Marcar como pago (creditar saldo + Transaction + comissão de afiliado)
 *   - Expirar cobranças vencidas
 *   - Consultar cobrança por txid
 */
import { MercadoPagoConfig, Payment } from "mercadopago";

import env from "@/config/env";
import logger from "@/config/logger";
import prisma from "@/config/prisma";

// ============================================================
// 🔧 CLIENTE MERCADO PAGO
// ============================================================
const mpClient = new MercadoPagoConfig({
  accessToken: env.MP_ACCESS_TOKEN,
});

const mpPayment = new Payment(mpClient);

// ============================================================
// 🧮 CONFIGURAÇÕES
// ============================================================
const PIX_EXPIRATION_MINUTES = 30;
const MIN_DEPOSIT = 1;
const MAX_DEPOSIT = 50000;

// ============================================================
// 🧾 TIPOS
// ============================================================
export interface CreatePixInput {
  userId: number;
  amount: number;
  bonus?: number;
  orderId?: number;
}

export interface PixChargeData {
  id: number;
  txid: string;
  userId: number;
  amount: number;
  bonus: number;
  qrCodeBase64: string | null;
  copyPaste: string;
  expiresAt: Date;
  status: "PENDING" | "PAID" | "EXPIRED" | "CANCELED" | "REFUNDED";
  mpPaymentId: string | null;
}

// ============================================================
// 🎯 HELPERS
// ============================================================
function generateTxid(): string {
  // 32 caracteres alfanuméricos maiúsculos (padrão aceito pelo MP)
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
  let out = "";
  for (let i = 0; i < 32; i++) {
    out += chars[Math.floor(Math.random() * chars.length)];
  }
  return out;
}

function isValidAmount(amount: number): boolean {
  return Number.isFinite(amount) && amount >= MIN_DEPOSIT && amount <= MAX_DEPOSIT;
}

// ============================================================
// 💠 CRIAR COBRANÇA PIX
// ============================================================
export async function createPixCharge(
  input: CreatePixInput,
): Promise<PixChargeData> {
  const { userId, amount, bonus = 0, orderId } = input;

  if (!isValidAmount(amount)) {
    throw new Error(
      `Valor inválido. Mínimo R$ ${MIN_DEPOSIT.toFixed(2)} · Máximo R$ ${MAX_DEPOSIT.toFixed(2)}`,
    );
  }

  // ---------- Carrega o usuário ----------
  const user = await prisma.user.findUnique({ where: { id: userId } });
  if (!user) throw new Error("Usuário não encontrado.");

  // ---------- Gera txid ----------
  let txid = generateTxid();
  let attempts = 0;
  while (attempts < 3) {
    const existing = await prisma.pixCharge.findUnique({ where: { txid } });
    if (!existing) break;
    txid = generateTxid();
    attempts++;
  }

  const expiresAt = new Date(Date.now() + PIX_EXPIRATION_MINUTES * 60 * 1000);

  // ---------- Chama a API do MP ----------
  let mpResponse: Awaited<ReturnType<typeof mpPayment.create>>;
  try {
    mpResponse = await mpPayment.create({
      body: {
        transaction_amount: Number(amount.toFixed(2)),
        description: `Recarga ${env.APP_NAME}`,
        payment_method_id: "pix",
        external_reference: txid,
        notification_url: env.MP_WEBHOOK_URL || undefined,
        statement_descriptor: "LARIZINHASTORE",
        date_of_expiration: expiresAt.toISOString(),
        payer: {
          email:
            user.email ||
            `user${Number(user.telegramId)}@larizinha.store`,
          first_name: user.firstName || "Cliente",
          last_name: user.lastName || "Larizinha",
        },
        metadata: {
          user_id: userId,
          txid,
          bonus,
          order_id: orderId ?? null,
        },
      },
    });
  } catch (err) {
    logger.error({ err, userId, amount }, "Falha ao criar PIX no Mercado Pago");
    throw new Error("Erro ao comunicar com o Mercado Pago.");
  }

  const mpPaymentId = String(mpResponse.id ?? "");
  const poi = mpResponse.point_of_interaction?.transaction_data;
  const qrCode = poi?.qr_code ?? null;
  const qrCodeBase64 = poi?.qr_code_base64 ?? null;

  if (!qrCode) {
    logger.error({ mpResponse }, "MP não retornou qr_code");
    throw new Error("Mercado Pago não retornou o QR Code.");
  }

  // ---------- Persiste ----------
  const charge = await prisma.pixCharge.create({
    data: {
      txid,
      userId,
      amount,
      bonus,
      qrCode,
      qrCodeBase64,
      copyPaste: qrCode,
      mpPaymentId,
      status: "PENDING",
      expiresAt,
      orderId: orderId ?? null,
    },
  });

  logger.info(
    { chargeId: charge.id, userId, amount, txid },
    "PIX criado",
  );

  return {
    id: charge.id,
    txid: charge.txid,
    userId: charge.userId,
    amount: Number(charge.amount),
    bonus: Number(charge.bonus),
    qrCodeBase64: charge.qrCodeBase64,
    copyPaste: charge.copyPaste ?? qrCode,
    expiresAt: charge.expiresAt,
    status: "PENDING",
    mpPaymentId,
  };
}

// ============================================================
// 🔎 CONSULTAR COBRANÇA (por txid)
// ============================================================
export async function getPixCharge(txid: string): Promise<PixChargeData | null> {
  const charge = await prisma.pixCharge.findUnique({ where: { txid } });
  if (!charge) return null;

  return {
    id: charge.id,
    txid: charge.txid,
    userId: charge.userId,
    amount: Number(charge.amount),
    bonus: Number(charge.bonus),
    qrCodeBase64: charge.qrCodeBase64,
    copyPaste: charge.copyPaste ?? charge.qrCode ?? "",
    expiresAt: charge.expiresAt,
    status: charge.status as PixChargeData["status"],
    mpPaymentId: charge.mpPaymentId,
  };
}

// ============================================================
// 🔄 SINCRONIZAR COM O MP
// ============================================================
export async function syncPixStatus(txid: string): Promise<PixChargeData | null> {
  const charge = await prisma.pixCharge.findUnique({ where: { txid } });
  if (!charge) return null;

  if (charge.status === "PAID" || !charge.mpPaymentId) {
    return getPixCharge(txid);
  }

  // ---------- Consulta MP ----------
  let mpInfo: Awaited<ReturnType<typeof mpPayment.get>>;
  try {
    mpInfo = await mpPayment.get({ id: charge.mpPaymentId });
  } catch (err) {
    logger.warn({ err, txid }, "Falha ao consultar status no MP");
    return getPixCharge(txid);
  }

  const mpStatus = String(mpInfo.status ?? "").toLowerCase();

  if (mpStatus === "approved" || mpStatus === "authorized") {
    await markPixAsPaid(txid);
  } else if (["cancelled", "refunded", "charged_back"].includes(mpStatus)) {
    await prisma.pixCharge.update({
      where: { txid },
      data: { status: "CANCELED" },
    });
  } else if (mpStatus === "rejected") {
    await prisma.pixCharge.update({
      where: { txid },
      data: { status: "CANCELED" },
    });
  }

  return getPixCharge(txid);
}

// ============================================================
// ✅ MARCAR COMO PAGO (credita saldo + comissão afiliado)
// ============================================================
export async function markPixAsPaid(txid: string): Promise<PixChargeData | null> {
  const charge = await prisma.pixCharge.findUnique({ where: { txid } });
  if (!charge) return null;

  // ---------- Idempotência ----------
  if (charge.status === "PAID") return getPixCharge(txid);

  const totalCredit = Number(charge.amount) + Number(charge.bonus ?? 0);

  await prisma.$transaction(async (tx) => {
    // ---------- Carrega o usuário ----------
    const user = await tx.user.findUnique({ where: { id: charge.userId } });
    if (!user) throw new Error("Usuário não encontrado.");

    const newBalance = Number(user.balance) + totalCredit;

    // ---------- Atualiza saldo ----------
    await tx.user.update({
      where: { id: user.id },
      data: {
        balance: newBalance,
        totalDeposited: Number(user.totalDeposited) + Number(charge.amount),
      },
    });

    // ---------- Marca cobrança como paga ----------
    await tx.pixCharge.update({
      where: { txid },
      data: {
        status: "PAID",
        paidAt: new Date(),
      },
    });

    // ---------- Transaction ----------
    await tx.transaction.create({
      data: {
        userId: user.id,
        type: "DEPOSIT",
        amount: totalCredit,
        balanceAfter: newBalance,
        description:
          `Depósito PIX aprovado (${txid})` +
          (Number(charge.bonus ?? 0) > 0
            ? ` + bônus R$ ${Number(charge.bonus).toFixed(2)}`
            : ""),
        reference: txid,
      },
    });

    // ---------- Comissão de afiliado ----------
    if (user.referredById) {
      const earner = await tx.user.findUnique({
        where: { id: user.referredById },
      });
      if (earner) {
        // Percentual do depósito (default 5%)
        const percent = 5;
        const commissionValue = Number(charge.amount) * (percent / 100);

        await tx.commission.create({
          data: {
            earnerId: earner.id,
            sourceUserId: user.id,
            amount: commissionValue,
            percent,
            type: "DEPOSIT",
            status: "PENDING",
            reference: txid,
          },
        });

        await tx.user.update({
          where: { id: earner.id },
          data: {
            affiliateBalance: Number(earner.affiliateBalance) + commissionValue,
          },
        });
      }
    }
  });

  logger.info({ txid, userId: charge.userId }, "PIX confirmado e saldo creditado");

  return getPixCharge(txid);
}

// ============================================================
// ⏰ EXPIRAR COBRANÇAS VENCIDAS
// ============================================================
export async function expireOldCharges(limit = 200): Promise<number> {
  const now = new Date();

  const expired = await prisma.pixCharge.findMany({
    where: {
      status: "PENDING",
      expiresAt: { lte: now },
    },
    take: limit,
  });

  if (expired.length === 0) return 0;

  await prisma.pixCharge.updateMany({
    where: {
      id: { in: expired.map((c) => c.id) },
    },
    data: { status: "EXPIRED" },
  });

  logger.info({ count: expired.length }, "PIX expirados");
  return expired.length;
}

// ============================================================
// 📊 HEALTHCHECK DO MP
// ============================================================
export async function mercadopagoHealthcheck(): Promise<boolean> {
  try {
    // Chama /users/me para validar o token
    const res = await fetch("https://api.mercadopago.com/users/me", {
      headers: {
        Authorization: `Bearer ${env.MP_ACCESS_TOKEN}`,
      },
    });
    return res.ok;
  } catch {
    return false;
  }
}

// ============================================================
// 🎁 BÔNUS PADRÃO
// ============================================================
export function calculateBonus(amount: number): number {
  // Sem bônus por padrão; ajustável no painel admin
  const BONUS_PERCENT = 0;
  const BONUS_MIN = 50;
  if (amount < BONUS_MIN || BONUS_PERCENT <= 0) return 0;
  return Number((amount * (BONUS_PERCENT / 100)).toFixed(2));
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export default {
  createPixCharge,
  getPixCharge,
  syncPixStatus,
  markPixAsPaid,
  expireOldCharges,
  mercadopagoHealthcheck,
  calculateBonus,
};
