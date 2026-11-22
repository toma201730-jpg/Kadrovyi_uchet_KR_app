"""Ближайшие сроки: список для главной страницы и рассылка кадровику."""
import datetime, os
from flask import Blueprint, redirect, url_for, flash, current_app
import db, audit, params, documents, leaves, mailer
from auth import roles_required, current_user

bp = Blueprint("reminders", __name__)
KIND_LABEL = {"contract_end": "Окончание срочного договора", "probation_end": "Окончание испытательного срока",
              "leave_start": "Начало отпуска", "dismissal": "Прекращение трудового договора"}

def _d(iso):
    return datetime.date.fromisoformat(iso)

def upcoming_events(today=None, days=None):
    """События до today+days (включая просроченные окончания договоров), по возрастанию даты."""
    conn = db.get_db()
    today = today or leaves.today()
    days = days or params.get_int("reminder_days")
    horizon = today + datetime.timedelta(days=days)
    ev = []

    def add(kind, date, ref, emp_id, name, title):
        ev.append({"kind": kind, "date": date.isoformat(), "ref": ref, "employee_id": emp_id, "employee_name": name,
                   "title": title, "days_left": (date - today).days, "overdue": date < today,
                   "key": f"{kind}:{ref}:{date.isoformat()}"})

    for r in conn.execute("SELECT c.id, c.number, c.end_date, e.id AS eid, e.full_name FROM contracts c JOIN employees e ON e.id=c.employee_id "
                          "WHERE c.status='active' AND c.contract_type='fixed' AND c.end_date IS NOT NULL AND c.end_date<=? "
                          "AND e.dismissal_date IS NULL", (horizon.isoformat(),)):
        end = _d(r["end_date"])
        add("contract_end", end, r["id"], r["eid"], r["full_name"],
            f"Срок договора № {r['number']} истёк, договор всё ещё действует" if end < today else f"Окончание срочного договора № {r['number']}")
    for r in conn.execute("SELECT c.id, c.number, c.start_date, c.probation_days, e.id AS eid, e.full_name FROM contracts c JOIN employees e ON e.id=c.employee_id "
                          "WHERE c.status='active' AND c.probation_days>0 AND e.status IN ('working','on_leave')"):
        end = _d(r["start_date"]) + datetime.timedelta(days=r["probation_days"] - 1)
        if today <= end <= horizon:
            add("probation_end", end, r["id"], r["eid"], r["full_name"], f"Окончание испытательного срока (договор № {r['number']})")
    for r in conn.execute("SELECT l.id, l.start_date, l.days, l.leave_type, l.leave_name, e.id AS eid, e.full_name FROM leaves l JOIN employees e ON e.id=l.employee_id "
                          "WHERE l.status='posted' AND l.start_date>=? AND l.start_date<=?", (today.isoformat(), horizon.isoformat())):
        what = r["leave_name"] if r["leave_type"] == "other" else leaves.LEAVE_TYPES[r["leave_type"]].lower()
        add("leave_start", _d(r["start_date"]), r["id"], r["eid"], r["full_name"], f"Начало отпуска ({what}, {r['days']} дн.)")
    for r in conn.execute("SELECT id, full_name, dismissal_date FROM employees WHERE status IN ('working','on_leave') AND dismissal_date>? AND dismissal_date<=?",
                          (today.isoformat(), horizon.isoformat())):
        add("dismissal", _d(r["dismissal_date"]), r["id"], r["id"], r["full_name"], "Прекращение трудового договора (последний день работы)")
    return sorted(ev, key=lambda e: (e["date"], e["employee_name"]))

def when(e):
    n = e["days_left"]
    return f"просрочено на {-n} дн." if n < 0 else ("сегодня" if n == 0 else ("завтра" if n == 1 else f"через {n} дн."))

def summary():
    conn = db.get_db()
    count = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    return {"working": count("SELECT COUNT(*) FROM employees WHERE status='working'"),
            "on_leave": count("SELECT COUNT(*) FROM employees WHERE status='on_leave'"),
            "new": count("SELECT COUNT(*) FROM employees WHERE status='new'"),
            "contracts": count("SELECT COUNT(*) FROM contracts WHERE status='active'"),
            "drafts": count("SELECT (SELECT COUNT(*) FROM orders WHERE status='draft') + (SELECT COUNT(*) FROM contracts WHERE status='draft') "
                            "+ (SELECT COUNT(*) FROM leaves WHERE status='draft')")}

def recipients():
    """Активные кадровики и администраторы с заполненным email."""
    return [r["email"] for r in db.get_db().execute(
        "SELECT DISTINCT email FROM users WHERE is_active=1 AND role IN ('hr','admin') AND email IS NOT NULL AND email<>'' ORDER BY email")]

def _sender():
    return current_app.config.get("MAIL_SENDER") or (mailer.send if mailer.configured() else None)

def mail_state():
    return {"configured": _sender() is not None, "recipients": len(recipients())}

def _body(events):
    base = os.environ.get("APP_URL", "")
    lines = ["Ближайшие кадровые сроки:", ""]
    for e in events:
        lines.append(f"{documents.fmt_date(e['date'])} — {e['title']}: {e['employee_name']} ({when(e)})")
    if base:
        lines += ["", f"Открыть приложение: {base}"]
    return "\n".join(lines) + "\n"

def run(today=None):
    """Рассылка новых событий. Возвращает (код, сообщение). Событие отправляется один раз (ключ: тип, объект, дата)."""
    conn = db.get_db()
    leaves.refresh_statuses()
    conn.execute("DELETE FROM notification_log WHERE sent_at IS NULL AND claimed_at < datetime('now','-1 hour')")  # зависшие заявки
    conn.commit()
    sender, to = _sender(), recipients()
    events = upcoming_events(today)
    if not events:
        return "empty", "Ближайших сроков нет."
    if sender is None:
        return "not_configured", "Отправка email не настроена (SMTP_HOST и SMTP_FROM)."
    if not to:
        return "no_recipients", "Нет получателей: укажите email у кадровика или администратора."
    mine = [e for e in events if conn.execute("INSERT OR IGNORE INTO notification_log (event_key) VALUES (?)", (e["key"],)).rowcount == 1]
    conn.commit()
    if not mine:
        return "nothing_new", "Новых сроков для отправки нет."
    try:
        sender(to, f"Кадровый учёт КР: ближайшие сроки ({len(mine)})", _body(mine))
    except Exception as exc:
        conn.executemany("DELETE FROM notification_log WHERE event_key=? AND sent_at IS NULL", [(e["key"],) for e in mine])
        conn.commit()
        audit.log("reminders_failed", details=f"{type(exc).__name__}")
        return "failed", "Не удалось отправить письмо, повторим при следующем запуске."
    conn.executemany("UPDATE notification_log SET sent_at=datetime('now'), recipients=? WHERE event_key=?", [(str(len(to)), e["key"]) for e in mine])
    conn.commit()
    audit.log("reminders_sent", details=f"сроков: {len(mine)}, получателей: {len(to)}")
    return "sent", f"Отправлено сроков: {len(mine)}, получателей: {len(to)}."

@bp.route("/reminders/run", methods=["POST"])
@roles_required("admin", "hr")
def run_now():
    code, msg = run()
    flash(msg, "ok" if code in ("sent", "nothing_new", "empty") else "err")
    return redirect(url_for("home"))
