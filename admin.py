import re
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from werkzeug.security import generate_password_hash
import db, audit, params
from auth import ROLES, roles_required, current_user

bp = Blueprint("admin", __name__)
KINDS = {"departments": "Подразделения", "positions": "Должности"}
MIN_PASSWORD = 8
EMAIL_RE = re.compile(r"[^@\s]{1,100}@[^@\s]+\.[^@\s]+")

def _kind(kind):
    if kind not in KINDS:
        abort(404)
    return kind

def _active_admins():
    return db.get_db().execute("SELECT COUNT(*) c FROM users WHERE role='admin' AND is_active=1").fetchone()["c"]

def _is_last_admin(u):
    return u["role"] == "admin" and u["is_active"] and _active_admins() <= 1

# ---------- справочники ----------
@bp.route("/directories")
@roles_required("admin")
def directories():
    data = {k: db.get_db().execute(f"SELECT * FROM {k} ORDER BY is_active DESC, name").fetchall() for k in KINDS}
    return render_template("directories.html", data=data, kinds=KINDS)

@bp.route("/directories/<kind>/add", methods=["POST"])
@roles_required("admin")
def add_item(kind):
    kind = _kind(kind)
    name = " ".join(request.form.get("name", "").split())
    existing = [r["name"].casefold() for r in db.get_db().execute(f"SELECT name FROM {kind}")]
    if not name or len(name) > 200:
        flash("Введите название (до 200 символов).", "err")
    elif name.casefold() in existing:
        flash("Такое название уже есть.", "err")
    else:
        cur = db.get_db().execute(f"INSERT INTO {kind} (name) VALUES (?)", (name,))
        db.get_db().commit()
        audit.log("create", kind, cur.lastrowid, name)
        flash("Добавлено.", "ok")
    return redirect(url_for("admin.directories"))

@bp.route("/directories/<kind>/<int:item_id>/toggle", methods=["POST"])
@roles_required("admin")
def toggle_item(kind, item_id):
    kind = _kind(kind)
    row = db.get_db().execute(f"SELECT * FROM {kind} WHERE id=?", (item_id,)).fetchone()
    if not row:
        abort(404)
    new = 0 if row["is_active"] else 1
    db.get_db().execute(f"UPDATE {kind} SET is_active=? WHERE id=?", (new, item_id))
    db.get_db().commit()
    audit.log("restore" if new else "archive", kind, item_id, row["name"])
    return redirect(url_for("admin.directories"))

# ---------- настройки ----------
@bp.route("/settings")
@roles_required("admin")
def settings_page():
    org = db.get_db().execute("SELECT * FROM organization WHERE id=1").fetchone()
    rows = [(k, params.DEFS[k][0], params.get_int(k), params.DEFS[k][2], params.DEFS[k][3]) for k in params.NORMATIVE]
    reminder = (params.get_int("reminder_days"), params.DEFS["reminder_days"][2], params.DEFS["reminder_days"][3])
    return render_template("settings.html", org=org, params=rows, reminder=reminder)

@bp.route("/settings/organization", methods=["POST"])
@roles_required("admin")
def save_organization():
    f = {k: " ".join(request.form.get(k, "").split()) for k in
         ("full_name", "ownership_form", "inn", "address", "head_name", "head_position")}
    if any(len(v) > 300 for v in f.values()):
        flash("Слишком длинное значение (максимум 300 символов).", "err")
    elif f["inn"] and not re.fullmatch(r"\d{1,14}", f["inn"]):
        flash("ИНН должен состоять только из цифр (до 14).", "err")
    else:
        db.get_db().execute(
            "UPDATE organization SET full_name=?, ownership_form=?, inn=?, address=?, head_name=?, head_position=? WHERE id=1",
            (f["full_name"], f["ownership_form"], f["inn"], f["address"], f["head_name"], f["head_position"]))
        db.get_db().commit()
        audit.log("update", "organization", 1)
        flash("Данные организации сохранены.", "ok")
    return redirect(url_for("admin.settings_page"))

