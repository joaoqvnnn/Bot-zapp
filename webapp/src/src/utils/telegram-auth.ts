/**
 * Larizinha Store — API TypeScript
 * Validação do `initData` do Telegram WebApp.
 *
 * O Telegram envia um `initData` (query string) assinado com HMAC-SHA256
 * usando o token do bot. Este helper valida a assinatura e extrai o user.
 *
 * Uso:
 *   const user = verifyTelegramInitData(initData, botToken);
 *   if (!user) throw new Error("Assinatura inválida");
 */
import crypto from "node:crypto";

// ============================================================
// 🧩 TIPOS
// ============================================================
export interface TelegramInitUser {
  id: number;
  first_name?: string;
  last_name?: string;
  username?: string;
  language_code?: string;
  is_premium?: boolean;
  photo_url?: string;
}

export interface TelegramInitData {
  user: TelegramInitUser | null;
  authDate: number | null;
  hash: string | null;
  startParam: string | null;
  queryId: string | null;
  raw: Record<string, string>;
}

// ============================================================
// 🔐 VERIFY
// ============================================================
/**
 * Verifica a assinatura HMAC do `initData`.
 * Retorna true se a assinatura for válida.
 */
export function verifyInitDataSignature(
  initData: string,
  botToken: string,
): boolean {
  if (!initData || !botToken) return false;

  try {
    const params = new URLSearchParams(initData);
    const hash = params.get("hash");
    if (!hash) return false;

    // Remove o hash
    params.delete("hash");

    // Ordena alfabeticamente as chaves
    const dataCheckString = Array.from(params.entries())
      .map(([k, v]) => `${k}=${v}`)
      .sort()
      .join("\n");

    // secret_key = HMAC_SHA256("WebAppData", bot_token)
    const secretKey = crypto
      .createHmac("sha256", "WebAppData")
      .update(botToken)
      .digest();

    // hash calculado = HMAC_SHA256(secret_key, data_check_string)
    const calculatedHash = crypto
      .createHmac("sha256", secretKey)
      .update(dataCheckString)
      .digest("hex");

    // Comparação em tempo constante
    const a = Buffer.from(calculatedHash, "hex");
    const b = Buffer.from(hash, "hex");
    if (a.length !== b.length) return false;
    return crypto.timingSafeEqual(a, b);
  } catch {
    return false;
  }
}

// ============================================================
// 🧾 PARSE
// ============================================================
/**
 * Faz o parse do `initData` retornando os campos conhecidos.
 * NÃO valida assinatura — use `verifyInitDataSignature` antes.
 */
export function parseInitData(initData: string): TelegramInitData {
  const params = new URLSearchParams(initData || "");

  let user: TelegramInitUser | null = null;
  const rawUser = params.get("user");
  if (rawUser) {
    try {
      user = JSON.parse(rawUser) as TelegramInitUser;
    } catch {
      user = null;
    }
  }

  const raw: Record<string, string> = {};
  params.forEach((v, k) => {
    raw[k] = v;
  });

  return {
    user,
    authDate: params.get("auth_date") ? Number(params.get("auth_date")) : null,
    hash: params.get("hash"),
    startParam: params.get("start_param"),
    queryId: params.get("query_id"),
    raw,
  };
}

// ============================================================
// 🎯 VERIFY + PARSE
// ============================================================
/**
 * Combina verificação de assinatura + parse.
 * Retorna `null` se a assinatura for inválida OU se estiver expirada.
 */
export function verifyAndParse(
  initData: string,
  botToken: string,
  maxAgeSec = 86400,
): TelegramInitData | null {
  if (!verifyInitDataSignature(initData, botToken)) return null;

  const parsed = parseInitData(initData);

  // Verifica expiração
  if (parsed.authDate) {
    const now = Math.floor(Date.now() / 1000);
    if (now - parsed.authDate > maxAgeSec) {
      return null;
    }
  }

  return parsed;
}

// ============================================================
// 🧮 HELPERS EXTRAS
// ============================================================
/** Extrai apenas o `user` se válido — senão retorna null. */
export function getVerifiedUser(
  initData: string,
  botToken: string,
  maxAgeSec = 86400,
): TelegramInitUser | null {
  const parsed = verifyAndParse(initData, botToken, maxAgeSec);
  return parsed?.user ?? null;
}

/** Extrai o `start_param` (deep-link) se válido. */
export function getStartParam(
  initData: string,
  botToken: string,
): string | null {
  const parsed = verifyAndParse(initData, botToken);
  return parsed?.startParam ?? null;
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export default {
  verifyInitDataSignature,
  parseInitData,
  verifyAndParse,
  getVerifiedUser,
  getStartParam,
};
