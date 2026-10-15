-- ============================================================
-- Larizinha Store — Inicialização do PostgreSQL
-- Executado UMA VEZ no boot do container (docker-entrypoint-initdb.d)
-- Complementa o schema Prisma (que é a fonte de verdade das migrations)
-- ============================================================

-- ------------------------------------------------------------
-- 🌐 CONFIGURAÇÕES DE SESSÃO
-- ------------------------------------------------------------
SET timezone = 'America/Sao_Paulo';
SET client_encoding = 'UTF8';
SET lc_monetary = 'pt_BR.UTF-8';
SET lc_numeric = 'pt_BR.UTF-8';
SET lc_time = 'pt_BR.UTF-8';
SET default_text_search_config = 'pg_catalog.portuguese';

-- ------------------------------------------------------------
-- 🧩 EXTENSÕES
-- ------------------------------------------------------------
-- pg_trgm → busca ILIKE/LIKE rápida em nomes/códigos
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- pgcrypto → gen_random_uuid(), criptografia auxiliar
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- unaccent → busca sem acentos (usado em nome/descrição)
CREATE EXTENSION IF NOT EXISTS unaccent;

-- btree_gin → suporte a GIN em tipos escalares (auxilia índices compostos JSONB)
CREATE EXTENSION IF NOT EXISTS btree_gin;

-- ------------------------------------------------------------
-- 📊 ESTATÍSTICAS DE PERFORMANCE
-- ------------------------------------------------------------
-- Aumenta amostragem para planejamento melhor em tabelas grandes
ALTER DATABASE CURRENT SET default_statistics_target = 200;

-- ------------------------------------------------------------
-- 🔎 FUNÇÕES AUXILIARES
-- ------------------------------------------------------------
-- Trigger function para atualizar `updated_at` automaticamente
-- (o Prisma faz via @updatedAt, mas garantimos no nível do banco para
--  queries diretas via psql/administrativas)
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW() AT TIME ZONE 'UTC';
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Função utilitária: normaliza texto (lower + unaccent) para busca
CREATE OR REPLACE FUNCTION normalize_text(txt TEXT)
RETURNS TEXT AS $$
BEGIN
    RETURN LOWER(unaccent(COALESCE(txt, '')));
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Função utilitária: valida chave PIX (formato simples)
CREATE OR REPLACE FUNCTION is_valid_pix_key(key TEXT)
RETURNS BOOLEAN AS $$
DECLARE
    k TEXT;
BEGIN
    IF key IS NULL OR LENGTH(TRIM(key)) = 0 THEN
        RETURN FALSE;
    END IF;
    k := TRIM(key);

    -- CPF (11 dígitos)
    IF k ~ '^[0-9]{11}$' THEN
        RETURN TRUE;
    END IF;
    -- CNPJ (14 dígitos)
    IF k ~ '^[0-9]{14}$' THEN
        RETURN TRUE;
    END IF;
    -- Telefone (10 a 13 dígitos, opcional +55)
    IF k ~ '^\+?[0-9]{10,13}$' THEN
        RETURN TRUE;
    END IF;
    -- E-mail
    IF k ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' THEN
        RETURN TRUE;
    END IF;
    -- Chave aleatória (UUID)
    IF k ~ '^[0-9a-fA-F\-]{32,45}$' THEN
        RETURN TRUE;
    END IF;

    RETURN FALSE;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ------------------------------------------------------------
-- 📝 ÍNDICES ADICIONAIS (GIN em JSONB)
-- Prisma não cria GIN em JSONB — criamos aqui para performance
-- ------------------------------------------------------------

-- users.alerts_enabled → array JSONB de product_ids sendo seguidos
CREATE INDEX IF NOT EXISTS ix_users_alerts_enabled_gin
    ON users USING GIN (alerts_enabled);

-- admins.permissions → array JSONB de permissões
CREATE INDEX IF NOT EXISTS ix_admins_permissions_gin
    ON admins USING GIN (permissions);

-- roles.permissions → array JSONB de permissões
CREATE INDEX IF NOT EXISTS ix_roles_permissions_gin
    ON roles USING GIN (permissions);

-- orders.metadata → objeto JSONB com dados extras do pedido
CREATE INDEX IF NOT EXISTS ix_orders_metadata_gin
    ON orders USING GIN (metadata);

