# ============================================
# LARIZINHA STORE — BOT + WORKER (Python 3.12)
# Aiogram 3.x + PostgreSQL + Redis + Mercado Pago
# ============================================

FROM python:3.12-slim AS base

# --------------------------------------------
# ⚙️ VARIÁVEIS DE AMBIENTE
# --------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Sao_Paulo \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# --------------------------------------------
# 📦 DEPENDÊNCIAS DE SISTEMA
# --------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libpq-dev \
    libffi-dev \
    libssl-dev \
    libjpeg-dev \
    zlib1g-dev \
    libfreetype6-dev \
    libpng-dev \
    curl \
    ca-certificates \
    tzdata \
    fonts-dejavu \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# --------------------------------------------
# 👤 USUÁRIO NÃO-ROOT
# --------------------------------------------
RUN groupadd -r larizinha && useradd -r -g larizinha -m -d /app larizinha

# --------------------------------------------
# 📁 DIRETÓRIO DE TRABALHO
# --------------------------------------------
WORKDIR /app

# --------------------------------------------
# 🐍 DEPENDÊNCIAS PYTHON
# --------------------------------------------
COPY requirements.txt /app/requirements.txt

RUN pip install --upgrade pip setuptools wheel \
    && pip install -r /app/requirements.txt

# --------------------------------------------
# 📂 CÓDIGO-FONTE DO BOT
# --------------------------------------------
COPY --chown=larizinha:larizinha . /app

# --------------------------------------------
# 📁 PASTAS DE RUNTIME
# --------------------------------------------
RUN mkdir -p /app/logs /app/backups /app/uploads /app/tmp \
    && chown -R larizinha:larizinha /app

# --------------------------------------------
# 🔐 PERMISSÕES
# --------------------------------------------
USER larizinha

# --------------------------------------------
# 🌐 EXPOSE (usado apenas pelo worker/api)
# --------------------------------------------
EXPOSE 8000

# --------------------------------------------
# 🚀 HEALTHCHECK
# --------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# --------------------------------------------
# ▶️ COMANDO PADRÃO (sobrescrito pelo docker-compose)
# --------------------------------------------
CMD ["python", "-m", "bot.main"]