@bp.route("/settings/params", methods=["POST"])
@roles_required("admin")
def save_params():
    new, errors = {}, []
    for key in params.NORMATIVE:
        label, _default, lo, hi = params.DEFS[key]
        raw = request.form.get(key, "").strip()
        if not raw.isdigit() or not lo <= int(raw) <= hi:
            errors.append(f"«{label}»: целое число от {lo} до {hi}.")
        else:
            new[key] = int(raw)
    if errors:
        for e in errors:
            flash(e, "err")
    else:
        changes = []
        for key, value in new.items():
            old = params.get_int(key)
            if old != value:
                params.set_int(key, value)
                changes.append(f"{key}: {old} → {value}")
        if changes:
            audit.log("update", "settings", None, "; ".join(changes))
        flash("Параметры сохранены. Проверьте их у юриста по действующей редакции ТК КР.", "ok")
    return redirect(url_for("admin.settings_page"))

@bp.route("/settings/reminders", methods=["POST"])
@roles_required("admin")
def save_reminders():
    label, default, lo, hi = params.DEFS["reminder_days"]
    raw = request.form.get("reminder_days", "").strip()
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        flash(f"«{label}»: целое число от {lo} до {hi}.", "err")
    else:
        old = params.get_int("reminder_days")
        if old != int(raw):
            params.set_int("reminder_days", int(raw))
            audit.log("update", "settings", None, f"reminder_days: {old} → {int(raw)}")
        flash("Настройка сохранена.", "ok")
    return redirect(url_for("admin.settings_page"))

# ---------- пользователи ----------
@bp.route("/users")
@roles_required("admin")
def users():
    rows = db.get_db().execute("SELECT * FROM users ORDER BY is_active DESC, login").fetchall()
    return render_template("users.html", rows=rows)

@bp.route("/users/create", methods=["POST"])
@roles_required("admin")
def create_user():
    login = request.form.get("login", "").strip()
    name = " ".join(request.form.get("full_name", "").split())
    role = request.form.get("role", "")
    password = request.form.get("password", "")
    email = request.form.get("email", "").strip()
    taken = db.get_db().execute("SELECT 1 FROM users WHERE login=? COLLATE NOCASE", (login,)).fetchone()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", login):
        flash("Логин: 3–40 символов, латиница, цифры, _ . -", "err")
    elif taken:
        flash("Такой логин уже есть.", "err")
    elif role not in ROLES:
        flash("Выберите роль.", "err")
    elif len(password) < MIN_PASSWORD:
        flash(f"Пароль: минимум {MIN_PASSWORD} символов.", "err")
    elif email and not EMAIL_RE.fullmatch(email):
        flash("Email: неверный формат.", "err")
    else:
        cur = db.get_db().execute(
            "INSERT INTO users (login, full_name, password_hash, role, email) VALUES (?,?,?,?,?)",
            (login, name, generate_password_hash(password), role, email or None))
        db.get_db().commit()
        audit.log("create", "user", cur.lastrowid, f"{login}, роль {role}")
        flash("Пользователь создан.", "ok")
    return redirect(url_for("admin.users"))

def _user_or_404(user_id):
    u = db.get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not u:
        abort(404)
    return u

@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@roles_required("admin")
def toggle_user(user_id):
    u = _user_or_404(user_id)
    if u["is_active"] and (u["id"] == current_user()["id"] or _is_last_admin(u)):
        flash("Нельзя отключить себя или последнего администратора.", "err")
    else:
        new = 0 if u["is_active"] else 1
        db.get_db().execute("UPDATE users SET is_active=? WHERE id=?", (new, user_id))
        db.get_db().commit()
        audit.log("deactivate" if not new else "activate", "user", user_id, u["login"])
    return redirect(url_for("admin.users"))

@bp.route("/users/<int:user_id>/role", methods=["POST"])
@roles_required("admin")
def change_role(user_id):
    u = _user_or_404(user_id)
    role = request.form.get("role", "")
    if role not in ROLES:
        flash("Выберите роль.", "err")
    elif role != u["role"] and (u["id"] == current_user()["id"] or _is_last_admin(u)):
        flash("Нельзя менять роль себе или последнему администратору.", "err")
    elif role != u["role"]:
        db.get_db().execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        db.get_db().commit()
        audit.log("change_role", "user", user_id, f"{u['login']}: {u['role']} → {role}")
    return redirect(url_for("admin.users"))

