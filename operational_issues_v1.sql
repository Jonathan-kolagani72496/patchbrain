-- PatchBrain Phase 2: Operational Issues v1
-- Migration: operational_issues_v1.sql
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- TABLE: operational_issues
-- ============================================================
CREATE TABLE IF NOT EXISTS operational_issues (
    id                   uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id           uuid             NOT NULL,
    issue_type           text             NOT NULL,
    compliance_framework text             NOT NULL DEFAULT 'internal',
    status               text             NOT NULL DEFAULT 'candidate',
    severity             text             NOT NULL DEFAULT 'medium',
    title                text             NOT NULL,
    description          text,
    related_entities     jsonb            NOT NULL DEFAULT '{}'::jsonb,
    related_files        jsonb            NOT NULL DEFAULT '[]'::jsonb,
    raw_data             jsonb            NOT NULL DEFAULT '{}'::jsonb,
    confidence_score     double precision NOT NULL DEFAULT 1.0,
    suppression_hash     text,
    rule_key             text             NOT NULL,
    rule_version         int              NOT NULL DEFAULT 1,
    created_at           timestamptz      NOT NULL DEFAULT now(),
    updated_at           timestamptz      NOT NULL DEFAULT now(),
    resolved_at          timestamptz
);

-- Check constraints (idempotent via exception handling)
DO $$ BEGIN
    ALTER TABLE operational_issues
        ADD CONSTRAINT chk_operational_issues_status
        CHECK (status IN ('candidate','confirmed','dismissed','resolved','ignored'));
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE operational_issues
        ADD CONSTRAINT chk_operational_issues_severity
        CHECK (severity IN ('critical','high','medium','low'));
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE operational_issues
        ADD CONSTRAINT chk_operational_issues_compliance_framework
        CHECK (compliance_framework IN ('FMCSA','PHMSA','OSHA','DOT','internal','internal_safety'));
EXCEPTION WHEN duplicate_object THEN null;
END $$;

-- Indexes on operational_issues
CREATE INDEX IF NOT EXISTS idx_operational_issues_company_id       ON operational_issues(company_id);
CREATE INDEX IF NOT EXISTS idx_operational_issues_status           ON operational_issues(status);
CREATE INDEX IF NOT EXISTS idx_operational_issues_issue_type       ON operational_issues(issue_type);
CREATE INDEX IF NOT EXISTS idx_operational_issues_severity         ON operational_issues(severity);
CREATE INDEX IF NOT EXISTS idx_operational_issues_rule_key         ON operational_issues(rule_key);
CREATE INDEX IF NOT EXISTS idx_operational_issues_suppression_hash ON operational_issues(suppression_hash);
CREATE INDEX IF NOT EXISTS idx_operational_issues_created_at       ON operational_issues(created_at);
CREATE INDEX IF NOT EXISTS idx_operational_issues_company_status   ON operational_issues(company_id, status);
CREATE INDEX IF NOT EXISTS idx_operational_issues_company_hash     ON operational_issues(company_id, suppression_hash);

-- ============================================================
-- TABLE: issue_evidence
-- ============================================================
CREATE TABLE IF NOT EXISTS issue_evidence (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       uuid        NOT NULL,
    issue_id         uuid        NOT NULL REFERENCES operational_issues(id) ON DELETE CASCADE,
    file_id          uuid,
    file_version_id  uuid,
    chunk_id         uuid,
    source_filename  text,
    source_label     text,
    evidence_text    text        NOT NULL,
    evidence_json    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- Indexes on issue_evidence
CREATE INDEX IF NOT EXISTS idx_issue_evidence_company_id      ON issue_evidence(company_id);
CREATE INDEX IF NOT EXISTS idx_issue_evidence_issue_id        ON issue_evidence(issue_id);
CREATE INDEX IF NOT EXISTS idx_issue_evidence_file_id         ON issue_evidence(file_id);
CREATE INDEX IF NOT EXISTS idx_issue_evidence_file_version_id ON issue_evidence(file_version_id);
CREATE INDEX IF NOT EXISTS idx_issue_evidence_chunk_id        ON issue_evidence(chunk_id);

-- ============================================================
-- TABLE: issue_rule_runs
-- ============================================================
CREATE TABLE IF NOT EXISTS issue_rule_runs (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id         uuid        NOT NULL,
    rule_key           text        NOT NULL,
    rule_version       int         NOT NULL DEFAULT 1,
    candidates_found   int         NOT NULL DEFAULT 0,
    issues_created     int         NOT NULL DEFAULT 0,
    issues_suppressed  int         NOT NULL DEFAULT 0,
    errors             jsonb       NOT NULL DEFAULT '[]'::jsonb,
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz
);

-- Indexes on issue_rule_runs
CREATE INDEX IF NOT EXISTS idx_issue_rule_runs_company_id  ON issue_rule_runs(company_id);
CREATE INDEX IF NOT EXISTS idx_issue_rule_runs_rule_key    ON issue_rule_runs(rule_key);
CREATE INDEX IF NOT EXISTS idx_issue_rule_runs_started_at  ON issue_rule_runs(started_at);

-- Run this in Supabase SQL editor before deploying backend.
-- Idempotent: safe to re-run.
