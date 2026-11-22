CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  login TEXT NOT NULL UNIQUE,
  full_name TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin','hr','manager')),
  is_active INTEGER NOT NULL DEFAULT 1,
  email TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  user_id INTEGER,
  user_login TEXT,
  action TEXT NOT NULL,
  entity TEXT,
  entity_id TEXT,
  details TEXT
);
CREATE TABLE IF NOT EXISTS doc_counters (
  doc_type TEXT NOT NULL,
  year INTEGER NOT NULL,
  last_number INTEGER NOT NULL,
  PRIMARY KEY (doc_type, year)
);
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  original_name TEXT NOT NULL,
  stored_name TEXT NOT NULL UNIQUE,
  size INTEGER NOT NULL,
  uploaded_by INTEGER,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS organization (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  full_name TEXT NOT NULL DEFAULT '',
  ownership_form TEXT NOT NULL DEFAULT '',
  inn TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  head_name TEXT NOT NULL DEFAULT '',
  head_position TEXT NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO organization (id) VALUES (1);

CREATE TABLE IF NOT EXISTS departments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO settings (key, value) VALUES
  ('annual_leave_days', '28'),
  ('probation_max_months', '3'),
  ('resignation_notice_months', '1');

CREATE TABLE IF NOT EXISTS employees (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  full_name TEXT NOT NULL,
  birth_date TEXT,
  gender TEXT,
  citizenship TEXT,
  pin TEXT UNIQUE,
  passport_number TEXT,
  passport_issued_by TEXT,
  passport_issued_date TEXT,
  address_registration TEXT,
  address_residence TEXT,
  phone TEXT,
  email TEXT,
  social_number TEXT,
  family_status TEXT,
  education TEXT,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','working','on_leave','archived')),
  hire_date TEXT,
  dismissal_date TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS employee_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  start_date TEXT NOT NULL,
  department_id INTEGER REFERENCES departments(id),
  position_id INTEGER REFERENCES positions(id),
  salary REAL,
  working_conditions TEXT,
  basis TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_history_employee ON employee_history (employee_id, start_date);

CREATE TABLE IF NOT EXISTS document_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_type TEXT NOT NULL,
  version INTEGER NOT NULL,
  body TEXT NOT NULL,
  created_by INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (doc_type, version)
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_type TEXT NOT NULL,
  number INTEGER,
  order_date TEXT NOT NULL,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  params TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted')),
  template_version INTEGER,
  rendered_text TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  posted_by INTEGER,
  posted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_number ON orders (substr(order_date, 1, 4), number) WHERE number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_employee ON orders (employee_id);

CREATE TABLE IF NOT EXISTS contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  number INTEGER,
  contract_date TEXT NOT NULL,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  contract_type TEXT NOT NULL CHECK (contract_type IN ('indefinite','fixed')),
  start_date TEXT NOT NULL,
  end_date TEXT,
  probation_days INTEGER NOT NULL DEFAULT 0,
  department_id INTEGER REFERENCES departments(id),
  position_id INTEGER REFERENCES positions(id),
  salary REAL NOT NULL,
  work_mode TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','terminated')),
  template_version INTEGER,
  rendered_text TEXT,
  scan_file_id INTEGER REFERENCES files(id),
  created_by INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  issued_by INTEGER,
  issued_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contracts_number ON contracts (substr(contract_date, 1, 4), number) WHERE number IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_contracts_one_active ON contracts (employee_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_contracts_employee ON contracts (employee_id);

CREATE TABLE IF NOT EXISTS leaves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  leave_type TEXT NOT NULL CHECK (leave_type IN ('annual','unpaid','study','other')),
  leave_name TEXT,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  days INTEGER NOT NULL,
  basis TEXT,
  order_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted')),
  order_id INTEGER REFERENCES orders(id),
  created_by INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  posted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leaves_employee ON leaves (employee_id, start_date);
CREATE TABLE IF NOT EXISTS leave_adjustments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  days INTEGER NOT NULL CHECK (days <> 0),
  reason TEXT NOT NULL,
  created_by INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS termination_grounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('agreement','term_expired','employee','employer','other')),
  article_ref TEXT NOT NULL DEFAULT '',
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS notification_log (
  event_key TEXT PRIMARY KEY,
  claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
  sent_at TEXT,
  recipients TEXT
);
