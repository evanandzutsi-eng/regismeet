-- =====================================================================================
-- MEETING INTELLIGENCE PLATFORM — CORE DATABASE MIGRATION
-- Target: Supabase PostgreSQL (>= 15). Idempotent where practical.
-- =====================================================================================

BEGIN;

-- ------------------------------------------------------------------------------------
-- 0. EXTENSIONS & SCHEMA
-- ------------------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "pgcrypto";      -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "vector";        -- pgvector embeddings
CREATE EXTENSION IF NOT EXISTS "pg_cron";       -- scheduled quota resets

CREATE SCHEMA IF NOT EXISTS intel_core;
SET search_path = intel_core, public;

-- ------------------------------------------------------------------------------------
-- 1. ENUM TYPES
-- ------------------------------------------------------------------------------------
DO $$ BEGIN
    CREATE TYPE intel_core.source_channel_t AS ENUM ('dashboard_upload', 'webhook_stream');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE intel_core.processing_state_t AS ENUM
        ('QUEUED', 'TRANSCRIBING', 'SUMMARIZING', 'COMPLETED', 'FAILED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE intel_core.subscription_status_t AS ENUM ('trialing', 'active', 'disabled', 'past_due');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ------------------------------------------------------------------------------------
-- 2. TABLE: companies (tenant root)
-- ------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intel_core.companies (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                            TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
    billing_email                   TEXT NOT NULL,
    paystack_customer_code          TEXT UNIQUE,
    paystack_subscription_code      TEXT UNIQUE,
    subscription_status             intel_core.subscription_status_t NOT NULL DEFAULT 'trialing',
    plan_tier                       TEXT NOT NULL DEFAULT 'starter',
    monthly_audio_minutes_limit     INTEGER NOT NULL DEFAULT 300 CHECK (monthly_audio_minutes_limit >= 0),
    processing_usage_this_month     NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (processing_usage_this_month >= 0),
    slack_webhook_url               TEXT,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE intel_core.companies IS 'Tenant root. Every downstream row is scoped to a company_id.';

-- ------------------------------------------------------------------------------------
-- 3. TABLE: webinars (ingested audio assets)
-- ------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intel_core.webinars (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id          UUID NOT NULL REFERENCES intel_core.companies(id) ON DELETE CASCADE,
    title               TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
    storage_path        TEXT NOT NULL,
    source_channel      intel_core.source_channel_t NOT NULL DEFAULT 'dashboard_upload',
    duration_seconds    INTEGER CHECK (duration_seconds >= 0),
    mime_type           TEXT NOT NULL,
    file_size_bytes     BIGINT NOT NULL CHECK (file_size_bytes > 0 AND file_size_bytes <= 26214400),
    processing_state    intel_core.processing_state_t NOT NULL DEFAULT 'QUEUED',
    failure_reason      TEXT,
    uploaded_by         UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webinars_company_id ON intel_core.webinars(company_id);
CREATE INDEX IF NOT EXISTS idx_webinars_state ON intel_core.webinars(processing_state);

-- ------------------------------------------------------------------------------------
-- 4. TABLE: summaries (Gemini structured output + embeddings)
-- ------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intel_core.summaries (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    webinar_id           UUID NOT NULL REFERENCES intel_core.webinars(id) ON DELETE CASCADE,
    company_id           UUID NOT NULL REFERENCES intel_core.companies(id) ON DELETE CASCADE,
    meeting_title        TEXT NOT NULL,
    executive_summary    TEXT NOT NULL,
    key_topics           TEXT[] NOT NULL DEFAULT '{}',
    action_items         JSONB NOT NULL DEFAULT '[]'::jsonb,
    project_deadlines    JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary_embedding    vector(1536),
    raw_transcript_chars INTEGER NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_action_items_is_array CHECK (jsonb_typeof(action_items) = 'array'),
    CONSTRAINT chk_project_deadlines_is_array CHECK (jsonb_typeof(project_deadlines) = 'array')
);

CREATE INDEX IF NOT EXISTS idx_summaries_company_id ON intel_core.summaries(company_id);
CREATE INDEX IF NOT EXISTS idx_summaries_webinar_id ON intel_core.summaries(webinar_id);

-- ANN index for cosine-similarity semantic search (ivfflat requires ANALYZE after bulk load)
CREATE INDEX IF NOT EXISTS idx_summaries_embedding_cosine
    ON intel_core.summaries USING ivfflat (summary_embedding vector_cosine_ops)
    WITH (lists = 100);

-- ------------------------------------------------------------------------------------
-- 5. TABLE: semantic_metrics (search interaction telemetry)
-- ------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intel_core.semantic_metrics (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id    UUID NOT NULL REFERENCES intel_core.companies(id) ON DELETE CASCADE,
    webinar_id    UUID REFERENCES intel_core.webinars(id) ON DELETE SET NULL,
    query_text    TEXT NOT NULL,
    match_count   INTEGER NOT NULL DEFAULT 0,
    search_count  INTEGER NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_semantic_metrics_company_id ON intel_core.semantic_metrics(company_id);

-- Deduplicate identical repeated queries per company so increment_semantic_metric
-- can UPSERT atomically rather than always inserting a new row.
CREATE UNIQUE INDEX IF NOT EXISTS uq_semantic_metrics_company_query
    ON intel_core.semantic_metrics(company_id, query_text);

-- ------------------------------------------------------------------------------------
-- 6. updated_at TRIGGERS
-- ------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION intel_core.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_companies_updated_at ON intel_core.companies;
CREATE TRIGGER trg_companies_updated_at
    BEFORE UPDATE ON intel_core.companies
    FOR EACH ROW EXECUTE FUNCTION intel_core.set_updated_at();

DROP TRIGGER IF EXISTS trg_webinars_updated_at ON intel_core.webinars;
CREATE TRIGGER trg_webinars_updated_at
    BEFORE UPDATE ON intel_core.webinars
    FOR EACH ROW EXECUTE FUNCTION intel_core.set_updated_at();

-- ------------------------------------------------------------------------------------
-- 7. TENANT CLAIM EVALUATOR
--    Reads the active organization_id off the verified Supabase JWT
--    (`request.jwt.claims` is populated by PostgREST/Supabase's auth layer per request).
-- ------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION auth.get_active_tenant_company_id()
RETURNS UUID
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT NULLIF(
        current_setting('request.jwt.claims', true)::jsonb ->> 'organization_id',
        ''
    )::uuid;
$$;

COMMENT ON FUNCTION auth.get_active_tenant_company_id() IS
    'Extracts organization_id from the verified request.jwt.claims context set per-connection by the auth layer. Returns NULL for unauthenticated/service-role sessions, which RLS policies below treat as "deny".';

-- ------------------------------------------------------------------------------------
-- 8. ROW LEVEL SECURITY
-- ------------------------------------------------------------------------------------
ALTER TABLE intel_core.companies        ENABLE ROW LEVEL SECURITY;
ALTER TABLE intel_core.webinars         ENABLE ROW LEVEL SECURITY;
ALTER TABLE intel_core.summaries        ENABLE ROW LEVEL SECURITY;
ALTER TABLE intel_core.semantic_metrics ENABLE ROW LEVEL SECURITY;

ALTER TABLE intel_core.companies        FORCE ROW LEVEL SECURITY;
ALTER TABLE intel_core.webinars         FORCE ROW LEVEL SECURITY;
ALTER TABLE intel_core.summaries        FORCE ROW LEVEL SECURITY;
ALTER TABLE intel_core.semantic_metrics FORCE ROW LEVEL SECURITY;

-- companies: a tenant may only ever see/modify its own row
DROP POLICY IF EXISTS companies_select ON intel_core.companies;
CREATE POLICY companies_select ON intel_core.companies
    FOR SELECT USING (id = auth.get_active_tenant_company_id());

DROP POLICY IF EXISTS companies_update ON intel_core.companies;
CREATE POLICY companies_update ON intel_core.companies
    FOR UPDATE USING (id = auth.get_active_tenant_company_id())
               WITH CHECK (id = auth.get_active_tenant_company_id());

-- Inserts/deletes on companies are intentionally NOT exposed to tenant JWTs;
-- only the service-role key (which bypasses RLS) may provision/deprovision tenants.

-- webinars: full CRUD matrix scoped to organization_id
DROP POLICY IF EXISTS webinars_select ON intel_core.webinars;
CREATE POLICY webinars_select ON intel_core.webinars
    FOR SELECT USING (company_id = auth.get_active_tenant_company_id());

DROP POLICY IF EXISTS webinars_insert ON intel_core.webinars;
CREATE POLICY webinars_insert ON intel_core.webinars
    FOR INSERT WITH CHECK (company_id = auth.get_active_tenant_company_id());

DROP POLICY IF EXISTS webinars_update ON intel_core.webinars;
CREATE POLICY webinars_update ON intel_core.webinars
    FOR UPDATE USING (company_id = auth.get_active_tenant_company_id())
               WITH CHECK (company_id = auth.get_active_tenant_company_id());

DROP POLICY IF EXISTS webinars_delete ON intel_core.webinars;
CREATE POLICY webinars_delete ON intel_core.webinars
    FOR DELETE USING (company_id = auth.get_active_tenant_company_id());

-- summaries: full CRUD matrix scoped to organization_id
DROP POLICY IF EXISTS summaries_select ON intel_core.summaries;
CREATE POLICY summaries_select ON intel_core.summaries
    FOR SELECT USING (company_id = auth.get_active_tenant_company_id());

DROP POLICY IF EXISTS summaries_insert ON intel_core.summaries;
CREATE POLICY summaries_insert ON intel_core.summaries
    FOR INSERT WITH CHECK (company_id = auth.get_active_tenant_company_id());

DROP POLICY IF EXISTS summaries_update ON intel_core.summaries;
CREATE POLICY summaries_update ON intel_core.summaries
    FOR UPDATE USING (company_id = auth.get_active_tenant_company_id())
               WITH CHECK (company_id = auth.get_active_tenant_company_id());

DROP POLICY IF EXISTS summaries_delete ON intel_core.summaries;
CREATE POLICY summaries_delete ON intel_core.summaries
    FOR DELETE USING (company_id = auth.get_active_tenant_company_id());

-- semantic_metrics: full CRUD matrix scoped to organization_id
DROP POLICY IF EXISTS semantic_metrics_select ON intel_core.semantic_metrics;
CREATE POLICY semantic_metrics_select ON intel_core.semantic_metrics
    FOR SELECT USING (company_id = auth.get_active_tenant_company_id());

DROP POLICY IF EXISTS semantic_metrics_insert ON intel_core.semantic_metrics;
CREATE POLICY semantic_metrics_insert ON intel_core.semantic_metrics
    FOR INSERT WITH CHECK (company_id = auth.get_active_tenant_company_id());

DROP POLICY IF EXISTS semantic_metrics_update ON intel_core.semantic_metrics;
CREATE POLICY semantic_metrics_update ON intel_core.semantic_metrics
    FOR UPDATE USING (company_id = auth.get_active_tenant_company_id())
               WITH CHECK (company_id = auth.get_active_tenant_company_id());

DROP POLICY IF EXISTS semantic_metrics_delete ON intel_core.semantic_metrics;
CREATE POLICY semantic_metrics_delete ON intel_core.semantic_metrics
    FOR DELETE USING (company_id = auth.get_active_tenant_company_id());

-- ------------------------------------------------------------------------------------
-- 9. ATOMIC increment_semantic_metric()
--    Safe under concurrent access via UPSERT + row-level lock semantics.
-- ------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION intel_core.increment_semantic_metric(
    p_company_id  UUID,
    p_query_text  TEXT,
    p_match_count INTEGER,
    p_webinar_id  UUID DEFAULT NULL
)
RETURNS intel_core.semantic_metrics
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = intel_core, pg_catalog
AS $$
DECLARE
    v_row intel_core.semantic_metrics;
BEGIN
    IF p_company_id IS NULL THEN
        RAISE EXCEPTION 'increment_semantic_metric: company_id is required';
    END IF;

    INSERT INTO intel_core.semantic_metrics AS sm
        (company_id, webinar_id, query_text, match_count, search_count)
    VALUES
        (p_company_id, p_webinar_id, p_query_text, GREATEST(p_match_count, 0), 1)
    ON CONFLICT (company_id, query_text)
    DO UPDATE SET
        match_count  = GREATEST(EXCLUDED.match_count, 0),
        search_count = sm.search_count + 1,
        webinar_id   = COALESCE(EXCLUDED.webinar_id, sm.webinar_id),
        updated_at   = now()
    RETURNING * INTO v_row;

    RETURN v_row;
END;
$$;

-- ------------------------------------------------------------------------------------
-- 10. SEMANTIC SEARCH RPC (cosine similarity, tenant-scoped inside the function body)
-- ------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION intel_core.match_summaries(
    p_company_id     UUID,
    p_query_embedding vector(1536),
    p_match_threshold FLOAT DEFAULT 0.75,
    p_match_count     INTEGER DEFAULT 10
)
RETURNS TABLE (
    summary_id        UUID,
    webinar_id        UUID,
    meeting_title     TEXT,
    executive_summary TEXT,
    similarity        FLOAT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = intel_core, pg_catalog
AS $$
    SELECT
        s.id,
        s.webinar_id,
        s.meeting_title,
        s.executive_summary,
        1 - (s.summary_embedding <=> p_query_embedding) AS similarity
    FROM intel_core.summaries s
    WHERE s.company_id = p_company_id
      AND s.summary_embedding IS NOT NULL
      AND 1 - (s.summary_embedding <=> p_query_embedding) >= p_match_threshold
    ORDER BY s.summary_embedding <=> p_query_embedding ASC
    LIMIT LEAST(GREATEST(p_match_count, 1), 50);
$$;

-- ------------------------------------------------------------------------------------
-- 11. MONTHLY QUOTA RESET (pg_cron)
--     Runs 00:00 on the 1st of every month, zeroing consumption counters.
-- ------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION intel_core.reset_monthly_usage()
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = intel_core, pg_catalog
AS $$
BEGIN
    UPDATE intel_core.companies
    SET processing_usage_this_month = 0,
        updated_at = now()
    WHERE processing_usage_this_month <> 0;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM cron.job WHERE jobname = 'intel_core_monthly_quota_reset'
    ) THEN
        PERFORM cron.schedule(
            'intel_core_monthly_quota_reset',
            '0 0 1 * *',
            $$SELECT intel_core.reset_monthly_usage();$$
        );
    END IF;
END $$;

COMMIT;
