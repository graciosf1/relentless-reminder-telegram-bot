import asyncio
import logging
import os
import random
import re
import uuid
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logging.basicConfig(level=logging.INFO)

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN", "8885604141:AAFjCmo3C20Ew_As1tjcRSjMeSZhKt8taHY")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler()

# Интервалы чекпоинтов в минутах (15 -> 10 -> 5 -> 2 -> 1...)
ESCALATION_MINUTES = [15, 10, 5, 2, 1]

# Награды (гифки) за выполнение задачи
REWARD_GIFS = [
    "https://media.giphy.com/media/111ebonMs90YLu/giphy.gif",
    "https://media.giphy.com/media/l0MYt5jPR6QX5pnqM/giphy.gif",
    "https://media.giphy.com/media/g9582DNuQppxC/giphy.gif",
    "https://media.giphy.com/media/3o7abKhOpu0NwenH3O/giphy.gif",
    "https://media.giphy.com/media/XreQmk7ETCak0/giphy.gif",
]

# Хранилище задач
tasks_db: dict[str, dict] = {}
completed_today: dict[int, list[dict]] = {}


# --- СОСТОЯНИЯ (FSM) ДЛЯ ПЕРЕНОСА ЗАДАЧИ ---
class RescheduleState(StatesGroup):
    waiting_for_reason = State()
    waiting_for_new_time = State()


