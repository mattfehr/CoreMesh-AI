-- CoreMesh AI — PostgreSQL 16 Initialization Script
-- Schemas: prompt_registry, feature_experiments, golden_datasets
-- Mounted by docker-compose into /docker-entrypoint-initdb.d/ and executed on first boot.

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
    created_at       TIMESTAMP WITHOUT TIME ZONE DEFAULT TIMEZONE('utc', NOW())
);
