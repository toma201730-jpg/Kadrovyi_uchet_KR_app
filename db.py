import sqlite3
from flask import current_app, g

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")  # читатели не блокируют писателя
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db

def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()

def init_db():
    with current_app.open_resource("schema.sql") as f:
        get_db().executescript(f.read().decode("utf-8"))
    get_db().commit()
    migrate()
    import documents
    documents.ensure_defaults()

def migrate():
    """Добавляет колонки, появившиеся после первого выпуска (CREATE IF NOT EXISTS старые таблицы не меняет)."""
    conn = get_db()
    if "email" not in [r["name"] for r in conn.execute("PRAGMA table_info(users)")]:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    conn.commit()

def init_app(app):
    app.teardown_appcontext(close_db)
