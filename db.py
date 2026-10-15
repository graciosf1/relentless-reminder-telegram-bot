import aiosqlite
from datetime import datetime

DB_NAME = "bot.db"


async def init_db():
    """Создание таблиц при первом запуске"""
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблица активных задач
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                chat_id INTEGER,
                text TEXT,
                step INTEGER,
                rescheduled INTEGER,
                reason TEXT,
                run_date TEXT
            )
        """)
        # Таблица выполненных задач за день
        await db.execute("""
            CREATE TABLE IF NOT EXISTS completed_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                text TEXT,
                time TEXT,
                rescheduled INTEGER,
                date TEXT
            )
        """)
        await db.commit()


async def add_task(task_id: str, chat_id: int, text: str, run_date: datetime):
    """Добавление нового напоминания"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """INSERT INTO tasks (task_id, chat_id, text, step, rescheduled, reason, run_date)
               VALUES (?, ?, ?, 0, 0, '', ?)""",
            (task_id, chat_id, text, run_date.isoformat())
        )
        await db.commit()


async def get_task(task_id: str) -> dict | None:
    """Получение задачи по task_id"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None


async def update_task_step(task_id: str, step: int, next_run: datetime):
    """Обновление шага чекпоинта и следующего времени вызова"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE tasks SET step = ?, run_date = ? WHERE task_id = ?",
            (step, next_run.isoformat(), task_id)
        )
        await db.commit()


async def postpone_task(task_id: str, reason: str, run_date: datetime):
    """Перенос задачи на другое время с указанием причины"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE tasks SET rescheduled = 1, reason = ?, step = 0, run_date = ? WHERE task_id = ?",
            (reason, run_date.isoformat(), task_id)
        )
        await db.commit()


async def mark_task_done(task_id: str) -> dict | None:
    """Переносит задачу из активных в выполненные и возвращает её данные"""
    task = await get_task(task_id)
    if not task:
        return None

    now = datetime.now()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """INSERT INTO completed_tasks (chat_id, text, time, rescheduled, date)
               VALUES (?, ?, ?, ?, ?)""",
            (
                task["chat_id"],
                task["text"],
                now.strftime("%H:%M"),
                task["rescheduled"],
                now.strftime("%Y-%m-%d")
            )
        )
        await db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        await db.commit()

    return task


async def get_daily_completed(chat_id: int | None = None) -> list[dict]:
    """Возвращает закрытые задачи за сегодня (для одного пользователя или всех)"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        if chat_id:
            query = "SELECT * FROM completed_tasks WHERE chat_id = ? AND date = ?"
            params = (chat_id, today_str)
        else:
            query = "SELECT * FROM completed_tasks WHERE date = ?"
            params = (today_str,)

        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def clear_old_completed():
    """Очистка старых записей выполненных задач при вечерней сводке"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM completed_tasks WHERE date = ?", (today_str,))
        await db.commit()


async def get_all_active_tasks() -> list[dict]:
    """Для восстановления задач в планировщике после перезапуска бота"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
