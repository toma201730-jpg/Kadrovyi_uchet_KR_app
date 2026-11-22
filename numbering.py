import datetime
from db import get_db

def next_number(doc_type, year=None, commit=True):
    """Сквозной номер документа в пределах типа и года (атомарно)."""
    year = year or datetime.date.today().year
    row = get_db().execute(
        "INSERT INTO doc_counters (doc_type, year, last_number) VALUES (?,?,1) "
        "ON CONFLICT(doc_type, year) DO UPDATE SET last_number = last_number + 1 "
        "RETURNING last_number",
        (doc_type, year),
    ).fetchone()
    if commit:
        get_db().commit()
    return row["last_number"]
