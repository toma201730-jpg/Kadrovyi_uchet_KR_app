from functools import wraps
from flask import session, redirect, url_for, abort, g
from db import get_db

ROLES = {"admin": "Администратор", "hr": "Кадровик", "manager": "Руководитель"}

# Меню по ролям. Разделы этапов 1+ пока заглушки.
MENU = {
    "admin": [("home", "Главная"), ("employees.list_page", "Сотрудники"), ("orders.list_page", "Приказы"), ("contracts.list_page", "Договоры"), ("leaves.schedule", "Отпуска"), ("admin.directories", "Справочники"), ("orders.templates_page", "Шаблоны"), ("admin.users", "Пользователи"), ("admin.settings_page", "Настройки"), ("audit_page", "Журнал действий")],
    "hr": [("home", "Главная"), ("employees.list_page", "Сотрудники"), ("orders.list_page", "Приказы"), ("contracts.list_page", "Договоры"), ("leaves.schedule", "Отпуска")],
    "manager": [("home", "Главная"), ("employees.list_page", "Сотрудники")],
}

def current_user():
    if "user" not in g:
        uid = session.get("user_id")
        g.user = None
        if uid:
            row = get_db().execute("SELECT * FROM users WHERE id=? AND is_active=1", (uid,)).fetchone()
            g.user = row
    return g.user

def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if current_user() is None:
            session.clear()
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapped

def roles_required(*roles):
    """Проверка прав на сервере: не та роль → 403."""
    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(*a, **kw):
            if current_user()["role"] not in roles:
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco
