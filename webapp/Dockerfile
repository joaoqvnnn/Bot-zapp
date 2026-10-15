# ============================================
# LARIZINHA STORE — WEBAPP (Next.js 14 + React + TypeScript)
# Loja + Senha de Saque + Recuperação por E-mail
# ============================================

# --------------------------------------------
# 🏗️ STAGE 1 — DEPENDENCIES
# --------------------------------------------
FROM node:20-alpine AS deps

WORKDIR /app

RUN apk add --no-cache libc6-compat openssl tzdata

ENV TZ=America/Sao_Paulo \
    npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false

COPY package.json package-lock.json* ./
RUN npm ci

# --------------------------------------------
# 🏗️ STAGE 2 — BUILDER
# --------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production \
    TZ=America/Sao_Paulo

COPY --from=deps /app/node_modules ./node_modules
COPY . .

RUN npm run build

# --------------------------------------------
# 🚀 STAGE 3 — RUNNER
# --------------------------------------------
FROM node:20-alpine AS runner

WORKDIR /app

RUN apk add --no-cache \
    ca-certificates \
    openssl \
    tzdata \
    curl \
    && ln -snf /usr/share/zoneinfo/America/Sao_Paulo /etc/localtime \
    && echo "America/Sao_Paulo" > /etc/timezone

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    TZ=America/Sao_Paulo

# --------------------------------------------
# 👤 USUÁRIO NÃO-ROOT
# --------------------------------------------
RUN addgroup -g 1001 -S larizinha \
    && adduser -S -u 1001 -G larizinha larizinha

# --------------------------------------------
# 📂 COPIA ARTEFATOS
# --------------------------------------------
COPY --from=builder --chown=larizinha:larizinha /app/public ./public
COPY --from=builder --chown=larizinha:larizinha /app/.next/standalone ./
COPY --from=builder --chown=larizinha:larizinha /app/.next/static ./.next/static

# --------------------------------------------
# 📁 PASTAS DE RUNTIME
# --------------------------------------------
RUN mkdir -p /app/logs /app/tmp \
    && chown -R larizinha:larizinha /app

USER larizinha

# --------------------------------------------
# 🌐 PORTA
# --------------------------------------------
EXPOSE 3000

# --------------------------------------------
# 🚀 HEALTHCHECK
# --------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:3000/api/health || exit 1

# --------------------------------------------
# ▶️ ENTRYPOINT
# --------------------------------------------
CMD ["node", "server.js"]
