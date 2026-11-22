"""Ежедневные задачи внутри контейнера/сервиса: резервная копия (02:00) и напоминания (08:00) по местному времени.
Запуск: python scheduler.py. Если сервис был выключен в нужное время, задача выполнится при старте (догоняет).
Переменные: TZ (по умолчанию Asia/Bishkek), BACKUP_DIR, BACKUP_KEEP, BACKUP_UPLOAD_CMD (команда, которой передаётся
путь к новой копии, например `rclone copyto` или скрипт выгрузки), SCHEDULER_STATE."""
import datetime, json, os, shlex, subprocess, sys, time

SCHEDULE = [("backup", datetime.time(2, 0)), ("reminders", datetime.time(8, 0))]
DONE_CODES = {"sent", "nothing_new", "empty"}
RETRY_SECONDS = 3600

def local_tz():
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(os.environ.get("TZ", "Asia/Bishkek"))
    except Exception:
        return datetime.timezone(datetime.timedelta(hours=6))  # Бишкек: UTC+6 без перехода на летнее время

def log(msg):
    print(f"[scheduler {datetime.datetime.now(local_tz()):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)

def due_jobs(now, state):
    """Какие задачи пора выполнить: время наступило, сегодня ещё не выполнялась, пауза после сбоя прошла."""
    due = []
    for name, at in SCHEDULE:
        scheduled = datetime.datetime.combine(now.date(), at, tzinfo=now.tzinfo)
        if now >= scheduled and state.get(name) != now.date().isoformat() and now.timestamp() >= state.get(name + "_retry", 0):
            due.append(name)
    return due

def run_job(app, name):
    """Возвращает True, если задачу на сегодня можно считать выполненной."""
    import audit, backup, reminders
    with app.app_context():
        if name == "backup":
            path = backup.make_backup(app.config["DATABASE"], app.config["UPLOAD_DIR"], app.config["BACKUP_DIR"],
                                      int(os.environ.get("BACKUP_KEEP", 14)))
            log(f"резервная копия: {path}")
            cmd = os.environ.get("BACKUP_UPLOAD_CMD", "").strip()
            if cmd:  # выгрузка за пределы сервера; сбой не отменяет копию, но фиксируется в журнале
                try:
                    res = subprocess.run([*shlex.split(cmd), path], capture_output=True, text=True, timeout=600)
                    ok = res.returncode == 0
                    err = (res.stderr or res.stdout or "").strip()[-300:]
                except Exception as exc:
                    ok, err = False, type(exc).__name__
                if not ok:
                    log(f"ВНИМАНИЕ: выгрузка копии не удалась: {err}")
                    audit.log("backup_upload_failed", details=err[:200])
                else:
                    log("копия выгружена")
            return True
        code, msg = reminders.run()
        log(f"напоминания: {code}: {msg}")
        return code in DONE_CODES

def tick(app, state_path, now=None, runner=run_job):
    now = now or datetime.datetime.now(local_tz())
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    for name in due_jobs(now, state):
        try:
            done = runner(app, name)
        except Exception as exc:
            log(f"{name}: ошибка {type(exc).__name__}: {exc}")
            done = False
        if done:
            state[name] = now.date().isoformat()
            state.pop(name + "_retry", None)
        else:
            state[name + "_retry"] = now.timestamp() + RETRY_SECONDS
    os.makedirs(os.path.dirname(os.path.abspath(state_path)), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return state

def main():
    from app import create_app
    app = create_app()
    state_path = os.environ.get("SCHEDULER_STATE", os.path.join(os.path.dirname(os.path.abspath(app.config["DATABASE"])), ".scheduler_state.json"))
    log(f"запущен; состояние: {state_path}")
    while True:
        tick(app, state_path)
        time.sleep(60)

if __name__ == "__main__":
    main()
