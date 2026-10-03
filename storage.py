"""
Хранилище на SQLite — переживает перезапуск бота (в отличие от старого варианта
в памяти), и по нему же строится статистика и просмотр переписок для владельца.
"""

import os
import sqlite3

# На хостинге (Railway) задаём DB_PATH=/data/bot.db, чтобы база жила на диске и не терялась при перезапуске
DB_PATH = os.getenv("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")

_conn.executescript("""
CREATE TABLE IF NOT EXISTS modes (
    user_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'ai'
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);

CREATE TABLE IF NOT EXISTS payments (
    user_id INTEGER PRIMARY KEY,
    pending INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS profiles (
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    phone TEXT,
    interest TEXT,
    confirm_asked INTEGER NOT NULL DEFAULT 0,
    confirmed INTEGER NOT NULL DEFAULT 0,
    declined INTEGER NOT NULL DEFAULT 0
);

-- Временная пауза бота, когда владелец сам пишет клиенту: до paused_until (UTC) бот молчит
CREATE TABLE IF NOT EXISTS pauses (
    user_id INTEGER PRIMARY KEY,
    paused_until TEXT NOT NULL
);
""")
_conn.commit()

# Добавлено позже — на уже существующей базе ALTER TABLE может упасть, если колонка
# уже есть, это ожидаемо и безопасно игнорировать
try:
    _conn.execute("ALTER TABLE profiles ADD COLUMN confirmed_at TEXT")
    _conn.commit()
except sqlite3.OperationalError:
    pass


def _empty_profile() -> dict:
    return {
        "name": None,
        "phone": None,
        "interest": None,
        "confirm_asked": False,  # бот уже переспросил "всё верно?"
        "confirmed": False,      # клиент подтвердил свои данные
        "declined": False,       # клиент явно отказался
    }


def get_profile(user_id: int) -> dict:
    row = _conn.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return _empty_profile()
    return {
        "name": row["name"],
        "phone": row["phone"],
        "interest": row["interest"],
        "confirm_asked": bool(row["confirm_asked"]),
        "confirmed": bool(row["confirmed"]),
        "declined": bool(row["declined"]),
    }


def _ensure_profile_row(user_id: int) -> None:
    _conn.execute("INSERT OR IGNORE INTO profiles (user_id) VALUES (?)", (user_id,))


def update_profile(user_id: int, new_data: dict) -> dict | None:
    """Дополняет профиль только пустыми полями — не затирает то, что уже узнали."""
    profile = get_profile(user_id)
    _ensure_profile_row(user_id)
    changed = False
    for field in ("name", "phone", "interest"):
        value = new_data.get(field)
        if value and not profile.get(field):
            _conn.execute(f"UPDATE profiles SET {field} = ? WHERE user_id = ?", (value, user_id))
            changed = True
    if changed:
        _conn.commit()
    return get_profile(user_id) if changed else None


def set_profile_fields(user_id: int, new_data: dict) -> None:
    """Клиент исправил данные в карточке — перезаписываем только названные поля, остальное не трогаем."""
    _ensure_profile_row(user_id)
    for field in ("name", "phone", "interest"):
        value = new_data.get(field)
        if value:
            _conn.execute(f"UPDATE profiles SET {field} = ? WHERE user_id = ?", (value, user_id))
    _conn.commit()


def is_profile_complete(profile: dict) -> bool:
    return bool(profile.get("name") and profile.get("phone") and profile.get("interest"))


def mark_confirm_asked(user_id: int) -> None:
    _ensure_profile_row(user_id)
    _conn.execute("UPDATE profiles SET confirm_asked = 1 WHERE user_id = ?", (user_id,))
    _conn.commit()


def mark_confirmed(user_id: int) -> None:
    _ensure_profile_row(user_id)
    _conn.execute(
        "UPDATE profiles SET confirmed = 1, declined = 0, confirmed_at = datetime('now') WHERE user_id = ?",
        (user_id,),
    )
    _conn.commit()


def mark_declined(user_id: int) -> None:
    _ensure_profile_row(user_id)
    _conn.execute("UPDATE profiles SET declined = 1 WHERE user_id = ? AND confirmed = 0", (user_id,))
    _conn.commit()


