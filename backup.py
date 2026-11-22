"""Резервная копия: согласованный снимок базы SQLite + загруженные файлы в одном ZIP."""
import datetime, glob, json, os, re, shutil, sqlite3, tempfile, zipfile

PATTERN = "kadry-backup-*.zip"

def make_backup(db_path, upload_dir, out_dir, keep=None):
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = os.path.join(out_dir, f"kadry-backup-{stamp}.zip")
    with tempfile.TemporaryDirectory() as tmp:
        snap = os.path.join(tmp, "kadry.db")
        src, dst = sqlite3.connect(db_path), sqlite3.connect(snap)
        try:
            src.backup(dst)  # корректно и при работающем приложении
        finally:
            dst.close(); src.close()
        files = sorted(os.listdir(upload_dir)) if os.path.isdir(upload_dir) else []
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "kadry.db")
            for f in files:
                z.write(os.path.join(upload_dir, f), f"uploads/{f}")
            z.writestr("manifest.json", json.dumps({"created_utc": stamp, "uploads": len(files)}))
    if keep:
        for old in sorted(glob.glob(os.path.join(out_dir, PATTERN)))[:-keep]:
            os.remove(old)
    return target

def restore_backup(zip_path, db_path, upload_dir, force=False):
    """Восстановление. Приложение на это время должно быть остановлено."""
    if os.path.exists(db_path) and not force:
        raise FileExistsError("База уже существует: остановите приложение и используйте --force.")
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        ok = lambda n: n in ("kadry.db", "manifest.json") or re.fullmatch(r"uploads/[0-9a-f]{32}", n)  # без путей наружу
        if "kadry.db" not in names or not all(ok(n) for n in names):
            raise ValueError("Это не резервная копия «Кадрового учёта КР» или архив повреждён.")
        with tempfile.TemporaryDirectory() as tmp:
            z.extract("kadry.db", tmp)
            check = sqlite3.connect(os.path.join(tmp, "kadry.db"))
            try:
                if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Проверка целостности базы в копии не пройдена.")
                tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"users", "employees", "audit_log"} <= tables:
                    raise ValueError("В копии нет таблиц приложения.")
            finally:
                check.close()
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(db_path + suffix):
                    os.remove(db_path + suffix)
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            shutil.copyfile(os.path.join(tmp, "kadry.db"), db_path)
        os.makedirs(upload_dir, exist_ok=True)
        for n in names:
            if n.startswith("uploads/"):
                with z.open(n) as src, open(os.path.join(upload_dir, os.path.basename(n)), "wb") as dst:
                    shutil.copyfileobj(src, dst)
    return len([n for n in names if n.startswith("uploads/")])
