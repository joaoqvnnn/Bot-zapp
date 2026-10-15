# ============================================
# LARIZINHA STORE — API (Node.js 20 + TypeScript)
# Webhooks Mercado Pago + Telegram + Admin API + WebApp API
# ============================================

# --------------------------------------------
# 🏗️ STAGE 1 — BUILDER
# --------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /build

# Dependências do sistema para builds nativos
RUN apk add --no-cache \
    python3 \
    make \
    g++ \
    libc6-compat \
    openssl \
    ca-certificates \
    tzdata

# Variáveis de build
ENV NODE_ENV=development \
    TZ=America/Sao_Paulo \
    npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false

# --------------------------------------------
# 📦 INSTALA DEPENDÊNCIAS (cache de camada)
# --------------------------------------------
COPY package*.json ./
COPY tsconfig*.json ./

RUN npm ci --include=dev

# --------------------------------------------
# 📂 COPIA FONTE E COMPILA
# --------------------------------------------
COPY . .

RUN npm run build

# --------------------------------------------
# 🧹 REMOVE DEV DEPENDENCIES
# --------------------------------------------
RUN npm prune --omit=dev

# ============================================
# 🚀 STAGE 2 — RUNTIME
# ============================================
FROM node:20-alpine AS runtime

WORKDIR /app

# Dependências de runtime
RUN apk add --no-cache \
    ca-certificates \
    openssl \
    tzdata \
    curl \
    && ln -snf /usr/share/zoneinfo/America/Sao_Paulo /etc/localtime \
    && echo "America/Sao_Paulo" > /etc/timezone

# Variáveis de runtime
ENV NODE_ENV=production \
    TZ=America/Sao_Paulo \
    PORT=8000

# --------------------------------------------
# 👤 USUÁRIO NÃO-ROOT
# --------------------------------------------
RUN addgroup -g 1001 -S larizinha \
    && adduser -S -u 1001 -G larizinha larizinha

# --------------------------------------------
# 📂 COPIA ARTEFATOS DO BUILDER
# --------------------------------------------
COPY --from=builder --chown=larizinha:larizinha /build/node_modules ./node_modules
COPY --from=builder --chown=larizinha:larizinha /build/dist ./dist
COPY --from=builder --chown=larizinha:larizinha /build/package.json ./package.json
COPY --from=builder --chown=larizinha:larizinha /build/prisma ./prisma

# --------------------------------------------
# 📁 PASTAS DE RUNTIME
# --------------------------------------------
RUN mkdir -p /app/logs /app/tmp /app/uploads \
    && chown -R larizinha:larizinha /app

# --------------------------------------------
# 🔐 PERMISSÕES
# --------------------------------------------
USER larizinha

# --------------------------------------------
# 🌐 PORTA
# --------------------------------------------
EXPOSE 8000

# --------------------------------------------
# 🚀 HEALTHCHECK
# --------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# --------------------------------------------
# ▶️ ENTRYPOINT
# --------------------------------------------
CMD ["node", "dist/api/main.js"]