# --- КЛАВИАТУРЫ ---
def get_checkpoint_kb(task_id: str, can_reschedule: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done:{task_id}")]
    ]
    if can_reschedule:
        buttons.append(
            [InlineKeyboardButton(text="⏳ Отложить (нужна причина)", callback_data=f"postpone:{task_id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- ПАРСЕР ВРЕМЕНИ ---
def parse_time_input(user_text: str, require_text: bool = True) -> tuple[datetime | None, str | None]:
    now = datetime.now()
    user_text = user_text.strip()
    tail_pattern = r"\s+(.+)$" if require_text else r"$"

    # 1. Относительное время: 10м, 2ч, 30с, 1д
    rel_match = re.match(rf"^(\d+)\s*([смчдsmhd]){tail_pattern}", user_text, re.IGNORECASE)
    if rel_match:
        amount = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        text = rel_match.group(3).strip() if require_text else ""

        if unit in ("с", "s"):
            run_date = now + timedelta(seconds=amount)
        elif unit in ("м", "m"):
            run_date = now + timedelta(minutes=amount)
        elif unit in ("ч", "h"):
            run_date = now + timedelta(hours=amount)
        elif unit in ("д", "d"):
            run_date = now + timedelta(days=amount)
        else:
            return None, None
        return run_date, text

    # 2. Точное время ЧЧ:ММ
    time_match = re.match(rf"^(\d{{1,2}}):(\d{{2}}){tail_pattern}", user_text)
    if time_match:
        hour, minute = int(time_match.group(1)), int(time_match.group(2))
        text = time_match.group(3).strip() if require_text else ""

        if 0 <= hour <= 23 and 0 <= minute <= 59:
            run_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if run_date <= now:
                run_date += timedelta(days=1)
            return run_date, text

    # 3. Полная дата ДД.ММ ЧЧ:ММ
    date_match = re.match(rf"^(\d{{1,2}})\.(\d{{1,2}})\s+(\d{{1,2}}):(\d{{2}}){tail_pattern}", user_text)
    if date_match:
        day, month = int(date_match.group(1)), int(date_match.group(2))
        hour, minute = int(date_match.group(3)), int(date_match.group(4))
        text = date_match.group(5).strip() if require_text else ""

        try:
            run_date = datetime(now.year, month, day, hour, minute, 0)
            if run_date <= now:
                run_date = datetime(now.year + 1, month, day, hour, minute, 0)
            return run_date, text
        except ValueError:
            return None, None

    return None, None


# --- ЧЕКПОИНТЫ И СВОДКА ---
async def trigger_checkpoint(task_id: str):
    task = tasks_db.get(task_id)
    if not task:
        return

    step = task["step"]
    can_reschedule = not task["rescheduled"]

    idx = min(step, len(ESCALATION_MINUTES) - 1)
    next_delay_min = ESCALATION_MINUTES[idx]

    if step == 0:
        header = "🔔 **НАПОМИНАНИЕ!**"
    else:
        header = f"🚨 **ЧЕКПОИНТ №{step}! Вы не отметили задачу!**"

    reason_note = f"\n_(Откладывалось ранее по причине: «{task['reason']}»)_" if task["rescheduled"] else ""
    warning = (
        f"\n\n⏱ Если не ответить, повторю через **{next_delay_min} мин.**"
        + ("\n⚠️ _Перенос больше недоступен! Только выполнение._" if not can_reschedule else "")
    )

    try:
        await bot.send_message(
            chat_id=task["chat_id"],
            text=f"{header}\n**Задача:** {task['text']}{reason_note}{warning}",
            reply_markup=get_checkpoint_kb(task_id, can_reschedule),
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.error(f"Ошибка при отправке чекпоинта: {e}")

    task["step"] += 1
    next_run = datetime.now() + timedelta(minutes=next_delay_min)

    scheduler.add_job(
        trigger_checkpoint,
        trigger=DateTrigger(run_date=next_run),
        id=task_id,
        replace_existing=True,
        kwargs={"task_id": task_id},
    )


async def send_daily_summary():
    """Сводка в 23:00"""
    if not completed_today:
        return

    for chat_id, done_list in list(completed_today.items()):
        if not done_list:
            continue

        lines = []
        for i, item in enumerate(done_list, 1):
            note = " _(с переносом)_" if item["rescheduled"] else " _(с первого раза! 🔥)_"
            lines.append(f"{i}. **{item['text']}** — в {item['time']}{note}")

        summary_text = (
            "🌙 **Итоги дня (23:00)!**\n\n"
            f"Сегодня закрыто задач: **{len(done_list)}**\n\n"
            + "\n".join(lines)
            + "\n\nОтличная работа! Время отдыхать 😴"
        )

        try:
            await bot.send_message(chat_id=chat_id, text=summary_text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Ошибка при отправке сводки: {e}")

    completed_today.clear()


# --- ОБРАБОТЧИКИ НАЖАТИЙ НА КНОПКИ ---
@router.callback_query(F.data.startswith("done:"))
async def cb_task_done(callback: CallbackQuery, state: FSMContext):
    task_id = callback.data.split(":")[1]
    task = tasks_db.pop(task_id, None)

    if scheduler.get_job(task_id):
        scheduler.remove_job(task_id)

    current_state = await state.get_state()
    if current_state:
        data = await state.get_data()
        if data.get("task_id") == task_id:
            await state.clear()

    if task:
        chat_id = task["chat_id"]
        completed_today.setdefault(chat_id, []).append({
            "text": task["text"],
            "time": datetime.now().strftime("%H:%M"),
            "rescheduled": task["rescheduled"],
        })

        await callback.message.edit_text(
            f"✅ **Задача выполнена!**\n~{task['text']}~", parse_mode="Markdown"
        )

        gif_url = random.choice(REWARD_GIFS)
        try:
            await callback.message.answer_animation(
                animation=gif_url,
                caption="🎉 **Красавчик! +1 выполненная задача в копилку дня!**",
                parse_mode="Markdown",
            )
        except Exception as e:
            logging.error(f"Не удалось отправить гифку: {e}")
    else:
        await callback.message.edit_text("✅ Эта задача уже закрыта.")

    await callback.answer("Отличная работа!")


@router.callback_query(F.data.startswith("postpone:"))
async def cb_task_postpone(callback: CallbackQuery, state: FSMContext):
    task_id = callback.data.split(":")[1]
    task = tasks_db.get(task_id)

    if not task:
        await callback.answer("Задача уже неактивна.", show_alert=True)
        return

    if task["rescheduled"]:
        await callback.answer("Эту задачу уже откладывали! Повторный перенос запрещен.", show_alert=True)
        return

    if scheduler.get_job(task_id):
        scheduler.remove_job(task_id)

    await state.set_state(RescheduleState.waiting_for_reason)
    await state.update_data(task_id=task_id)

    await callback.message.edit_text(
        f"⏸ **Перенос задачи:** «{task['text']}»\n\n"
        "✍️ **Напишите причину**, почему вы откладываете эту задачу:",
        parse_mode="Markdown",
    )
    await callback.answer()


# --- ОБРАБОТЧИКИ ПЕРЕНОСА (FSM) ---
@router.message(RescheduleState.waiting_for_reason, F.text)
async def process_postpone_reason(message: Message, state: FSMContext):
    reason = message.text.strip()
    if len(reason) < 3:
        await message.answer("Пожалуйста, напишите понятную причину (минимум 3 символа):")
        return

    await state.update_data(reason=reason)
    await state.set_state(RescheduleState.waiting_for_new_time)

    await message.answer(
        "📌 Причина зафиксирована.\n"
        "Теперь **укажите новое время или дату** (например: `30м`, `2ч`, `19:00`):",
        parse_mode="Markdown",
    )


@router.message(RescheduleState.waiting_for_new_time, F.text)
async def process_postpone_time(message: Message, state: FSMContext):
    run_date, _ = parse_time_input(message.text, require_text=False)

    if not run_date:
        await message.answer(
            "❌ Неверный формат времени. Напишите, например: `45м`, `3ч` или `18:30`.",
            parse_mode="Markdown",
        )
        return

    data = await state.get_data()
    task_id = data.get("task_id")
    reason = data.get("reason")
    task = tasks_db.get(task_id)

    if not task:
        await state.clear()
        await message.answer("Задача не найдена.")
        return

    task["rescheduled"] = True
    task["reason"] = reason
    task["step"] = 0

    scheduler.add_job(
        trigger_checkpoint,
        trigger=DateTrigger(run_date=run_date),
        id=task_id,
        replace_existing=True,
        kwargs={"task_id": task_id},
    )

    await state.clear()
    formatted_date = run_date.strftime("%d.%m.%Y в %H:%M:%S")
    await message.answer(
        f"🔄 **Задача перенесена (последний раз)!**\n"
        f"**Задача:** {task['text']}\n"
        f"**Причина:** {reason}\n"
        f"**Новое время:** {formatted_date}\n\n"
        "⚠️ _В следующий раз отложить ее будет нельзя!_",
        parse_mode="Markdown",
    )


# --- КОМАНДЫ И ОБЫЧНЫЕ СООБЩЕНИЯ ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я строгий бот-напоминалка с чекпоинтами и наградами.\n\n"
        "**Как создать напоминание:**\n"
        "🔹 `15м Выключить духовку`\n"
        "🔹 `18:30 Созвон по работе`\n\n"
        "• Не закрываешь дело — напоминаю каждые 15 -> 10 -> 5 -> 2 -> 1 мин.\n"
        "• Отложить можно только 1 раз с указанием причины.\n"
        "• За выполненное дело — победная GIF! 🎉\n"
        "• В 23:00 — вечерняя сводка (или проверь командой /summary).",
        parse_mode="Markdown",
    )


@router.message(Command("summary"))
async def cmd_summary_now(message: Message):
    done_list = completed_today.get(message.chat.id, [])
    if not done_list:
        await message.answer("За сегодня пока нет закрытых задач. Всё еще впереди! 💪")
        return

    lines = [
        f"{i}. **{item['text']}** — в {item['time']}"
        + (" _(с переносом)_" if item["rescheduled"] else " _(с первого раза! 🔥)_")
        for i, item in enumerate(done_list, 1)
    ]
    await message.answer(
        f"📊 **Выполнено за сегодня ({len(done_list)}):**\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


@router.message(StateFilter(None), F.text)
async def handle_new_reminder(message: Message):
    run_date, reminder_text = parse_time_input(message.text, require_text=True)

    if not run_date or not reminder_text:
        await message.answer(
            "❌ Не понял формат.\n"
            "Напишите время и текст через пробел, например:\n"
            "• `10м Проверить почту`\n"
            "• `19:00 Тренировка`",
            parse_mode="Markdown",
        )
        return

    task_id = uuid.uuid4().hex[:8]
    tasks_db[task_id] = {
        "chat_id": message.chat.id,
        "text": reminder_text,
        "step": 0,
        "rescheduled": False,
        "reason": "",
    }

    scheduler.add_job(
        trigger_checkpoint,
        trigger=DateTrigger(run_date=run_date),
        id=task_id,
        kwargs={"task_id": task_id},
    )

    formatted_date = run_date.strftime("%d.%m.%Y в %H:%M:%S")
    await message.answer(
        f"✅ **Запланировано!**\n"
        f"**Задача:** {reminder_text}\n"
        f"**Время:** {formatted_date}",
        parse_mode="Markdown",
    )


# --- ТОЧКА ВХОДА ---
async def handle_ping(request):
    """Отвечает серверу Render, что сервис жив"""
    return web.Response(text="Bot is running!")

async def start_dummy_server():
    """Фейковый веб-сервер для бесплатного тарифа Render"""
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    dp.include_router(router)

    scheduler.add_job(
        send_daily_summary,
        trigger=CronTrigger(hour=23, minute=0),
        id="daily_summary",
        replace_existing=True,
    )
    scheduler.start()

    # Запускаем фоновый веб-сервер для Render
    await start_dummy_server()

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")