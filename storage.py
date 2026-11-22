import os, uuid
from flask import current_app
from db import get_db

def save_file(file_storage, user_id=None):
    """Сохраняет файл и запись о нём. Сейчас локальная папка;
    для облака заменить тело на загрузку в S3-совместимое хранилище."""
    folder = current_app.config["UPLOAD_DIR"]
    os.makedirs(folder, exist_ok=True)
    stored = uuid.uuid4().hex
    path = os.path.join(folder, stored)
    file_storage.save(path)
    size = os.path.getsize(path)
    cur = get_db().execute(
        "INSERT INTO files (original_name, stored_name, size, uploaded_by) VALUES (?,?,?,?)",
        (os.path.basename(file_storage.filename or "file"), stored, size, user_id),
    )
    get_db().commit()
    return cur.lastrowid
