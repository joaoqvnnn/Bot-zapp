/**
 * Larizinha Store — WebApp Next.js
 * Hooks SWR customizados para consumir a API do WebApp.
 *
 * Cada hook:
 * - Traz dados tipados
 * - Expõe { data, error, isLoading, isValidating, mutate }
 * - Usa revalidação condicional (foco, intervalo quando relevante)
 * - Faz cache entre páginas
 */
"use client";

import useSWR, { type SWRConfiguration, type SWRResponse } from "swr";
import useSWRMutation, { type SWRMutationResponse } from "swr/mutation";

import {
  webappApi,
  type WebAppUser,
  type Category,
  type ProductListItem,
  type ProductDetail,
  type Order,
  type PixCharge,
  type Withdraw,
} from "@/lib/api";

// ============================================================
// ⚙️ CONFIGURAÇÕES PADRÃO
// ============================================================
const defaultConfig: SWRConfiguration = {
  revalidateOnFocus: true,
  revalidateOnReconnect: true,
  shouldRetryOnError: false,
  keepPreviousData: true,
  dedupingInterval: 3000,
};

// ============================================================
// 🔑 KEYS (centralizadas)
// ============================================================
export const keys = {
  me: "/webapp/me" as const,
  categories: "/webapp/categories" as const,
  products: (filters?: ProductsFilters) =>
    [
      "/webapp/products",
      filters?.categoryId ?? null,
      filters?.search ?? null,
      filters?.page ?? 1,
      filters?.perPage ?? 20,
    ] as const,
  product: (id: number) => `/webapp/products/${id}` as const,
  orders: (page = 1, perPage = 20) =>
    ["/webapp/orders", page, perPage] as const,
  order: (id: number) => `/webapp/orders/${id}` as const,
  pix: (txid: string) => `/webapp/wallet/pix/${txid}` as const,
  transactions: (page = 1, perPage = 20) =>
    ["/webapp/wallet/transactions", page, perPage] as const,
  withdrawHistory: (page = 1, perPage = 20) =>
    ["/webapp/withdraw/history", page, perPage] as const,
  affiliateStats: "/webapp/affiliate/stats" as const,
  hasWithdrawPassword: "/webapp/password/has" as const,
  health: "/webapp/health" as const,
};

// ============================================================
// 📋 TIPOS AUXILIARES
// ============================================================
export interface ProductsFilters {
  categoryId?: number;
  search?: string;
  page?: number;
  perPage?: number;
}

// ============================================================
// 👤 USUÁRIO LOGADO
// ============================================================
export function useMe(config?: SWRConfiguration) {
  return useSWR<WebAppUser>(
    keys.me,
    async () => {
      const res = await webappApi.me();
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao carregar perfil");
      }
      return res.data;
    },
    { ...defaultConfig, ...config },
  );
}

// ============================================================
// 📂 CATEGORIAS
// ============================================================
export function useCategories(config?: SWRConfiguration) {
  return useSWR<Category[]>(
    keys.categories,
    async () => {
      const res = await webappApi.categories();
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao carregar categorias");
      }
      return res.data;
    },
    { ...defaultConfig, ...config },
  );
}

// ============================================================
// 📦 PRODUTOS (com filtros + paginação)
// ============================================================
export function useProducts(
  filters?: ProductsFilters,
  config?: SWRConfiguration,
) {
  const key = keys.products(filters);

  return useSWR<ProductListItem[]>(
    key,
    async () => {
      const res = await webappApi.products(filters);
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao carregar produtos");
      }
      return res.data;
    },
    { ...defaultConfig, ...config },
  );
}

// ============================================================
// 🔍 PRODUTO (detalhe)
// ============================================================
export function useProduct(id: number | null | undefined) {
  return useSWR<ProductDetail>(
    id ? keys.product(id) : null,
    async () => {
      if (!id) throw new Error("ID inválido");
      const res = await webappApi.product(id);
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Produto não encontrado");
      }
      return res.data;
    },
    { ...defaultConfig, refreshInterval: 15000 },
  );
}

// ============================================================
// 🛒 PEDIDOS
// ============================================================
export function useOrders(page = 1, perPage = 20) {
  return useSWR<Order[]>(
    keys.orders(page, perPage),
    async () => {
      const res = await webappApi.orders({ page, perPage });
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao carregar pedidos");
      }
      return res.data;
    },
    defaultConfig,
  );
}

export function useOrder(id: number | null | undefined) {
  return useSWR<Order>(
    id ? keys.order(id) : null,
    async () => {
      if (!id) throw new Error("ID inválido");
      const res = await webappApi.order(id);
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Pedido não encontrado");
      }
      return res.data;
    },
    defaultConfig,
  );
}

// ============================================================
// 💰 TRANSAÇÕES (extrato)
// ============================================================
export function useTransactions(page = 1, perPage = 20) {
  return useSWR<unknown[]>(
    keys.transactions(page, perPage),
    async () => {
      const res = await webappApi.transactions({ page, perPage });
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao carregar transações");
      }
      return res.data;
    },
    defaultConfig,
  );
}

// ============================================================
// 💠 PIX — criar
// ============================================================
export function useCreatePix(): SWRMutationResponse<
  PixCharge,
  Error,
  string,
  { amount: number }
> {
  return useSWRMutation<
    PixCharge,
    Error,
    string,
    { amount: number }
  >(
    "/webapp/wallet/pix",
    async (_key, { arg }) => {
      const res = await webappApi.createPix(arg.amount);
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao gerar PIX");
      }
      return res.data;
    },
  );
}

