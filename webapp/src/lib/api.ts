/**
 * Larizinha Store — WebApp Next.js
 * Cliente HTTP + endpoints tipados.
 *
 * - Envia o `initData` do Telegram em toda requisição (via header X-Telegram-Init-Data).
 * - Também suporta JWT (fallback para acesso fora do Telegram).
 * - Trata erros padronizados (401, 403, 404, 429, 500).
 * - Helpers de GET/POST/PUT/PATCH/DELETE.
 * - Endpoints específicos: catálogo, produto, carrinho, recarga, pedidos, perfil, saque, senha.
 */
"use client";

import axios, {
  AxiosError,
  AxiosInstance,
  AxiosRequestConfig,
  AxiosResponse,
} from "axios";

// ============================================================
// 🌐 BASE URL
// ============================================================
const BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ||
  process.env.NEXT_PUBLIC_API ||
  "http://localhost:8000";

// ============================================================
// 🧾 TIPAGEM PADRÃO
// ============================================================
export interface ApiError {
  code: string;
  message: string;
  details?: Record<string, unknown> | null;
  httpStatus: number;
}

export interface ApiResponse<T> {
  ok: boolean;
  data: T | null;
  error: ApiError | null;
}

// ============================================================
// 🔑 OBTENÇÃO DO initData
// ============================================================
function getInitData(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.Telegram?.WebApp?.initData || "";
  } catch {
    return "";
  }
}

function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return localStorage.getItem("larizinha_token");
  } catch {
    return null;
  }
}

export function setAuthToken(token: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (token) localStorage.setItem("larizinha_token", token);
    else localStorage.removeItem("larizinha_token");
  } catch {
    // ignora
  }
}

// ============================================================
// 🚀 INSTÂNCIA AXIOS
// ============================================================
export const api: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 20000,
  headers: {
    "Content-Type": "application/json",
    Accept: "application/json",
  },
});

// ---------- Interceptor de REQUEST ----------
api.interceptors.request.use(
  (config) => {
    const initData = getInitData();
    const token = getStoredToken();

    if (initData) {
      config.headers = config.headers ?? {};
      (config.headers as Record<string, string>)["X-Telegram-Init-Data"] = initData;
    }
    if (token) {
      config.headers = config.headers ?? {};
      (config.headers as Record<string, string>)["Authorization"] = `Bearer ${token}`;
    }

    return config;
  },
  (error) => Promise.reject(error),
);

// ---------- Interceptor de RESPONSE ----------
api.interceptors.response.use(
  (response: AxiosResponse) => response,
  (error: AxiosError) => {
    // Não loga 404 para não poluir console
    if (process.env.NODE_ENV !== "production" && error.response?.status !== 404) {
      console.warn(
        `[api] ${error.config?.method?.toUpperCase()} ${error.config?.url} → ${error.response?.status}`,
      );
    }
    return Promise.reject(error);
  },
);

// ============================================================
// 🧰 HELPERS DE RESPOSTA
// ============================================================
function toApiError(error: unknown): ApiError {
  if (axios.isAxiosError(error)) {
    const status = error.response?.status ?? 0;
    const data = error.response?.data as Record<string, unknown> | undefined;

    let message = error.message || "Erro desconhecido";
    let code = "UNKNOWN_ERROR";

    if (data && typeof data === "object") {
      message = String(data.message || data.error || message);
      code = String(data.code || code);
    }

    if (status === 401) {
      message = message || "Sessão expirada. Abra novamente pelo Telegram.";
      code = "UNAUTHORIZED";
    } else if (status === 403) {
      message = message || "Você não tem permissão para esta ação.";
      code = "FORBIDDEN";
    } else if (status === 404) {
      message = message || "Recurso não encontrado.";
      code = "NOT_FOUND";
    } else if (status === 429) {
      message = message || "Muitas requisições. Aguarde alguns segundos.";
      code = "RATE_LIMITED";
    } else if (status >= 500) {
      message = "Erro no servidor. Tente novamente em instantes.";
      code = "SERVER_ERROR";
    } else if (error.code === "ECONNABORTED") {
      message = "Tempo esgotado. Verifique sua conexão.";
      code = "TIMEOUT";
    } else if (!error.response) {
      message = "Sem conexão com o servidor.";
      code = "NETWORK_ERROR";
    }

    return {
      code,
      message,
      details: (data?.details as Record<string, unknown>) ?? null,
      httpStatus: status,
    };
  }

  return {
    code: "UNKNOWN_ERROR",
    message: "Erro inesperado.",
    details: null,
    httpStatus: 0,
  };
}

