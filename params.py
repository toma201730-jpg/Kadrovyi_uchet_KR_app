from db import get_db

# ключ: (подпись, значение по умолчанию, минимум, максимум)
DEFS = {
    "annual_leave_days": ("Минимальная продолжительность ежегодного отпуска, календарных дней", 28, 1, 365),
    "probation_max_months": ("Максимальный испытательный срок, месяцев", 3, 0, 12),
    "resignation_notice_months": ("Срок предупреждения при увольнении по инициативе работника, месяцев", 1, 0, 12),
    "reminder_days": ("За сколько дней показывать и присылать напоминания о сроках", 30, 1, 90),
}
NORMATIVE = [k for k in DEFS if k != "reminder_days"]  # параметры ТК КР; reminder_days — настройка приложения

def get_int(key):
    """Нормативный параметр из настроек (в коде значений нет)."""
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return int(row["value"]) if row else DEFS[key][1]

def set_int(key, value):
    get_db().execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))
    get_db().commit()