def reset_profile_fields(user_id: int) -> None:
    """Клиент сказал, что данные неверны — начинаем сбор заново."""
    _conn.execute(
        "INSERT INTO profiles (user_id, name, phone, interest, confirm_asked, confirmed, declined) "
        "VALUES (?, NULL, NULL, NULL, 0, 0, 0) "
        "ON CONFLICT(user_id) DO UPDATE SET name=NULL, phone=NULL, interest=NULL, "
        "confirm_asked=0, confirmed=0, declined=0",
        (user_id,),
    )
    _conn.commit()


def get_mode(user_id: int) -> str:
    row = _conn.execute("SELECT mode FROM modes WHERE user_id = ?", (user_id,)).fetchone()
    mode = row["mode"] if row else "ai"
    if mode == "manager":
        # Если это была временная пауза и её срок вышел — бот снова отвечает сам
        expired = _conn.execute(
            "SELECT 1 FROM pauses WHERE user_id = ? AND paused_until <= datetime('now')", (user_id,)
        ).fetchone()
        if expired:
            set_mode(user_id, "ai")
            return "ai"
    return mode


def set_mode(user_id: int, mode: str) -> None:
    """Постоянная смена режима (менеджер взял чат / вернул ИИ) — временная пауза при этом снимается."""
    _conn.execute(
        "INSERT INTO modes (user_id, mode) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET mode = ?",
        (user_id, mode, mode),
    )
    _conn.execute("DELETE FROM pauses WHERE user_id = ?", (user_id,))
    _conn.commit()


def pause_ai(user_id: int, hours: float) -> None:
    """Владелец сам написал клиенту — бот молчит указанное число часов (каждое новое сообщение продлевает)."""
    set_mode(user_id, "manager")
    _conn.execute(
        "INSERT INTO pauses (user_id, paused_until) VALUES (?, datetime('now', ?)) "
        "ON CONFLICT(user_id) DO UPDATE SET paused_until = excluded.paused_until",
        (user_id, f"+{int(hours * 3600)} seconds"),
    )
    _conn.commit()


def add_message(user_id: int, role: str, content: str) -> None:
    _conn.execute(
        "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content),
    )
    _conn.commit()


def get_history(user_id: int) -> list[dict]:
    """Последние 20 сообщений в хронологическом порядке — контекст для ИИ."""
    rows = _conn.execute(
        "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT 20",
        (user_id,),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def get_full_history(user_id: int) -> list[dict]:
    """Вся переписка с клиентом целиком — для владельца, не для контекста ИИ."""
    rows = _conn.execute(
        "SELECT role, content, ts FROM messages WHERE user_id = ? ORDER BY id ASC",
        (user_id,),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"], "ts": r["ts"]} for r in rows]


def set_payment_pending(user_id: int) -> None:
    _conn.execute(
        "INSERT INTO payments (user_id, pending) VALUES (?, 1) ON CONFLICT(user_id) DO UPDATE SET pending = 1",
        (user_id,),
    )
    _conn.commit()


def is_payment_pending(user_id: int) -> bool:
    row = _conn.execute("SELECT pending FROM payments WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row["pending"])


def confirm_payment(user_id: int) -> None:
    _conn.execute("UPDATE payments SET pending = 0 WHERE user_id = ?", (user_id,))
    _conn.commit()


def get_confirmed_leads() -> list[dict]:
    """Список подтверждённых заявок, свежие сверху — для владельца, чтобы обзвонить клиентов."""
    rows = _conn.execute(
        "SELECT user_id, name, phone, interest, confirmed_at FROM profiles "
        "WHERE confirmed = 1 ORDER BY confirmed_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Сводка по всем клиентам, которые хоть раз дошли до профиля."""
    total = _conn.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()["c"]
    confirmed = _conn.execute("SELECT COUNT(*) AS c FROM profiles WHERE confirmed = 1").fetchone()["c"]
    declined = _conn.execute("SELECT COUNT(*) AS c FROM profiles WHERE declined = 1 AND confirmed = 0").fetchone()["c"]
    thinking = total - confirmed - declined
    return {"total": total, "confirmed": confirmed, "declined": declined, "thinking": thinking}