-- pix_charges.metadata → objeto JSONB com dados extras do PIX
CREATE INDEX IF NOT EXISTS ix_pix_charges_metadata_gin
    ON pix_charges USING GIN (metadata);

-- transactions.metadata → objeto JSONB com dados extras da transação
CREATE INDEX IF NOT EXISTS ix_transactions_metadata_gin
    ON transactions USING GIN (metadata);

-- logs.details → objeto JSONB de detalhes da ação
CREATE INDEX IF NOT EXISTS ix_logs_details_gin
    ON logs USING GIN (details);

-- security_events.details → objeto JSONB de detalhes do evento
CREATE INDEX IF NOT EXISTS ix_security_events_details_gin
    ON security_events USING GIN (details);

-- tickets.rewards → array JSONB de prêmios
CREATE INDEX IF NOT EXISTS ix_tickets_rewards_gin
    ON tickets USING GIN (rewards);

-- tickets.odds → objeto JSONB de probabilidades
CREATE INDEX IF NOT EXISTS ix_tickets_odds_gin
    ON tickets USING GIN (odds);

-- games.rewards → array JSONB de recompensas
CREATE INDEX IF NOT EXISTS ix_games_rewards_gin
    ON games USING GIN (rewards);

-- games.limits → objeto JSONB de limites
CREATE INDEX IF NOT EXISTS ix_games_limits_gin
    ON games USING GIN (limits);

-- broadcasts.content → objeto JSONB com conteúdo do disparo
CREATE INDEX IF NOT EXISTS ix_broadcasts_content_gin
    ON broadcasts USING GIN (content);

-- automations.metadata → objeto JSONB com config extra
CREATE INDEX IF NOT EXISTS ix_automations_metadata_gin
    ON automations USING GIN (metadata);

-- ------------------------------------------------------------
-- 📝 ÍNDICES TRIGRAM (busca LIKE rápida)
-- Usados em buscas por nome/código (users, products, coupons, etc)
-- ------------------------------------------------------------

