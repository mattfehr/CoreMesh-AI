-- Module: Phase 4.1 production log-miner schema migration.
-- Role: upgrades an existing CoreMesh PostgreSQL volume with redacted source
-- logs, run/candidate audit tables, and idempotent golden-dataset lineage.
-- Dependencies: PostgreSQL 16 and the original golden_datasets bootstrap table.
-- Side effects: alters golden_datasets and creates persistent tables/indexes.

BEGIN;

ALTER TABLE golden_datasets
    ADD COLUMN IF NOT EXISTS source_fingerprint CHAR(64),
    ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS golden_datasets_source_fingerprint_uq
    ON golden_datasets (source_fingerprint)
    WHERE source_fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS production_interaction_logs (
    trace_id                VARCHAR(64) PRIMARY KEY,
    feature_scope           VARCHAR(64) NOT NULL,
    redacted_prompt         TEXT        NOT NULL,
    prompt_fingerprint      CHAR(64)    NOT NULL,
    arbitration_scores      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    min_arbitration_score   SMALLINT,
    arbitration_status      VARCHAR(32),
    negative_feedback       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT production_interaction_feature_scope_nonblank
        CHECK (BTRIM(feature_scope) <> ''),
    CONSTRAINT production_interaction_prompt_nonblank
        CHECK (BTRIM(redacted_prompt) <> ''),
    CONSTRAINT production_interaction_score_bounds
        CHECK (min_arbitration_score IS NULL OR min_arbitration_score BETWEEN 1 AND 5)
);

-- Existing volumes may contain malformed historical rows. NOT VALID keeps the
-- upgrade deployable while enforcing the boundary for every future write; the
-- miner also ignores any legacy violations until retention removes them.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'production_interaction_feature_scope_nonblank'
          AND conrelid = 'production_interaction_logs'::regclass
    ) THEN
        ALTER TABLE production_interaction_logs
            ADD CONSTRAINT production_interaction_feature_scope_nonblank
            CHECK (BTRIM(feature_scope) <> '') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'production_interaction_prompt_nonblank'
          AND conrelid = 'production_interaction_logs'::regclass
    ) THEN
        ALTER TABLE production_interaction_logs
            ADD CONSTRAINT production_interaction_prompt_nonblank
            CHECK (BTRIM(redacted_prompt) <> '') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'production_interaction_score_bounds'
          AND conrelid = 'production_interaction_logs'::regclass
    ) THEN
        ALTER TABLE production_interaction_logs
            ADD CONSTRAINT production_interaction_score_bounds
            CHECK (
                min_arbitration_score IS NULL
                OR min_arbitration_score BETWEEN 1 AND 5
            ) NOT VALID;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS production_interaction_logs_eligible_idx
    ON production_interaction_logs (created_at, feature_scope)
    WHERE negative_feedback OR min_arbitration_score < 4;

CREATE INDEX IF NOT EXISTS production_interaction_logs_prompt_fingerprint_idx
    ON production_interaction_logs (prompt_fingerprint);

CREATE TABLE IF NOT EXISTS log_miner_runs (
    run_id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    status                VARCHAR(16) NOT NULL,
    window_start          TIMESTAMPTZ NOT NULL,
    window_end            TIMESTAMPTZ NOT NULL,
    configuration         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    eligible_count        INT         NOT NULL DEFAULT 0,
    embedded_count        INT         NOT NULL DEFAULT 0,
    candidate_count       INT         NOT NULL DEFAULT 0,
    promoted_count        INT         NOT NULL DEFAULT 0,
    pending_review_count  INT         NOT NULL DEFAULT 0,
    duplicate_count       INT         NOT NULL DEFAULT 0,
    error_count           INT         NOT NULL DEFAULT 0,
    purged_count          INT         NOT NULL DEFAULT 0,
    error_message         TEXT,
    started_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at          TIMESTAMPTZ,
    CONSTRAINT log_miner_run_status
        CHECK (status IN ('running', 'completed', 'failed', 'skipped'))
);

