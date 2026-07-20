-- ADR-0007 boundary clarification.
--
-- GenStudio KH is the sole global customer-job, attempt, routing, retry,
-- billing, lease, and fencing authority.  Rows written by Studio Hub are
-- non-authoritative execution evidence and operational telemetry only.
-- The ownership-shaped columns shipped in migration 001 are retained for
-- backward compatibility but MUST NOT be acquired, incremented, or used to
-- claim/transfer work by Studio Hub.

ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS genstudio_job_id TEXT,
  ADD COLUMN IF NOT EXISTS genstudio_attempt_id TEXT,
  ADD COLUMN IF NOT EXISTS external_idempotency_hash TEXT,
  ADD COLUMN IF NOT EXISTS external_fencing_token BIGINT,
  ADD COLUMN IF NOT EXISTS operation TEXT,
  ADD COLUMN IF NOT EXISTS model_revision TEXT,
  ADD COLUMN IF NOT EXISTS voice_revision TEXT,
  ADD COLUMN IF NOT EXISTS evidence_site_id TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_genstudio_evidence
  ON jobs(genstudio_job_id, genstudio_attempt_id, updated_at DESC);

COMMENT ON TABLE jobs IS
  'Non-authoritative Studio Hub execution evidence. GenStudio owns the customer job and attempt.';
COMMENT ON COLUMN jobs.idempotency_key IS
  'Controller-namespaced evidence hash only. GenStudio owns global idempotency.';
COMMENT ON COLUMN jobs.assigned_site IS
  'LEGACY RESERVED. GenStudio owns cross-location routing and site assignment.';
COMMENT ON COLUMN jobs.assigned_machine IS
  'LEGACY RESERVED in PostgreSQL. SQLite owns site-local worker assignment.';
COMMENT ON COLUMN jobs.lease_owner IS
  'LEGACY RESERVED. Studio Hub must never use this column to claim a global job.';
COMMENT ON COLUMN jobs.lease_expires_at IS
  'LEGACY RESERVED. Studio Hub must never use this column to acquire or transfer global ownership.';
COMMENT ON COLUMN jobs.fencing_token IS
  'LEGACY RESERVED. Studio Hub must never generate or increment global fencing tokens.';
COMMENT ON COLUMN jobs.external_fencing_token IS
  'Fencing token supplied by GenStudio and copied as non-authoritative execution evidence.';
COMMENT ON COLUMN jobs.attempt IS
  'LEGACY RESERVED. GenStudio owns global execution attempts.';
COMMENT ON COLUMN job_items.fencing_token IS
  'LEGACY RESERVED. Studio Hub must never generate or increment global fencing tokens.';
COMMENT ON TABLE job_attempts IS
  'LEGACY RESERVED schema. GenStudio owns global attempts; Studio Hub records only site execution evidence.';
COMMENT ON TABLE global_operation_leases IS
  'LEGACY RESERVED schema. Studio Hub must never acquire global customer-job or routing leases.';

INSERT INTO studiohub_schema_migrations(version)
VALUES (2)
ON CONFLICT (version) DO NOTHING;