-- users: nome + username
CREATE INDEX IF NOT EXISTS ix_users_first_name_trgm
    ON users USING GIN (first_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_users_username_trgm
    ON users USING GIN (username gin_trgm_ops);

-- products: nome + slug
CREATE INDEX IF NOT EXISTS ix_products_name_trgm
    ON products USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_products_slug_trgm
    ON products USING GIN (slug gin_trgm_ops);

-- categories: nome + slug
CREATE INDEX IF NOT EXISTS ix_categories_name_trgm
    ON categories USING GIN (name gin_trgm_ops);

-- coupons: código
CREATE INDEX IF NOT EXISTS ix_coupons_code_trgm
    ON coupons USING GIN (code gin_trgm_ops);

-- gift_cards: código
CREATE INDEX IF NOT EXISTS ix_gift_cards_code_trgm
    ON gift_cards USING GIN (code gin_trgm_ops);

-- ------------------------------------------------------------
-- 📝 ÍNDICES COMPOSTOS (padrões de consulta frequentes)
-- ------------------------------------------------------------

-- Pedidos entregues por usuário (histórico)
CREATE INDEX IF NOT EXISTS ix_orders_user_status_delivered
    ON orders (user_id, status, delivered_at DESC);

-- PIX pendentes por usuário (fluxo de recarga)
CREATE INDEX IF NOT EXISTS ix_pix_user_status_pending
    ON pix_charges (user_id, status, expires_at);

-- Saques pendentes (fila de aprovação)
CREATE INDEX IF NOT EXISTS ix_withdraws_status_created
    ON withdraws (status, created_at DESC);

-- Transações por usuário + tipo + data (extrato)
CREATE INDEX IF NOT EXISTS ix_transactions_user_type_created
    ON transactions (user_id, type, created_at DESC);

-- Comissões pendentes por afiliado (fila de pagamento)
CREATE INDEX IF NOT EXISTS ix_commissions_earner_status_created
    ON commissions (earner_id, status, created_at DESC);

-- Credenciais disponíveis por produto (entrega automática)
CREATE INDEX IF NOT EXISTS ix_credentials_product_used
    ON product_credentials (product_id, used)
    WHERE used = FALSE;

-- Locks ativos (concorrência)
CREATE INDEX IF NOT EXISTS ix_locks_active
    ON locks (expires_at)
    WHERE released_at IS NULL;

-- Alertas abertos
CREATE INDEX IF NOT EXISTS ix_alerts_open
    ON alerts (category, severity, created_at DESC)
    WHERE resolved = FALSE;

-- ------------------------------------------------------------
-- 🔒 TRAVA DE SEGURANÇA: evita estoque negativo
-- ------------------------------------------------------------
-- (Sanity check executado periodicamente; apenas registra)
CREATE OR REPLACE FUNCTION check_negative_stock()
RETURNS TABLE(product_id BIGINT, stock INT) AS $$
BEGIN
    RETURN QUERY
        SELECT id, stock
        FROM products
        WHERE stock < 0;
END;
$$ LANGUAGE plpgsql STABLE;

-- ------------------------------------------------------------
-- 📊 VIEWS AUXILIARES (para relatórios/consultas admin)
-- ------------------------------------------------------------

-- View: resumo diário de vendas
CREATE OR REPLACE VIEW v_sales_daily AS
SELECT
    DATE(o.created_at AT TIME ZONE 'America/Sao_Paulo') AS day,
    COUNT(*) AS orders_count,
    COALESCE(SUM(o.final_total), 0) AS total_value,
    COALESCE(AVG(o.final_total), 0) AS avg_ticket
FROM orders o
WHERE o.status = 'DELIVERED'
GROUP BY 1
ORDER BY 1 DESC;

-- View: resumo diário de depósitos PIX
CREATE OR REPLACE VIEW v_pix_daily AS
SELECT
    DATE(p.created_at AT TIME ZONE 'America/Sao_Paulo') AS day,
    COUNT(*) AS pix_count,
    COALESCE(SUM(p.amount), 0) AS total_amount,
    COALESCE(SUM(p.bonus), 0) AS total_bonus
FROM pix_charges p
WHERE p.status = 'PAID'
GROUP BY 1
ORDER BY 1 DESC;

-- View: top produtos (últimos 30 dias)
CREATE OR REPLACE VIEW v_top_products AS
SELECT
    oi.product_id,
    oi.product_name,
    SUM(oi.quantity) AS total_qty,
    SUM(oi.total) AS total_value
FROM order_items oi
JOIN orders o ON o.id = oi.order_id
WHERE o.status = 'DELIVERED'
  AND o.created_at >= NOW() - INTERVAL '30 days'
GROUP BY 1, 2
ORDER BY 3 DESC;

-- View: top usuários por gasto
CREATE OR REPLACE VIEW v_top_users AS
SELECT
    u.id,
    u.telegram_id,
    u.first_name,
    u.total_spent,
    u.purchases_count
FROM users u
WHERE u.total_spent > 0
ORDER BY u.total_spent DESC;

-- View: afiliados ativos com stats
CREATE OR REPLACE VIEW v_affiliates_stats AS
SELECT
    u.id,
    u.telegram_id,
    u.first_name,
    u.affiliate_level,
    u.affiliate_balance,
    (
        SELECT COUNT(*) FROM users ref WHERE ref.referred_by_id = u.id
    ) AS referrals_count,
    (
        SELECT COALESCE(SUM(c.amount), 0)
        FROM commissions c
        WHERE c.earner_id = u.id AND c.status IN ('APPROVED', 'PAID')
    ) AS total_earned
FROM users u
WHERE u.affiliate_active = TRUE
ORDER BY 7 DESC;

-- ------------------------------------------------------------
-- ⚙️ CONFIGURAÇÕES FINAIS
-- ------------------------------------------------------------

-- Aumenta o timeout padrão de transações (evita erros em backups longos)
ALTER DATABASE CURRENT SET statement_timeout = '5min';
ALTER DATABASE CURRENT SET idle_in_transaction_session_timeout = '10min';

-- Loga queries lentas (em produção, apenas as > 1s)
-- (Habilita somente se tiver permissão)
DO $$
BEGIN
    BEGIN
        ALTER DATABASE CURRENT SET log_min_duration_statement = 1000;
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
END $$;

-- ------------------------------------------------------------
-- ✅ MENSAGEM FINAL
-- ------------------------------------------------------------
DO $$
BEGIN
    RAISE NOTICE 'Larizinha Store: init.sql executado com sucesso.';
    RAISE NOTICE 'Extensões: pg_trgm, pgcrypto, unaccent, btree_gin';
    RAISE NOTICE 'Views: v_sales_daily, v_pix_daily, v_top_products, v_top_users, v_affiliates_stats';
END $$;