ALTER TABLE log_miner_runs
    ADD COLUMN IF NOT EXISTS duplicate_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS purged_count INT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS log_miner_leases (
    lease_name        VARCHAR(128) PRIMARY KEY,
    claim_token       UUID,
    run_id            UUID UNIQUE REFERENCES log_miner_runs(run_id),
    lease_expires_at  TIMESTAMPTZ,
    heartbeat_at      TIMESTAMPTZ,
    CONSTRAINT log_miner_lease_ownership CHECK (
        (claim_token IS NULL AND run_id IS NULL AND lease_expires_at IS NULL)
        OR
        (claim_token IS NOT NULL AND run_id IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

INSERT INTO log_miner_leases (lease_name)
VALUES ('production-log-miner')
ON CONFLICT (lease_name) DO NOTHING;

CREATE TABLE IF NOT EXISTS log_miner_embedding_cache (
    prompt_fingerprint    CHAR(64)     NOT NULL,
    provider_name         VARCHAR(128) NOT NULL,
    embedding_model       VARCHAR(255) NOT NULL,
    input_version         VARCHAR(64)  NOT NULL,
    embedding_dimensions INT          NOT NULL CHECK (embedding_dimensions > 0),
    embedding_values     DOUBLE PRECISION[] NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (
        prompt_fingerprint, provider_name, embedding_model,
        input_version, embedding_dimensions
    ),
    CONSTRAINT log_miner_embedding_width CHECK (
        array_ndims(embedding_values) = 1
        AND cardinality(embedding_values) = embedding_dimensions
    )
);

CREATE INDEX IF NOT EXISTS log_miner_embedding_cache_prompt_idx
    ON log_miner_embedding_cache (prompt_fingerprint);

CREATE TABLE IF NOT EXISTS log_miner_candidates (
    candidate_id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                  UUID        REFERENCES log_miner_runs(run_id) ON DELETE SET NULL,
    source_fingerprint      CHAR(64)    NOT NULL UNIQUE,
    feature_scope           VARCHAR(64) NOT NULL,
    user_input              TEXT        NOT NULL,
    representative_trace_id VARCHAR(64) NOT NULL,
    member_trace_ids        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    cluster_label           INT,
    is_noise                BOOLEAN     NOT NULL DEFAULT FALSE,
    outlier_score           DOUBLE PRECISION,
    expected_output         JSONB       NOT NULL,
    difficulty_rating       VARCHAR(16) NOT NULL,
    label_confidence        DOUBLE PRECISION NOT NULL,
    status                  VARCHAR(24) NOT NULL,
    golden_case_id          UUID        REFERENCES golden_datasets(case_id) ON DELETE SET NULL,
    provenance              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT log_miner_candidate_confidence
        CHECK (label_confidence BETWEEN 0.0 AND 1.0),
    CONSTRAINT log_miner_candidate_status
        CHECK (status IN ('pending_review', 'promoted'))
);

CREATE INDEX IF NOT EXISTS log_miner_candidates_review_idx
    ON log_miner_candidates (status, created_at);

CREATE INDEX IF NOT EXISTS log_miner_candidates_representative_trace_idx
    ON log_miner_candidates (representative_trace_id);

CREATE TABLE IF NOT EXISTS log_miner_candidate_members (
    candidate_id UUID NOT NULL
        REFERENCES log_miner_candidates(candidate_id) ON DELETE CASCADE,
    trace_id VARCHAR(64) NOT NULL,
    PRIMARY KEY (candidate_id, trace_id)
);

CREATE INDEX IF NOT EXISTS log_miner_candidate_members_trace_idx
    ON log_miner_candidate_members (trace_id);

INSERT INTO log_miner_candidate_members (candidate_id, trace_id)
SELECT candidate_id, representative_trace_id
FROM log_miner_candidates
ON CONFLICT (candidate_id, trace_id) DO NOTHING;

INSERT INTO log_miner_candidate_members (candidate_id, trace_id)
SELECT candidate.candidate_id, member.trace_id
FROM log_miner_candidates AS candidate
CROSS JOIN LATERAL jsonb_array_elements_text(
    CASE
        WHEN jsonb_typeof(candidate.member_trace_ids) = 'array'
        THEN candidate.member_trace_ids
        ELSE '[]'::jsonb
    END
) AS member(trace_id)
ON CONFLICT (candidate_id, trace_id) DO NOTHING;

UPDATE log_miner_candidates AS candidate
SET member_trace_ids = normalized.member_trace_ids,
    provenance = COALESCE(candidate.provenance, '{}'::jsonb)
        || jsonb_build_object(
            'member_count', normalized.member_count,
            'cluster_label', candidate.cluster_label,
            'is_noise', candidate.is_noise,
            'outlier_score', candidate.outlier_score
        )
FROM (
    SELECT
        candidate_id,
        jsonb_agg(trace_id ORDER BY trace_id) AS member_trace_ids,
        COUNT(*) AS member_count
    FROM log_miner_candidate_members
    GROUP BY candidate_id
) AS normalized
WHERE normalized.candidate_id = candidate.candidate_id;

UPDATE golden_datasets AS golden
SET provenance = COALESCE(golden.provenance, '{}'::jsonb)
    || COALESCE(candidate.provenance, '{}'::jsonb)
FROM log_miner_candidates AS candidate
WHERE golden.case_id = candidate.golden_case_id;

COMMIT;
