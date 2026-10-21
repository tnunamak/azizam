-- admin.db at schema 6 (before access plans), from gateway.admin_store.init_db
-- and gateway.accounts.initialize at b03f1053. Fixture for tests/test_access_plan_migration.py.
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  issuer TEXT NOT NULL,
  subject TEXT NOT NULL,
  email TEXT,
  display_name TEXT,
  created_at INTEGER NOT NULL,
  last_login_at INTEGER,
  UNIQUE(issuer, subject)
);
CREATE TABLE user_identity_claims (
  user_id TEXT NOT NULL,
  claim_kind TEXT NOT NULL,
  claim_value TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(user_id, claim_kind, claim_value),
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX user_identity_claims_lookup
  ON user_identity_claims(user_id, claim_kind);
CREATE TABLE service_accounts (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at INTEGER NOT NULL,
  disabled_at INTEGER
);
CREATE TABLE apps (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  owner_user_id TEXT,
  owner_service_account_id TEXT,
  default_profile_id TEXT,
  created_at INTEGER NOT NULL,
  disabled_at INTEGER,
  FOREIGN KEY(owner_user_id) REFERENCES users(id),
  FOREIGN KEY(owner_service_account_id) REFERENCES service_accounts(id),
  FOREIGN KEY(default_profile_id) REFERENCES profiles(id)
);
CREATE INDEX apps_owner_user ON apps(owner_user_id);
CREATE INDEX apps_owner_service ON apps(owner_service_account_id);
CREATE TABLE session_apps (
  user_id TEXT PRIMARY KEY,
  app_id TEXT NOT NULL UNIQUE,
  FOREIGN KEY(user_id) REFERENCES users(id),
  FOREIGN KEY(app_id) REFERENCES apps(id)
);
CREATE TABLE profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  profile_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE credentials (
  id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  app_id TEXT,
  profile_id TEXT,
  hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  display_name TEXT,
  scopes_json TEXT,
  created_at INTEGER NOT NULL,
  created_by TEXT,
  expires_at INTEGER,
  revoked_at INTEGER,
  last_used_at INTEGER,
  -- A key bound to a browser sign-in session; revoked when it signs out.
  session_id TEXT,
  FOREIGN KEY(app_id) REFERENCES apps(id),
  FOREIGN KEY(profile_id) REFERENCES profiles(id)
);
CREATE INDEX credentials_subject ON credentials(subject_kind, subject_id);
CREATE INDEX credentials_app ON credentials(app_id);
CREATE INDEX credentials_active ON credentials(revoked_at);
CREATE TRIGGER credentials_never_on_session_apps
BEFORE INSERT ON credentials
WHEN EXISTS (SELECT 1 FROM session_apps WHERE app_id = COALESCE(NEW.app_id, NEW.subject_id))
BEGIN
  SELECT RAISE(ABORT, 'session Apps never hold credentials');
END;
CREATE TRIGGER credentials_never_move_to_session_apps
BEFORE UPDATE OF app_id, subject_id ON credentials
WHEN EXISTS (SELECT 1 FROM session_apps WHERE app_id = COALESCE(NEW.app_id, NEW.subject_id))
BEGIN
  SELECT RAISE(ABORT, 'session Apps never hold credentials');
END;
CREATE TABLE admin_role_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  scope_app_id TEXT,
  granted_at INTEGER NOT NULL,
  granted_by TEXT,
  provenance TEXT NOT NULL DEFAULT 'manual',
  revoked_at INTEGER,
  UNIQUE(role, subject_kind, subject_id, scope_app_id),
  FOREIGN KEY(scope_app_id) REFERENCES apps(id)
);
CREATE INDEX admin_role_subject
  ON admin_role_assignments(subject_kind, subject_id);
CREATE TABLE runtime_profile_mappings (
  id TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  source_value TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  priority INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  created_by TEXT,
  disabled_at INTEGER,
  UNIQUE(source_kind, source_value, profile_id)
);
CREATE INDEX runtime_profile_mappings_lookup
  ON runtime_profile_mappings(source_kind, source_value, disabled_at);
CREATE TABLE local_password_credentials (
  user_id TEXT PRIMARY KEY,
  verifier TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE operator_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  updated_by TEXT
);
CREATE INDEX operator_settings_updated_at
  ON operator_settings(updated_at);
CREATE TABLE operator_settings_generations (
  setting_key TEXT PRIMARY KEY,
  operator_settings_generation INTEGER NOT NULL
);
CREATE TABLE prompt_memory_preferences (
  user_id TEXT PRIMARY KEY,
  remember INTEGER NOT NULL,
  retention TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  updated_by TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE audit_events (
  id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  actor_kind TEXT,
  actor_id TEXT,
  actor_label TEXT,
  action TEXT NOT NULL,
  target_kind TEXT,
  target_id TEXT,
  metadata_json TEXT
);
CREATE INDEX audit_created_at ON audit_events(created_at);
CREATE INDEX audit_target ON audit_events(target_kind, target_id);
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE INDEX credentials_session ON credentials(session_id);
CREATE TABLE account_access (
 user_id TEXT PRIMARY KEY REFERENCES users(id), profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL,
 disabled_at INTEGER, removed_at INTEGER, require_registered_session INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE account_sessions (
 session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL
);
CREATE TABLE account_session_revocations (
 session_id TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
);
CREATE TABLE account_links (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL, user_id TEXT REFERENCES users(id), email TEXT,
 created_by TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 used_at INTEGER, revoked_at INTEGER
);
CREATE TABLE account_oidc_verification (
 user_id TEXT PRIMARY KEY REFERENCES users(id), email_verified INTEGER NOT NULL
);
CREATE TABLE account_login_limits (
 bucket TEXT PRIMARY KEY, started_at INTEGER NOT NULL, attempts INTEGER NOT NULL
);