// ============================================================
// 💠 PIX — verificar status
// ============================================================
export function usePix(txid: string | null | undefined) {
  return useSWR<PixCharge>(
    txid ? keys.pix(txid) : null,
    async () => {
      if (!txid) throw new Error("TXID inválido");
      const res = await webappApi.checkPix(txid);
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "PIX não encontrado");
      }
      return res.data;
    },
    {
      refreshInterval: (data) =>
        data?.status === "PENDING" ? 4000 : 0,
      revalidateOnFocus: true,
    },
  );
}

// ============================================================
// 🔐 SENHA DE SAQUE
// ============================================================
export function useHasWithdrawPassword() {
  return useSWR<{ has: boolean }>(
    keys.hasWithdrawPassword,
    async () => {
      const res = await webappApi.hasWithdrawPassword();
      if (!res.ok || !res.data) {
        return { has: false };
      }
      return res.data;
    },
    { ...defaultConfig, revalidateOnFocus: false },
  );
}

// ============================================================
// 🔐 SENHA DE SAQUE — definir
// ============================================================
export function useSetWithdrawPassword() {
  return useSWRMutation<
    { ok: true },
    Error,
    string,
    { password: string; passwordConfirm: string; recoveryEmail?: string }
  >(
    "/webapp/password/set",
    async (_key, { arg }) => {
      const res = await webappApi.setWithdrawPassword(arg);
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao cadastrar senha");
      }
      return res.data;
    },
  );
}

export function useResetWithdrawPassword() {
  return useSWRMutation<
    { ok: true },
    Error,
    string,
    { token: string; password: string; passwordConfirm: string }
  >(
    "/webapp/password/reset",
    async (_key, { arg }) => {
      const res = await webappApi.resetWithdrawPassword(arg);
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao redefinir senha");
      }
      return res.data;
    },
  );
}

export function useForgotWithdrawPassword() {
  return useSWRMutation<
    { ok: true },
    Error,
    string,
    { email: string }
  >(
    "/webapp/password/forgot",
    async (_key, { arg }) => {
      const res = await webappApi.forgotWithdrawPassword(arg.email);
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao enviar e-mail");
      }
      return res.data;
    },
  );
}

// ============================================================
// 💸 SAQUES
// ============================================================
export function useWithdrawHistory(page = 1, perPage = 20) {
  return useSWR<Withdraw[]>(
    keys.withdrawHistory(page, perPage),
    async () => {
      const res = await webappApi.withdrawHistory({ page, perPage });
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao carregar saques");
      }
      return res.data;
    },
    defaultConfig,
  );
}

export function useRequestWithdraw() {
  return useSWRMutation<
    Withdraw,
    Error,
    string,
    {
      amount: number;
      pixKey: string;
      pixKeyType: string;
      password: string;
    }
  >(
    "/webapp/withdraw",
    async (_key, { arg }) => {
      const res = await webappApi.requestWithdraw(arg);
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao solicitar saque");
      }
      return res.data;
    },
  );
}

// ============================================================
// 👥 AFILIADOS
// ============================================================
export function useAffiliateStats() {
  return useSWR<unknown>(
    keys.affiliateStats,
    async () => {
      const res = await webappApi.affiliateStats();
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao carregar afiliados");
      }
      return res.data;
    },
    defaultConfig,
  );
}

// ============================================================
// 🎁 GIFT CARD
// ============================================================
export function useRedeemGift() {
  return useSWRMutation<
    { amount: number },
    Error,
    string,
    { code: string }
  >(
    "/webapp/gift/redeem",
    async (_key, { arg }) => {
      const res = await webappApi.redeemGift(arg.code);
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Gift Card inválido");
      }
      return res.data;
    },
  );
}

// ============================================================
// 🛍️ COMPRAR
// ============================================================
export function useBuy() {
  return useSWRMutation<
    Order,
    Error,
    string,
    { productId: number; quantity: number; method?: "BALANCE" | "PIX" }
  >(
    "/webapp/buy",
    async (_key, { arg }) => {
      const res = await webappApi.buy(arg);
      if (!res.ok || !res.data) {
        throw new Error(res.error?.message || "Falha ao processar compra");
      }
      return res.data;
    },
  );
}

// ============================================================
// 💚 HEALTH
// ============================================================
export function useHealth() {
  return useSWR<{ ok: boolean }>(
    keys.health,
    async () => {
      const res = await webappApi.health();
      return res.data ?? { ok: false };
    },
    { revalidateOnFocus: false, refreshInterval: 60000 },
  );
}

// ============================================================
// 🔄 UTILITÁRIOS DE MUTAÇÃO GLOBAL
// ============================================================
export function useGlobalMutate() {
  return {
    refreshMe: () => import("swr").then((m) => m.default.mutate(keys.me)),
    refreshWallet: () =>
      import("swr").then((m) =>
        Promise.all([
          m.default.mutate(keys.me),
          m.default.mutate((key) =>
            Array.isArray(key) && key[0] === "/webapp/wallet/transactions",
          ),
        ]),
      ),
    refreshOrders: () =>
      import("swr").then((m) =>
        m.default.mutate((key) =>
          Array.isArray(key) && key[0] === "/webapp/orders",
        ),
      ),
  };
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export type { WebAppUser, Category, ProductListItem, ProductDetail, Order, PixCharge, Withdraw };
