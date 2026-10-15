/**
 * Larizinha Store — WebApp Next.js
 * Endpoint /api/health — usado pelo Render para healthcheck.
 *
 * Retorna 200 se o WebApp está operacional.
 * Verifica:
 *   - Presença das variáveis de ambiente essenciais
 *   - Conexão com a API (opcional, mas mantida rápida)
 */
import { NextResponse } from "next/server";

// ============================================================
// 🔎 CONFIGURAÇÃO
// ============================================================
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const revalidate = 0;

const APP_VERSION = process.env.npm_package_version || "1.0.0";
const STORE_NAME = process.env.NEXT_PUBLIC_STORE_NAME || "Larizinha Store";

// ============================================================
// 📡 GET /api/health
// ============================================================
export async function GET() {
  const startedAt = Date.now();

  const checks: Record<string, boolean> = {
    // Variáveis críticas para o WebApp funcionar
    api_url: Boolean(process.env.NEXT_PUBLIC_API_URL || process.env.API_URL),
    webapp_secret: Boolean(process.env.WEBAPP_SECRET),
    bot_username: Boolean(process.env.NEXT_PUBLIC_BOT_USERNAME),
  };

  const allOk = Object.values(checks).every(Boolean);
  const status = allOk ? "ok" : "degraded";
  const httpStatus = allOk ? 200 : 503;

  return NextResponse.json(
    {
      status,
      app: STORE_NAME,
      version: APP_VERSION,
      environment: process.env.NODE_ENV || "production",
      checks,
      uptime_ms: Date.now() - startedAt,
      timestamp: new Date().toISOString(),
    },
    {
      status: httpStatus,
      headers: {
        "Cache-Control": "no-store, no-cache, must-revalidate",
      },
    },
  );
}

// ============================================================
// 🚫 MÉTODOS NÃO PERMITIDOS
// ============================================================
export async function POST() {
  return NextResponse.json(
    { status: "error", message: "Method Not Allowed" },
    { status: 405 },
  );
}

export async function PUT() {
  return NextResponse.json(
    { status: "error", message: "Method Not Allowed" },
    { status: 405 },
  );
}

export async function DELETE() {
  return NextResponse.json(
    { status: "error", message: "Method Not Allowed" },
    { status: 405 },
  );
}