@bp.route("/users/<int:user_id>/password", methods=["POST"])
@roles_required("admin")
def reset_password(user_id):
    u = _user_or_404(user_id)
    password = request.form.get("password", "")
    if len(password) < MIN_PASSWORD:
        flash(f"Пароль: минимум {MIN_PASSWORD} символов.", "err")
    else:
        db.get_db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(password), user_id))
        db.get_db().commit()
        audit.log("reset_password", "user", user_id, u["login"])  # сам пароль в журнал не пишется
        flash("Пароль изменён.", "ok")
    return redirect(url_for("admin.users"))

# ---------- основания прекращения трудового договора ----------
GROUND_KINDS = {"agreement": "Соглашение сторон", "term_expired": "Истечение срока договора", "employee": "Инициатива работника",
                "employer": "Инициатива работодателя", "other": "Иное"}

@bp.route("/directories/grounds")
@roles_required("admin")
def grounds():
    rows = db.get_db().execute("SELECT * FROM termination_grounds ORDER BY is_active DESC, id").fetchall()
    return render_template("grounds.html", rows=rows, kinds=GROUND_KINDS)

@bp.route("/directories/grounds/add", methods=["POST"])
@roles_required("admin")
def add_ground():
    name = " ".join(request.form.get("name", "").split())
    kind = request.form.get("kind", "")
    ref = " ".join(request.form.get("article_ref", "").split())
    exists = any(r["name"].casefold() == name.casefold() for r in db.get_db().execute("SELECT name FROM termination_grounds"))
    if not name or len(name) > 200 or len(ref) > 200 or kind not in GROUND_KINDS:
        flash("Укажите название (до 200 символов) и тип основания.", "err")
    elif exists:
        flash("Такое основание уже есть.", "err")
    else:
        cur = db.get_db().execute("INSERT INTO termination_grounds (name, kind, article_ref) VALUES (?,?,?)", (name, kind, ref))
        db.get_db().commit()
        audit.log("create", "termination_grounds", cur.lastrowid, name)
        flash("Основание добавлено.", "ok")
    return redirect(url_for("admin.grounds"))

@bp.route("/directories/grounds/<int:gid>/update", methods=["POST"])
@roles_required("admin")
def update_ground(gid):
    g = db.get_db().execute("SELECT * FROM termination_grounds WHERE id=?", (gid,)).fetchone()
    if not g:
        abort(404)
    name = " ".join(request.form.get("name", "").split())
    ref = " ".join(request.form.get("article_ref", "").split())
    clash = any(r["name"].casefold() == name.casefold() and r["id"] != gid for r in db.get_db().execute("SELECT id, name FROM termination_grounds"))
    if not name or len(name) > 200 or len(ref) > 200:
        flash("Название обязательно (до 200 символов), ссылка до 200 символов.", "err")
    elif clash:
        flash("Такое основание уже есть.", "err")
    else:
        db.get_db().execute("UPDATE termination_grounds SET name=?, article_ref=? WHERE id=?", (name, ref, gid))
        db.get_db().commit()
        audit.log("update", "termination_grounds", gid, name)  # тип основания менять нельзя: от него зависят проверки
        flash("Сохранено.", "ok")
    return redirect(url_for("admin.grounds"))

@bp.route("/directories/grounds/<int:gid>/toggle", methods=["POST"])
@roles_required("admin")
def toggle_ground(gid):
    g = db.get_db().execute("SELECT * FROM termination_grounds WHERE id=?", (gid,)).fetchone()
    if not g:
        abort(404)
    new = 0 if g["is_active"] else 1
    db.get_db().execute("UPDATE termination_grounds SET is_active=? WHERE id=?", (new, gid))
    db.get_db().commit()
    audit.log("restore" if new else "archive", "termination_grounds", gid, g["name"])
    return redirect(url_for("admin.grounds"))

@bp.route("/users/<int:user_id>/email", methods=["POST"])
@roles_required("admin")
def set_email(user_id):
    u = _user_or_404(user_id)
    email = request.form.get("email", "").strip()
    if email and (len(email) > 150 or not EMAIL_RE.fullmatch(email)):
        flash("Email: неверный формат.", "err")
    else:
        db.get_db().execute("UPDATE users SET email=? WHERE id=?", (email or None, user_id))
        db.get_db().commit()
        audit.log("set_email", "user", user_id, u["login"])  # сам адрес в журнал не пишется
        flash("Email сохранён." if email else "Email удалён: уведомления приходить не будут.", "ok")
    return redirect(url_for("admin.users"))
