from flask import session, has_request_context
from db import get_db

def log(action, entity=None, entity_id=None, details=None, user=None, commit=True):
    """Запись в журнал действий: кто, что, когда."""
    uid = session.get("user_id") if has_request_context() else None  # фоновые задачи работают без запроса
    login = None
    if uid:
        row = get_db().execute("SELECT login FROM users WHERE id=?", (uid,)).fetchone()
        login = row["login"] if row else None
    if user:
        uid, login = user["id"], user["login"]
    get_db().execute(
        "INSERT INTO audit_log (user_id, user_login, action, entity, entity_id, details) VALUES (?,?,?,?,?,?)",
        (uid, login, action, entity, None if entity_id is None else str(entity_id), details),
    )
    if commit:
        get_db().commit()
