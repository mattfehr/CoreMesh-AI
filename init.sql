-- Module: CoreMesh PostgreSQL bootstrap schema.
-- Role: creates the prompt/experiment/evaluation metadata contracts consumed
-- by planned control-plane features and the gateway autopilot experiment store.
-- Dependencies: PostgreSQL 16; mounted by docker-compose.yml under
-- /docker-entrypoint-initdb.d/.
-- Side effects: creates persistent tables and supporting indexes. This
-- script is intentionally non-idempotent and runs only for a new volume.

-- ---------------------------------------------------------------------------
-- [Project 9] Prompt Version Registry
-- Stores versioned system prompts with model parameters and activation state.
-- ---------------------------------------------------------------------------
CREATE TABLE prompt_registry (
    prompt_id         VARCHAR(64)  NOT NULL,
    version_id        INT          NOT NULL,
    system_prompt     TEXT         NOT NULL,
    few_shot_examples JSONB        DEFAULT '[]'::jsonb,
    model_parameters  JSONB        NOT NULL,  -- {"temperature": 0.2, "max_tokens": 1000}
    commit_message    TEXT,
    is_active         BOOLEAN      DEFAULT FALSE,
    created_at        TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW()),
    PRIMARY KEY (prompt_id, version_id)
);

-- ---------------------------------------------------------------------------
-- [Project 9, 12] Experiment Splits and Feature Flag Configurations
-- Controls A/B traffic splits and automatic rollback thresholds.
-- ---------------------------------------------------------------------------
CREATE TABLE feature_experiments (
    flag_name                    VARCHAR(64)    PRIMARY KEY,
    rollout_percentage           INT            NOT NULL DEFAULT 0,        -- 0 to 100
    quality_threshold_p10        NUMERIC(3, 2)  NOT NULL,                  -- rollback trigger
    baseline_prompt_version      INT            NOT NULL,
    experimental_prompt_version  INT            NOT NULL,
    status                       VARCHAR(32)    NOT NULL DEFAULT 'draft',  -- 'running', 'rolled_back', 'completed'
    updated_at                   TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
);

-- ---------------------------------------------------------------------------
-- [Project 1, 13] Master Evaluation Dataset Store
-- Golden test cases produced by human curation and production log mining.
-- ---------------------------------------------------------------------------
CREATE TABLE golden_datasets (
    case_id          UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_scope    VARCHAR(64)   NOT NULL,
    user_input       TEXT          NOT NULL,
    expected_output  JSONB         NOT NULL,
    difficulty_rating VARCHAR(16)  NOT NULL,  -- 'simple', 'moderate', 'hard', 'adversarial'
    origin_source    VARCHAR(32)   NOT NULL,  -- 'human_curated', 'production_miner'
    source_fingerprint CHAR(64),
    provenance       JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
);

CREATE UNIQUE INDEX golden_datasets_source_fingerprint_uq
    ON golden_datasets (source_fingerprint)
    WHERE source_fingerprint IS NOT NULL;

-- ---------------------------------------------------------------------------
-- [Project 13] Privacy-approved Production Interaction Log
-- Retains only redacted prompts and bounded arbitration metadata for 30 days.
-- ---------------------------------------------------------------------------
CREATE TABLE production_interaction_logs (
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

CREATE INDEX production_interaction_logs_eligible_idx
    ON production_interaction_logs (created_at, feature_scope)
    WHERE negative_feedback OR min_arbitration_score < 4;

CREATE INDEX production_interaction_logs_prompt_fingerprint_idx
    ON production_interaction_logs (prompt_fingerprint);

-- ---------------------------------------------------------------------------
-- [Project 13] Log-miner Run Audit
-- Records one scheduled/manual execution without retaining prompt content.
-- ---------------------------------------------------------------------------
CREATE TABLE log_miner_runs (
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

-- One renewable lease fences the expensive provider work without pinning a
-- PostgreSQL session. An expired owner can be recovered by the next run.
CREATE TABLE log_miner_leases (
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

-- Derived vectors contain no prompt text and expire as soon as no retained
-- source prompt references their fingerprint.
CREATE TABLE log_miner_embedding_cache (
    prompt_fingerprint    CHAR(64)    NOT NULL,
    provider_name         VARCHAR(128) NOT NULL,
    embedding_model       VARCHAR(255) NOT NULL,
    input_version         VARCHAR(64) NOT NULL,
    embedding_dimensions INT         NOT NULL CHECK (embedding_dimensions > 0),
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

CREATE INDEX log_miner_embedding_cache_prompt_idx
    ON log_miner_embedding_cache (prompt_fingerprint);

-- ---------------------------------------------------------------------------
-- [Project 13] Generated Candidate Review Queue
-- High-confidence candidates link to a promoted golden case; lower-confidence
-- candidates remain reviewable until the worker's retention cleanup removes them.
-- ---------------------------------------------------------------------------
CREATE TABLE log_miner_candidates (
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

CREATE INDEX log_miner_candidates_review_idx
    ON log_miner_candidates (status, created_at);

CREATE INDEX log_miner_candidates_representative_trace_idx
    ON log_miner_candidates (representative_trace_id);

-- Normalized membership supports indexed audit queries and idempotent cluster
-- evolution. trace_id is intentionally non-unique across candidate versions.
CREATE TABLE log_miner_candidate_members (
    candidate_id UUID NOT NULL
        REFERENCES log_miner_candidates(candidate_id) ON DELETE CASCADE,
    trace_id VARCHAR(64) NOT NULL,
    PRIMARY KEY (candidate_id, trace_id)
);

CREATE INDEX log_miner_candidate_members_trace_idx
    ON log_miner_candidate_members (trace_id);
