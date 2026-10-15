/**
 * Larizinha Store — API TypeScript
 * Endpoints /webapp/* — consumidos pelo WebApp Next.js.
 *
 * Autenticação: header `X-Telegram-Init-Data` (assinado pelo Telegram).
 * Todas as rotas exigem `app.requireAuth`.
 */
import type { FastifyInstance, FastifyPluginAsync } from "fastify";
import { z } from "zod";

import env from "@/config/env";
import logger from "@/config/logger";
import prisma from "@/config/prisma";
import { redis, keys, lockManager } from "@/config/redis";

import { createPixCharge, getPixCharge } from "@/services/pix-service";

// ============================================================
// 🧩 SCHEMAS (Zod) — validação de body
// ============================================================
const CreatePixSchema = z.object({
  amount: z.coerce.number().positive().min(1).max(50000),
});

const UpdateProfileSchema = z.object({
  whatsapp: z
    .string()
    .regex(/^\d{10,13}$/, "WhatsApp inválido (10-13 dígitos)")
    .optional(),
  email: z.string().email("E-mail inválido").optional(),
});

const SetPasswordSchema = z.object({
  password: z.string().min(6, "Mínimo 6 caracteres").max(64),
  passwordConfirm: z.string().min(6).max(64),
  recoveryEmail: z.string().email("E-mail inválido").optional(),
});

const WithdrawSchema = z.object({
  amount: z.coerce.number().positive().min(20).max(50000),
  pixKey: z.string().min(3).max(200),
  pixKeyType: z.string().min(2).max(40),
  password: z.string().min(4).max(64),
});

const BuySchema = z.object({
  productId: z.coerce.number().int().positive(),
  quantity: z.coerce.number().int().positive().max(100),
  method: z.enum(["BALANCE", "PIX"]).default("BALANCE"),
});

const PaginationSchema = z.object({
  page: z.coerce.number().int().positive().default(1),
  perPage: z.coerce.number().int().positive().max(50).default(20),
});

// ============================================================
// 🧮 HELPERS
// ============================================================
function serializeUser(u: {
  id: number;
  telegramId: bigint;
  firstName: string | null;
  username: string | null;
  photoUrl: string | null;
  balance: unknown;
  balanceLocked: unknown;
  totalSpent: unknown;
  totalDeposited: unknown;
  purchasesCount: number;
  whatsapp: string | null;
  email: string | null;
  affiliateCode: string | null;
  affiliateBalance: unknown;
  affiliateActive: boolean;
  affiliateLevel: string;
  status: string;
  withdrawPassword: string | null;
}) {
  return {
    id: u.id,
    telegramId: Number(u.telegramId),
    firstName: u.firstName,
    username: u.username,
    photoUrl: u.photoUrl,
    balance: Number(u.balance ?? 0),
    balanceLocked: Number(u.balanceLocked ?? 0),
    totalSpent: Number(u.totalSpent ?? 0),
    totalDeposited: Number(u.totalDeposited ?? 0),
    purchasesCount: u.purchasesCount,
    whatsapp: u.whatsapp,
    email: u.email,
    affiliateCode: u.affiliateCode,
    affiliateBalance: Number(u.affiliateBalance ?? 0),
    affiliateActive: u.affiliateActive,
    affiliateLevel: u.affiliateLevel,
    status: u.status,
    hasWithdrawPassword: Boolean(u.withdrawPassword),
  };
}

