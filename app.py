import os, re, secrets, datetime
import click
from flask import Flask, render_template, request, redirect, url_for, session, abort, flash
from werkzeug.security import check_password_hash, generate_password_hash
import db, audit, params
from auth import ROLES, MENU, current_user, login_required, roles_required

MAX_LOGIN_FAILS, LOGIN_WINDOW_MIN, AUDIT_PAGE = 5, 15, 100

def create_app(config=None):
    app = Flask(__name__, static_folder=None)  # статических файлов нет
    if os.environ.get("APP_ENV") == "production" and not os.environ.get("SECRET_KEY") and not (config or {}).get("SECRET_KEY"):
        raise RuntimeError("В production обязательно задайте SECRET_KEY (длинная случайная строка).")
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=8),  # скользящий срок: 8 часов без активности
        DATABASE=os.environ.get("DATABASE", "kadry.db"),
        UPLOAD_DIR=os.environ.get("UPLOAD_DIR", "uploads"),
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    )
    if config:
        app.config.update(config)
    app.config.setdefault("BACKUP_DIR", os.environ.get("BACKUP_DIR", "backups"))
    if os.environ.get("TRUST_PROXY") == "1":  # за обратным прокси (Caddy/nginx): верная схема и адрес клиента
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_app(app)
    from admin import bp as admin_bp
    app.register_blueprint(admin_bp)
    from employees import bp as employees_bp
    app.register_blueprint(employees_bp)
    from orders import bp as orders_bp
    app.register_blueprint(orders_bp)
    from contracts import bp as contracts_bp
    app.register_blueprint(contracts_bp)
    from leaves import bp as leaves_bp
    app.register_blueprint(leaves_bp)
    from reminders import bp as reminders_bp
    app.register_blueprint(reminders_bp)

    @app.before_request
    def csrf_protect():
        if request.method == "POST":
            token = session.get("_csrf")
            if not token or token != request.form.get("_csrf"):
                abort(400)

    @app.context_processor
    def inject():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        user = current_user()
        return dict(csrf_token=session["_csrf"], user=user, roles=ROLES,
                    menu=MENU.get(user["role"], []) if user else [])

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            login_name = request.form.get("login", "").strip()
            row = db.get_db().execute("SELECT * FROM users WHERE login=?", (login_name,)).fetchone()
            fails = db.get_db().execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='login_failed' AND details=? AND at >= datetime('now', ?) "
                "AND id > COALESCE((SELECT MAX(id) FROM audit_log WHERE action='login' AND user_login=?), 0)",
                (login_name, f"-{LOGIN_WINDOW_MIN} minutes", login_name)).fetchone()[0]
            if fails >= MAX_LOGIN_FAILS:
                audit.log("login_blocked", "user", None, details=login_name)
                return render_template("login.html", error=f"Слишком много неудачных попыток. Подождите {LOGIN_WINDOW_MIN} минут и повторите."), 429
            if row and row["is_active"] and check_password_hash(row["password_hash"], request.form.get("password", "")):
                session.clear()
                session.permanent = True
                session["user_id"] = row["id"]
                session["_csrf"] = secrets.token_hex(16)
                audit.log("login", "user", row["id"], user=row)
                return redirect(url_for("home"))
            audit.log("login_failed", "user", None, details=login_name)
            error = "Неверный логин или пароль"
        return render_template("login.html", error=error)

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        audit.log("logout")
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def home():
        import leaves, reminders
        leaves.refresh_statuses()
        user = current_user()
        full = user["role"] in ("admin", "hr")
        events = reminders.upcoming_events()
        for e in events:  # руководитель ведёт только в карточку сотрудника (там ему доступна сокращённая информация)
            e["when"] = reminders.when(e)
            if full and e["kind"] in ("contract_end", "probation_end"):
                e["url"] = url_for("contracts.view", cid=e["ref"])
            elif full and e["kind"] == "leave_start":
                e["url"] = url_for("leaves.view", lid=e["ref"])
            else:
                e["url"] = url_for("employees.card", emp_id=e["employee_id"])
        return render_template("home.html", events=events, summary=reminders.summary(), full=full,
                               mail=reminders.mail_state() if full else None, days=params.get_int("reminder_days"))

    @app.route("/audit")
    @roles_required("admin")
    def audit_page():
        f = {k: request.args.get(k, "").strip() for k in ("user", "action", "from", "to")}
        where, args = [], []
        if f["user"]:
            where.append("user_login LIKE ?"); args.append(f"%{f['user']}%")
        if f["action"]:
            where.append("action = ?"); args.append(f["action"])
        for key, op in (("from", ">="), ("to", "<=")):
            try:
                where.append(f"date(at) {op} ?"); args.append(datetime.date.fromisoformat(f[key]).isoformat())
            except ValueError:
                where.pop()
        page = int(request.args["page"]) if request.args.get("page", "").isdigit() and int(request.args["page"]) > 0 else 1
        sql = "FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "")
        conn = db.get_db()
        total = conn.execute(f"SELECT COUNT(*) {sql}", args).fetchone()[0]
        rows = conn.execute(f"SELECT * {sql} ORDER BY id DESC LIMIT ? OFFSET ?", [*args, AUDIT_PAGE, (page - 1) * AUDIT_PAGE]).fetchall()
        actions = [r[0] for r in conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")]
        query = {k: v for k, v in f.items() if v}
        return render_template("audit.html", rows=rows, f=f, actions=actions, page=page, total=total,
                               pages=max(1, -(-total // AUDIT_PAGE)), query=query)

    @app.route("/health")
    def health():
        return {"status": "ok"}

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'")
        resp.headers["Cache-Control"] = "no-store"  # в ответах персональные данные: не кэшировать
        if app.config["SESSION_COOKIE_SECURE"]:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    def _msg(title, text, code):
        return render_template("page.html", title=title, text=text), code

    @app.errorhandler(400)
    def bad_request(_e):
        return _msg("Запрос отклонён", "Обновите страницу и повторите действие: возможно, истекла сессия.", 400)

    @app.errorhandler(404)
    def not_found(_e):
        return _msg("Не найдено", "Страница или запись не найдена.", 404)

    @app.errorhandler(405)
    def not_allowed(_e):
        return _msg("Действие недоступно", "Этот адрес не поддерживает такое действие.", 405)

    @app.errorhandler(409)
    def conflict(_e):
        return _msg("Действие сейчас недоступно", "Например, документ ещё не проведён или уже изменён.", 409)

    @app.errorhandler(500)
    def server_error(_e):
        db.get_db().rollback()
        return _msg("Внутренняя ошибка", "Попробуйте ещё раз. Если ошибка повторяется, обратитесь к администратору.", 500)

    @app.route("/account", methods=["GET", "POST"])
    @login_required
    def account():
        if request.method == "POST":
            user = current_user()
            cur, new, rep = (request.form.get(k, "") for k in ("current_password", "new_password", "repeat_password"))
            if not check_password_hash(user["password_hash"], cur):
                flash("Текущий пароль неверный.", "err")
            elif len(new) < 8:
                flash("Новый пароль: минимум 8 символов.", "err")
            elif new != rep:
                flash("Новый пароль и повтор не совпадают.", "err")
            elif new == cur:
                flash("Новый пароль должен отличаться от текущего.", "err")
            else:
                db.get_db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), user["id"]))
                db.get_db().commit()
                audit.log("change_password", "user", user["id"])
                flash("Пароль изменён.", "ok")
                return redirect(url_for("account"))
        return render_template("account.html")

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("page.html", title="Нет доступа", text="У вас нет прав на этот раздел."), 403

    @app.errorhandler(413)
    def too_large(_e):
        return render_template("page.html", title="Файл слишком большой", text="Максимальный размер загружаемого файла: 10 МБ."), 413

    @app.cli.command("bootstrap-admin")
    def bootstrap_admin_cmd():
        """Создаёт первого администратора из ADMIN_LOGIN / ADMIN_PASSWORD, только если пользователей ещё нет."""
        conn = db.get_db()
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            click.echo("Пользователи уже есть: администратор не создаётся.")
            return
        login, password = os.environ.get("ADMIN_LOGIN", "").strip(), os.environ.get("ADMIN_PASSWORD", "")
        if not login or not password:
            click.echo("Задайте ADMIN_LOGIN и ADMIN_PASSWORD или создайте пользователя командой create-user.")
            return
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", login) or len(password) < 8:
            raise click.ClickException("ADMIN_LOGIN: 3–40 символов (латиница, цифры, _ . -); ADMIN_PASSWORD: от 8 символов.")
        conn.execute("INSERT INTO users (login, full_name, password_hash, role) VALUES (?,?,?,'admin')",
                     (login, os.environ.get("ADMIN_NAME", "Администратор"), generate_password_hash(password)))
        conn.commit()
        click.echo(f"Администратор {login} создан. После первого входа смените пароль в профиле и удалите ADMIN_PASSWORD из настроек.")

    @app.cli.command("backup")
    @click.option("--out", default="backups", show_default=True, help="Папка для резервных копий")
    @click.option("--keep", default=14, show_default=True, help="Сколько последних копий хранить")
    def backup_cmd(out, keep):
        """Резервная копия базы и файлов. Запускать ежедневно и копировать ZIP за пределы сервера."""
        import backup
        path = backup.make_backup(app.config["DATABASE"], app.config["UPLOAD_DIR"], out, keep)
        click.echo(f"Копия создана: {path}")

    @app.cli.command("restore")
    @click.argument("zip_file")
    @click.option("--force", is_flag=True, help="Заменить существующую базу (приложение должно быть остановлено)")
    def restore_cmd(zip_file, force):
        """Восстановление из резервной копии."""
        import backup
        try:
            n = backup.restore_backup(zip_file, app.config["DATABASE"], app.config["UPLOAD_DIR"], force)
        except (FileExistsError, ValueError) as e:
            raise click.ClickException(str(e))
        click.echo(f"Восстановлено. Файлов: {n}.")

    @app.cli.command("send-reminders")
    def send_reminders_cmd():
        """Ежедневная проверка сроков и рассылка. Запускать по расписанию (cron, Планировщик заданий, облачный cron)."""
        import reminders
        code, msg = reminders.run()
        click.echo(f"{code}: {msg}")

    @app.cli.command("init-db")
    def init_db_cmd():
        db.init_db()
        click.echo("База данных создана.")

    @app.cli.command("create-user")
    @click.argument("login")
    @click.option("--role", type=click.Choice(list(ROLES)), required=True)
    @click.option("--name", default="")
    @click.password_option()
    def create_user_cmd(login, role, name, password):
        db.get_db().execute(
            "INSERT INTO users (login, full_name, password_hash, role) VALUES (?,?,?,?)",
            (login, name, generate_password_hash(password), role))
        db.get_db().commit()
        click.echo(f"Пользователь {login} ({ROLES[role]}) создан.")

    return app

if __name__ == "__main__":
    create_app().run(debug=False)
