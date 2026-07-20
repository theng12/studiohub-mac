CREATE TABLE IF NOT EXISTS studiohub_schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sites (
  site_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS controllers (
  controller_id TEXT PRIMARY KEY,
  site_id TEXT NOT NULL REFERENCES sites(site_id),
  role TEXT NOT NULL CHECK (role IN ('controller', 'agent', 'standalone')),
  hostname TEXT NOT NULL,
  app_version TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ready BOOLEAN NOT NULL DEFAULT FALSE,
  migration_stage TEXT NOT NULL DEFAULT 'shadow',
  capacity JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_controllers_site_seen
  ON controllers(site_id, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS machines (
  machine_id TEXT PRIMARY KEY,
  site_id TEXT NOT NULL REFERENCES sites(site_id),
  authority_controller_id TEXT REFERENCES controllers(controller_id),
  address TEXT,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  reachable BOOLEAN NOT NULL DEFAULT FALSE,
  facts JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_seen_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_machines_site ON machines(site_id);

CREATE TABLE IF NOT EXISTS studios (
  studio_id TEXT PRIMARY KEY,
  runtime_id TEXT NOT NULL,
  machine_id TEXT NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
  site_id TEXT NOT NULL REFERENCES sites(site_id),
  modality TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  status TEXT NOT NULL DEFAULT 'unknown',
  app_version TEXT,
  capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_seen_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(site_id, machine_id, modality)
);
CREATE INDEX IF NOT EXISTS idx_studios_site_modality
  ON studios(site_id, modality, status);

-- Shadowed in the first migration stage. These rows do not become globally
-- claimable until the lease/fencing migration is explicitly enabled.
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  local_job_id TEXT NOT NULL,
  source_controller_id TEXT NOT NULL REFERENCES controllers(controller_id),
  site_id TEXT NOT NULL REFERENCES sites(site_id),
  tenant_id TEXT NOT NULL DEFAULT 'default',
  job_kind TEXT NOT NULL,
  idempotency_key TEXT,
  request_fingerprint TEXT,
  state TEXT NOT NULL,
  assigned_site TEXT,
  assigned_machine TEXT,
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  fencing_token BIGINT NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 0,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  UNIQUE(tenant_id, job_kind, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_claimable
  ON jobs(state, assigned_site, lease_expires_at, created_at);

CREATE TABLE IF NOT EXISTS job_items (
  job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  item_id TEXT NOT NULL,
  item_index INTEGER NOT NULL,
  state TEXT NOT NULL,
  assigned_machine TEXT,
  assigned_studio TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  fencing_token BIGINT NOT NULL DEFAULT 0,
  artifact_url TEXT,
  artifact_sha256 TEXT,
  error TEXT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(job_id, item_id)
);

CREATE TABLE IF NOT EXISTS job_attempts (
  attempt_id BIGSERIAL PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  item_id TEXT,
  controller_id TEXT REFERENCES controllers(controller_id),
  machine_id TEXT,
  studio_id TEXT,
  attempt INTEGER NOT NULL,
  fencing_token BIGINT NOT NULL,
  state TEXT NOT NULL,
  error TEXT,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS global_operation_leases (
  operation TEXT PRIMARY KEY,
  lease_owner TEXT NOT NULL REFERENCES controllers(controller_id),
  lease_expires_at TIMESTAMPTZ NOT NULL,
  fencing_token BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_events (
  event_id BIGSERIAL PRIMARY KEY,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  controller_id TEXT,
  site_id TEXT,
  actor TEXT,
  action TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  details JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_events_time
  ON audit_events(occurred_at DESC);

INSERT INTO studiohub_schema_migrations(version)
VALUES (1)
ON CONFLICT (version) DO NOTHING;