// ============================================================
// 🎯 REQUEST GENÉRICO
// ============================================================
export async function request<T>(
  config: AxiosRequestConfig,
): Promise<ApiResponse<T>> {
  try {
    const response = await api.request<T>(config);

    // A API pode retornar { ok: true, data: ... } ou só o payload
    const body = response.data as unknown;
    if (body && typeof body === "object" && "ok" in (body as object)) {
      const envelope = body as { ok: boolean; data?: T; error?: ApiError };
      if (envelope.ok === false) {
        return {
          ok: false,
          data: null,
          error: envelope.error ?? {
            code: "API_ERROR",
            message: "Erro retornado pela API.",
            details: null,
            httpStatus: response.status,
          },
        };
      }
      return {
        ok: true,
        data: (envelope.data as T) ?? null,
        error: null,
      };
    }

    return { ok: true, data: body as T, error: null };
  } catch (error) {
    return { ok: false, data: null, error: toApiError(error) };
  }
}

// ============================================================
// 🔗 MÉTODOS ATALHO
// ============================================================
export async function get<T>(
  url: string,
  params?: Record<string, unknown>,
): Promise<ApiResponse<T>> {
  return request<T>({ method: "GET", url, params });
}

export async function post<T>(
  url: string,
  data?: unknown,
): Promise<ApiResponse<T>> {
  return request<T>({ method: "POST", url, data });
}

export async function put<T>(
  url: string,
  data?: unknown,
): Promise<ApiResponse<T>> {
  return request<T>({ method: "PUT", url, data });
}

export async function patch<T>(
  url: string,
  data?: unknown,
): Promise<ApiResponse<T>> {
  return request<T>({ method: "PATCH", url, data });
}

export async function del<T>(
  url: string,
  params?: Record<string, unknown>,
): Promise<ApiResponse<T>> {
  return request<T>({ method: "DELETE", url, params });
}

// ============================================================
// 🎣 FETCHER DO SWR
// ============================================================
export async function fetcher<T = unknown>(url: string): Promise<T> {
  const res = await request<T>({ method: "GET", url });
  if (!res.ok) {
    const err = new Error(res.error?.message || "Erro na requisição");
    (err as Error & { code?: string }).code = res.error?.code;
    throw err;
  }
  return res.data as T;
}

// ============================================================
// 🧩 TIPOS DE DOMÍNIO
// ============================================================
export interface WebAppUser {
  id: number;
  telegramId: number;
  firstName: string | null;
  username: string | null;
  photoUrl: string | null;
  balance: number;
  balanceLocked: number;
  totalSpent: number;
  totalDeposited: number;
  purchasesCount: number;
  whatsapp: string | null;
  email: string | null;
  affiliateCode: string | null;
  affiliateBalance: number;
  affiliateActive: boolean;
  affiliateLevel: string;
  status: "ACTIVE" | "BLOCKED" | "BANNED";
  hasWithdrawPassword: boolean;
}

export interface Category {
  id: number;
  name: string;
  slug: string;
  emoji: string | null;
  order: number;
}

export interface ProductListItem {
  id: number;
  name: string;
  subtitle: string | null;
  price: number;
  pricePromo: number | null;
  imageWebapp: string | null;
  stock: number;
  unlimited: boolean;
  categoryId: number | null;
  featured: boolean;
  soldOut: boolean;
}

export interface ProductDetail extends ProductListItem {
  description: string | null;
  warrantyDays: number;
  expirationDays: number;
  noteProduct: string | null;
  noteActivation: string | null;
  activationLink: string | null;
  soldCount: number;
  images: string[];
}

export interface OrderItem {
  id: number;
  productName: string;
  quantity: number;
  unitPrice: number;
  total: number;
}

export interface Order {
  id: number;
  status: "PENDING" | "PAID" | "DELIVERED" | "CANCELED" | "REFUNDED" | "EXPIRED";
  finalTotal: number;
  method: "BALANCE" | "PIX" | "GIFT_CARD" | "MANUAL";
  createdAt: string;
  deliveredAt: string | null;
  expiresAt: string | null;
  items: OrderItem[];
}

export interface PixCharge {
  txid: string;
  amount: number;
  bonus: number;
  qrCodeBase64: string | null;
  copyPaste: string;
  expiresAt: string;
  status: "PENDING" | "PAID" | "EXPIRED" | "CANCELED" | "REFUNDED";
}

export interface WithdrawRequest {
  amount: number;
  pixKey: string;
  pixKeyType: string;
  password: string;
}