// ============================================================
// 🛣️ ROTAS
// ============================================================
const webappRoutes: FastifyPluginAsync = async (app: FastifyInstance) => {
  // Todas as rotas exigem auth
  app.addHook("preHandler", async (req, reply) => {
    await app.requireAuth(req, reply);
  });

  // ==========================================================
  // 👤 ME — usuário logado
  // ==========================================================
  app.get("/me", async (req) => {
    const userId = req.authUser!.id;
    const user = await prisma.user.findUnique({ where: { id: userId } });
    if (!user) {
      return {
        ok: false,
        error: { code: "NOT_FOUND", message: "Usuário não encontrado." },
      };
    }
    return { ok: true, data: serializeUser(user) };
  });

  // ==========================================================
  // ✏️ Atualizar perfil (whatsapp / email)
  // ==========================================================
  app.patch("/me", async (req, reply) => {
    const parsed = UpdateProfileSchema.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({
        ok: false,
        error: {
          code: "INVALID_BODY",
          message: parsed.error.issues[0]?.message ?? "Dados inválidos.",
        },
      });
    }

    const userId = req.authUser!.id;
    const updated = await prisma.user.update({
      where: { id: userId },
      data: {
        ...(parsed.data.whatsapp !== undefined
          ? { whatsapp: parsed.data.whatsapp }
          : {}),
        ...(parsed.data.email !== undefined
          ? { email: parsed.data.email }
          : {}),
      },
    });

    return { ok: true, data: serializeUser(updated) };
  });

  // ==========================================================
  // 📂 Categorias
  // ==========================================================
  app.get("/categories", async () => {
    const cats = await prisma.category.findMany({
      where: { visible: true },
      orderBy: [{ order: "asc" }, { id: "asc" }],
    });

    return {
      ok: true,
      data: cats.map((c) => ({
        id: c.id,
        name: c.name,
        slug: c.slug,
        emoji: c.emoji,
        order: c.order,
      })),
    };
  });

  // ==========================================================
  // 📦 Produtos (listagem com filtros)
  // ==========================================================
  app.get("/products", async (req) => {
    const q = req.query as Record<string, string>;
    const pagination = PaginationSchema.safeParse(q);
    const page = pagination.success ? pagination.data.page : 1;
    const perPage = pagination.success ? pagination.data.perPage : 20;

    const categoryId = q.categoryId ? Number(q.categoryId) : undefined;
    const search = q.search?.trim() || undefined;

    const where: Record<string, unknown> = { visible: true };
    if (categoryId) where.categoryId = categoryId;
    if (search) {
      where.OR = [
        { name: { contains: search, mode: "insensitive" } },
        { subtitle: { contains: search, mode: "insensitive" } },
      ];
    }

    const [items, total] = await Promise.all([
      prisma.product.findMany({
        where,
        orderBy: [
          { featured: "desc" },
          { order: "asc" },
          { id: "asc" },
        ],
        skip: (page - 1) * perPage,
        take: perPage,
      }),
      prisma.product.count({ where }),
    ]);

    return {
      ok: true,
      data: items.map((p) => ({
        id: p.id,
        name: p.name,
        subtitle: p.subtitle,
        price: Number(p.price),
        pricePromo: p.pricePromo ? Number(p.pricePromo) : null,
        imageWebapp: p.imageWebapp,
        stock: p.stock,
        unlimited: p.unlimited,
        categoryId: p.categoryId,
        featured: p.featured,
        soldOut: p.soldOut,
      })),
      meta: { page, perPage, total, hasMore: page * perPage < total },
    };
  });

  // ==========================================================
  // 🔍 Produto (detalhe)
  // ==========================================================
  app.get("/products/:id", async (req, reply) => {
    const id = Number((req.params as { id: string }).id);
    if (!Number.isFinite(id) || id <= 0) {
      return reply.status(400).send({
        ok: false,
        error: { code: "INVALID_ID", message: "ID inválido." },
      });
    }

    const product = await prisma.product.findUnique({
      where: { id },
      include: { images: { orderBy: { order: "asc" } } },
    });

    if (!product || !product.visible) {
      return reply.status(404).send({
        ok: false,
        error: { code: "NOT_FOUND", message: "Produto não encontrado." },
      });
    }

    return {
      ok: true,
      data: {
        id: product.id,
        name: product.name,
        subtitle: product.subtitle,
        description: product.description,
        price: Number(product.price),
        pricePromo: product.pricePromo ? Number(product.pricePromo) : null,
        imageWebapp: product.imageWebapp,
        stock: product.stock,
        unlimited: product.unlimited,
        categoryId: product.categoryId,
        featured: product.featured,
        soldOut: product.soldOut,
        warrantyDays: product.warrantyDays,
        expirationDays: product.expirationDays,
        noteProduct: product.noteProduct,
        noteActivation: product.noteActivation,
        activationLink: product.activationLink,
        soldCount: product.soldCount,
        images: product.images.map((img) => img.url),
      },
    };
  });

  // ==========================================================
  // 💠 CARTEIRA — criar PIX
  // ==========================================================
  app.post("/wallet/pix", async (req, reply) => {
    const parsed = CreatePixSchema.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({
        ok: false,
        error: {
          code: "INVALID_BODY",
          message: parsed.error.issues[0]?.message ?? "Valor inválido.",
        },
      });
    }

    const userId = req.authUser!.id;
    const amount = parsed.data.amount;

    try {
      const pix = await createPixCharge({ userId, amount });
      return {
        ok: true,
        data: {
          txid: pix.txid,
          amount: Number(pix.amount),
          bonus: Number(pix.bonus ?? 0),
          qrCodeBase64: pix.qrCodeBase64,
          copyPaste: pix.copyPaste,
          expiresAt: pix.expiresAt.toISOString(),
          status: pix.status,
        },
      };
    } catch (err) {
      logger.error({ err, userId, amount }, "Falha ao criar PIX");
      return reply.status(500).send({
        ok: false,
        error: {
          code: "PIX_FAILED",
          message:
            err instanceof Error
              ? err.message
              : "Não foi possível gerar o PIX.",
        },
      });
    }
  });

  // ==========================================================
  // 🔎 CARTEIRA — verificar PIX
  // ==========================================================
  app.get("/wallet/pix/:txid", async (req, reply) => {
    const txid = (req.params as { txid: string }).txid;
    if (!txid) {
      return reply.status(400).send({
        ok: false,
        error: { code: "INVALID_TXID", message: "TXID inválido." },
      });
    }

    const userId = req.authUser!.id;
    const charge = await getPixCharge(txid);

    if (!charge || charge.userId !== userId) {
      return reply.status(404).send({
        ok: false,
        error: { code: "NOT_FOUND", message: "PIX não encontrado." },
      });
    }

    return {
      ok: true,
      data: {
        txid: charge.txid,
        amount: Number(charge.amount),
        bonus: Number(charge.bonus ?? 0),
        qrCodeBase64: charge.qrCodeBase64,
        copyPaste: charge.copyPaste,
        expiresAt: charge.expiresAt.toISOString(),
        status: charge.status,
      },
    };
  });

  // ==========================================================
  // 📊 TRANSAÇÕES
  // ==========================================================
  app.get("/wallet/transactions", async (req) => {
    const q = PaginationSchema.safeParse(req.query);
    const page = q.success ? q.data.page : 1;
    const perPage = q.success ? q.data.perPage : 20;

    const userId = req.authUser!.id;

    const [items, total] = await Promise.all([
      prisma.transaction.findMany({
        where: { userId },
        orderBy: { createdAt: "desc" },
        skip: (page - 1) * perPage,
        take: perPage,
      }),
      prisma.transaction.count({ where: { userId } }),
    ]);

    return {
      ok: true,
      data: items.map((t) => ({
        id: t.id,
        type: t.type,
        amount: Number(t.amount),
        balanceAfter: Number(t.balanceAfter),
        description: t.description,
        reference: t.reference,
        createdAt: t.createdAt.toISOString(),
      })),
      meta: { page, perPage, total, hasMore: page * perPage < total },
    };
  });

  // ==========================================================
  // 🛒 PEDIDOS
  // ==========================================================
  app.get("/orders", async (req) => {
    const q = PaginationSchema.safeParse(req.query);
    const page = q.success ? q.data.page : 1;
    const perPage = q.success ? q.data.perPage : 20;

    const userId = req.authUser!.id;

    const [items, total] = await Promise.all([
      prisma.order.findMany({
        where: { userId },
        orderBy: { createdAt: "desc" },
        skip: (page - 1) * perPage,
        take: perPage,
        include: { items: true },
      }),
      prisma.order.count({ where: { userId } }),
    ]);

    return {
      ok: true,
      data: items.map((o) => ({
        id: o.id,
        status: o.status,
        method: o.method,
        finalTotal: Number(o.finalTotal),
        createdAt: o.createdAt.toISOString(),
        deliveredAt: o.deliveredAt?.toISOString() ?? null,
        expiresAt: o.expiresAt?.toISOString() ?? null,
        items: o.items.map((it) => ({
          id: it.id,
          productName: it.productName,
          quantity: it.quantity,
          unitPrice: Number(it.unitPrice),
          total: Number(it.total),
        })),
      })),
      meta: { page, perPage, total, hasMore: page * perPage < total },
    };
  });

  app.get("/orders/:id", async (req, reply) => {
    const id = Number((req.params as { id: string }).id);
    if (!Number.isFinite(id)) {
      return reply.status(400).send({
        ok: false,
        error: { code: "INVALID_ID", message: "ID inválido." },
      });
    }

    const userId = req.authUser!.id;
    const order = await prisma.order.findFirst({
      where: { id, userId },
      include: { items: true },
    });

    if (!order) {
      return reply.status(404).send({
        ok: false,
        error: { code: "NOT_FOUND", message: "Pedido não encontrado." },
      });
    }

    return {
      ok: true,
      data: {
        id: order.id,
        status: order.status,
        method: order.method,
        finalTotal: Number(order.finalTotal),
        createdAt: order.createdAt.toISOString(),
        deliveredAt: order.deliveredAt?.toISOString() ?? null,
        expiresAt: order.expiresAt?.toISOString() ?? null,
        items: order.items.map((it) => ({
          id: it.id,
          productName: it.productName,
          quantity: it.quantity,
          unitPrice: Number(it.unitPrice),
          total: Number(it.total),
          credentials: it.credentials,
        })),
      },
    };
  });

  // ==========================================================
  // 🛍️ COMPRAR
  // ==========================================================
  app.post("/buy", async (req, reply) => {
    const parsed = BuySchema.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({
        ok: false,
        error: {
          code: "INVALID_BODY",
          message: parsed.error.issues[0]?.message ?? "Dados inválidos.",
        },
      });
    }

    const userId = req.authUser!.id;
    const { productId, quantity } = parsed.data;

    // ---------- Trava de concorrência ----------
    const lock = await lockManager.reserveStock(productId, userId, quantity);
    if (!lock) {
      return reply.status(409).send({
        ok: false,
        error: {
          code: "LOCKED",
          message: "Este produto está sendo comprado agora. Tente novamente.",
        },
      });
    }

    try {
      const result = await prisma.$transaction(async (tx) => {
        const product = await tx.product.findUnique({
          where: { id: productId },
        });
        if (!product || !product.visible) {
          throw new Error("Produto não encontrado.");
        }
        if (!product.unlimited && product.stock < quantity) {
          throw new Error("Estoque insuficiente.");
        }

        const unitPrice = Number(product.pricePromo ?? product.price);
        const total = unitPrice * quantity;

        const user = await tx.user.findUnique({ where: { id: userId } });
        if (!user) throw new Error("Usuário não encontrado.");
        const balance = Number(user.balance);
        if (balance < total) throw new Error("Saldo insuficiente.");

        // Debita
        const newBalance = balance - total;
        await tx.user.update({
          where: { id: userId },
          data: {
            balance: newBalance,
            totalSpent: Number(user.totalSpent) + total,
            purchasesCount: user.purchasesCount + 1,
          },
        });

        // Cria pedido
        const order = await tx.order.create({
          data: {
            userId,
            total,
            finalTotal: total,
            method: "BALANCE",
            status: "DELIVERED",
            deliveredAt: new Date(),
            items: {
              create: {
                productId: product.id,
                productName: product.name,
                unitPrice,
                quantity,
                total,
                credentials: [],
              },
            },
          },
          include: { items: true },
        });

        // Baixa estoque
        if (!product.unlimited) {
          await tx.product.update({
            where: { id: productId },
            data: {
              stock: product.stock - quantity,
              soldCount: product.soldCount + quantity,
            },
          });
        } else {
          await tx.product.update({
            where: { id: productId },
            data: { soldCount: product.soldCount + quantity },
          });
        }

        // Transação
        await tx.transaction.create({
          data: {
            userId,
            type: "PURCHASE",
            amount: total,
            balanceAfter: newBalance,
            description: `Compra: ${quantity}x ${product.name}`,
            reference: `order:${order.id}`,
          },
        });

        return order;
      });

      // Libera a trava após sucesso
      await lockManager.release(productId, userId);

      return {
        ok: true,
        data: {
          id: result.id,
          status: result.status,
          method: result.method,
          finalTotal: Number(result.finalTotal),
          createdAt: result.createdAt.toISOString(),
          deliveredAt: result.deliveredAt?.toISOString() ?? null,
          expiresAt: result.expiresAt?.toISOString() ?? null,
          items: result.items.map((it) => ({
            id: it.id,
            productName: it.productName,
            quantity: it.quantity,
            unitPrice: Number(it.unitPrice),
            total: Number(it.total),
          })),
        },
      };
    } catch (err) {
      // Libera a trava em caso de erro
      await lockManager.release(productId, userId);
      logger.error({ err, userId, productId }, "Falha na compra");

      return reply.status(400).send({
        ok: false,
        error: {
          code: "BUY_FAILED",
          message: err instanceof Error ? err.message : "Falha ao comprar.",
        },
      });
    }
  });

  // ==========================================================
  // 🔐 SENHA DE SAQUE
  // ==========================================================
  app.get("/password/has", async (req) => {
    const userId = req.authUser!.id;
    const user = await prisma.user.findUnique({
      where: { id: userId },
      select: { withdrawPassword: true },
    });
    return { ok: true, data: { has: Boolean(user?.withdrawPassword) } };
  });

  app.post("/password/set", async (req, reply) => {
    const parsed = SetPasswordSchema.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({
        ok: false,
        error: {
          code: "INVALID_BODY",
          message: parsed.error.issues[0]?.message ?? "Dados inválidos.",
        },
      });
    }

    const { password, passwordConfirm, recoveryEmail } = parsed.data;
    if (password !== passwordConfirm) {
      return reply.status(400).send({
        ok: false,
        error: { code: "MISMATCH", message: "Senhas não coincidem." },
      });
    }

    const userId = req.authUser!.id;
    const bcrypt = await import("bcryptjs");
    const hash = await bcrypt.hash(password, 10);

    await prisma.user.update({
      where: { id: userId },
      data: {
        withdrawPassword: hash,
        recoveryEmail: recoveryEmail ?? undefined,
      },
    });

    return { ok: true, data: { ok: true } };
  });

  app.post("/password/forgot", async (req, reply) => {
    const parsed = z
      .object({ email: z.string().email() })
      .safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({
        ok: false,
        error: { code: "INVALID_BODY", message: "E-mail inválido." },
      });
    }

    // Implementação futura: enviar e-mail com token
    return { ok: true, data: { ok: true } };
  });

  app.post("/password/reset", async (req, reply) => {
    return reply.status(501).send({
      ok: false,
      error: {
        code: "NOT_IMPLEMENTED",
        message: "Reset por e-mail será implementado em breve.",
      },
    });
  });

  // ==========================================================
  // 💸 SAQUE
  // ==========================================================
  app.post("/withdraw", async (req, reply) => {
    const parsed = WithdrawSchema.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({
        ok: false,
        error: {
          code: "INVALID_BODY",
          message: parsed.error.issues[0]?.message ?? "Dados inválidos.",
        },
      });
    }

    const { amount, pixKey, pixKeyType, password } = parsed.data;
    const userId = req.authUser!.id;

    try {
      const result = await prisma.$transaction(async (tx) => {
        const user = await tx.user.findUnique({ where: { id: userId } });
        if (!user) throw new Error("Usuário não encontrado.");
        if (!user.withdrawPassword) {
          throw new Error("Cadastre uma senha de saque primeiro.");
        }

        const bcrypt = await import("bcryptjs");
        const valid = await bcrypt.compare(password, user.withdrawPassword);
        if (!valid) throw new Error("Senha incorreta.");

        const balance = Number(user.balance);
        if (balance < amount) throw new Error("Saldo insuficiente.");

        const newBalance = balance - amount;
        await tx.user.update({
          where: { id: userId },
          data: {
            balance: newBalance,
            totalWithdrawn: Number(user.totalWithdrawn) + amount,
          },
        });

        const withdraw = await tx.withdraw.create({
          data: {
            userId,
            amount,
            fee: 0,
            netAmount: amount,
            pixKey,
            pixKeyType,
            status: "PENDING",
          },
        });

        await tx.transaction.create({
          data: {
            userId,
            type: "WITHDRAW",
            amount,
            balanceAfter: newBalance,
            description: `Saque PIX · ${pixKeyType}`,
            reference: `withdraw:${withdraw.id}`,
          },
        });

        return withdraw;
      });

      return {
        ok: true,
        data: {
          id: result.id,
          amount: Number(result.amount),
          netAmount: Number(result.netAmount),
          pixKey: result.pixKey,
          pixKeyType: result.pixKeyType,
          status: result.status,
          createdAt: result.createdAt.toISOString(),
          processedAt: null,
        },
      };
    } catch (err) {
      logger.error({ err, userId }, "Falha no saque");
      return reply.status(400).send({
        ok: false,
        error: {
          code: "WITHDRAW_FAILED",
          message: err instanceof Error ? err.message : "Falha no saque.",
        },
      });
    }
  });

  app.get("/withdraw/history", async (req) => {
    const q = PaginationSchema.safeParse(req.query);
    const page = q.success ? q.data.page : 1;
    const perPage = q.success ? q.data.perPage : 20;

    const userId = req.authUser!.id;

    const [items, total] = await Promise.all([
      prisma.withdraw.findMany({
        where: { userId },
        orderBy: { createdAt: "desc" },
        skip: (page - 1) * perPage,
        take: perPage,
      }),
      prisma.withdraw.count({ where: { userId } }),
    ]);

    return {
      ok: true,
      data: items.map((w) => ({
        id: w.id,
        amount: Number(w.amount),
        netAmount: Number(w.netAmount),
        pixKey: w.pixKey,
        pixKeyType: w.pixKeyType,
        status: w.status,
        createdAt: w.createdAt.toISOString(),
        processedAt: w.processedAt?.toISOString() ?? null,
      })),
      meta: { page, perPage, total, hasMore: page * perPage < total },
    };
  });

  // ==========================================================
  // 👥 AFILIADOS
  // ==========================================================
  app.get("/affiliate/stats", async (req) => {
    const userId = req.authUser!.id;

    const [user, refCount, commissions] = await Promise.all([
      prisma.user.findUnique({ where: { id: userId } }),
      prisma.user.count({ where: { referredById: userId } }),
      prisma.commission.aggregate({
        where: {
          earnerId: userId,
          status: { in: ["APPROVED", "PAID"] },
        },
        _sum: { amount: true },
      }),
    ]);

    return {
      ok: true,
      data: {
        active: Boolean(user?.affiliateActive),
        code: user?.affiliateCode ?? null,
        level: user?.affiliateLevel ?? "iniciante",
        balance: Number(user?.affiliateBalance ?? 0),
        referrals: refCount,
        totalEarned: Number(commissions._sum.amount ?? 0),
        link: user?.affiliateCode
          ? `https://t.me/${env.BOT_USERNAME}?start=ref_${Number(user.telegramId)}`
          : null,
      },
    };
  });

  // ==========================================================
  // 🎁 GIFT CARD — resgatar
  // ==========================================================
  app.post("/gift/redeem", async (req, reply) => {
    const parsed = z
      .object({ code: z.string().min(3).max(40) })
      .safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({
        ok: false,
        error: { code: "INVALID_BODY", message: "Código inválido." },
      });
    }

    const userId = req.authUser!.id;
    const code = parsed.data.code.toUpperCase();

    try {
      const result = await prisma.$transaction(async (tx) => {
        const gift = await tx.giftCard.findUnique({ where: { code } });
        if (!gift || !gift.active) throw new Error("Gift Card inválido.");
        if (gift.expiresAt && gift.expiresAt < new Date()) {
          throw new Error("Gift Card expirado.");
        }
        if (gift.usesMax > 0 && gift.usesCount >= gift.usesMax) {
          throw new Error("Gift Card já esgotado.");
        }

        const already = await tx.giftCardRedemption.findFirst({
          where: { giftCardId: gift.id, userId },
        });
        if (already) throw new Error("Você já resgatou este Gift Card.");

        const amount = Number(gift.amount);
        const user = await tx.user.findUnique({ where: { id: userId } });
        if (!user) throw new Error("Usuário não encontrado.");

        const newBalance = Number(user.balance) + amount;
        await tx.user.update({
          where: { id: userId },
          data: {
            balance: newBalance,
            totalGifts: Number(user.totalGifts) + amount,
          },
        });

        await tx.giftCardRedemption.create({
          data: { giftCardId: gift.id, userId, amount },
        });

        await tx.giftCard.update({
          where: { id: gift.id },
          data: { usesCount: gift.usesCount + 1 },
        });

        await tx.transaction.create({
          data: {
            userId,
            type: "GIFT",
            amount,
            balanceAfter: newBalance,
            description: `Gift Card resgatado (${code})`,
            reference: code,
          },
        });

        return { amount };
      });

      return { ok: true, data: { amount: result.amount } };
    } catch (err) {
      logger.warn({ err, userId, code }, "Falha ao resgatar gift");
      return reply.status(400).send({
        ok: false,
        error: {
          code: "GIFT_FAILED",
          message: err instanceof Error ? err.message : "Gift Card inválido.",
        },
      });
    }
  });

  // ==========================================================
  // 💚 HEALTH INTERNO
  // ==========================================================
  app.get("/health", async () => {
    return { ok: true, data: { ok: true } };
  });
};

// ============================================================
// 📌 EXPORTS
// ============================================================
export default webappRoutes;