export interface Withdraw {
  id: number;
  amount: number;
  netAmount: number;
  pixKey: string;
  pixKeyType: string;
  status:
    | "PENDING"
    | "PROCESSING"
    | "APPROVED"
    | "PAID"
    | "REFUSED"
    | "CANCELED";
  createdAt: string;
  processedAt: string | null;
}

// ============================================================
// 🛍️ ENDPOINTS — LOJA
// ============================================================
export const webappApi = {
  // ---------- AUTH / PERFIL ----------
  async me(): Promise<ApiResponse<WebAppUser>> {
    return get<WebAppUser>("/webapp/me");
  },

  async updateProfile(data: {
    whatsapp?: string;
    email?: string;
  }): Promise<ApiResponse<WebAppUser>> {
    return patch<WebAppUser>("/webapp/me", data);
  },

  // ---------- CATÁLOGO ----------
  async categories(): Promise<ApiResponse<Category[]>> {
    return get<Category[]>("/webapp/categories");
  },

  async products(params?: {
    categoryId?: number;
    search?: string;
    page?: number;
    perPage?: number;
  }): Promise<ApiResponse<ProductListItem[]>> {
    return get<ProductListItem[]>("/webapp/products", params);
  },

  async product(id: number): Promise<ApiResponse<ProductDetail>> {
    return get<ProductDetail>(`/webapp/products/${id}`);
  },

  // ---------- CARTEIRA ----------
  async createPix(amount: number): Promise<ApiResponse<PixCharge>> {
    return post<PixCharge>("/webapp/wallet/pix", { amount });
  },

  async checkPix(txid: string): Promise<ApiResponse<PixCharge>> {
    return get<PixCharge>(`/webapp/wallet/pix/${txid}`);
  },

  async transactions(params?: {
    page?: number;
    perPage?: number;
  }): Promise<ApiResponse<unknown[]>> {
    return get<unknown[]>("/webapp/wallet/transactions", params);
  },

  // ---------- PEDIDOS ----------
  async orders(params?: {
    page?: number;
    perPage?: number;
  }): Promise<ApiResponse<Order[]>> {
    return get<Order[]>("/webapp/orders", params);
  },

  async order(id: number): Promise<ApiResponse<Order>> {
    return get<Order>(`/webapp/orders/${id}`);
  },

  async buy(data: {
    productId: number;
    quantity: number;
    method?: "BALANCE" | "PIX";
  }): Promise<ApiResponse<Order>> {
    return post<Order>("/webapp/buy", data);
  },

  // ---------- SENHA DE SAQUE ----------
  async hasWithdrawPassword(): Promise<ApiResponse<{ has: boolean }>> {
    return get<{ has: boolean }>("/webapp/password/has");
  },

  async setWithdrawPassword(data: {
    password: string;
    passwordConfirm: string;
    recoveryEmail?: string;
  }): Promise<ApiResponse<{ ok: true }>> {
    return post<{ ok: true }>("/webapp/password/set", data);
  },

  async forgotWithdrawPassword(
    email: string,
  ): Promise<ApiResponse<{ ok: true }>> {
    return post<{ ok: true }>("/webapp/password/forgot", { email });
  },

  async resetWithdrawPassword(data: {
    token: string;
    password: string;
    passwordConfirm: string;
  }): Promise<ApiResponse<{ ok: true }>> {
    return post<{ ok: true }>("/webapp/password/reset", data);
  },

  // ---------- SAQUE ----------
  async requestWithdraw(
    data: WithdrawRequest,
  ): Promise<ApiResponse<Withdraw>> {
    return post<Withdraw>("/webapp/withdraw", data);
  },

  async withdrawHistory(params?: {
    page?: number;
    perPage?: number;
  }): Promise<ApiResponse<Withdraw[]>> {
    return get<Withdraw[]>("/webapp/withdraw/history", params);
  },

  // ---------- AFILIADOS ----------
  async affiliateStats(): Promise<ApiResponse<unknown>> {
    return get<unknown>("/webapp/affiliate/stats");
  },

  // ---------- CUPOM / GIFT ----------
  async redeemGift(
    code: string,
  ): Promise<ApiResponse<{ amount: number }>> {
    return post<{ amount: number }>("/webapp/gift/redeem", { code });
  },

  // ---------- HEALTH ----------
  async health(): Promise<ApiResponse<{ ok: boolean }>> {
    return get<{ ok: boolean }>("/webapp/health");
  },
};

// ============================================================
// 📌 EXPORTS
// ============================================================
export default api;
