"""Telegram-бот записи на услуги (демо для портфолио).

Клиент выбирает услугу, дату и свободное время, оставляет имя и телефон.
Администратор получает уведомление и может выгрузить все записи в CSV.
"""
import asyncio
import csv
import io
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, KeyboardButton,
                           Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DB_PATH = os.environ.get("DB_PATH", "bookings.db")

SERVICES = {
    "cut": ("Стрижка", 60, 1200),
    "beard": ("Оформление бороды", 30, 700),
    "combo": ("Стрижка + борода", 90, 1700),
}
WORK_START, WORK_END = 10, 20  # часы работы
SLOT_MINUTES = 30
DAYS_AHEAD = 7

router = Router()


# --- база данных ---------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, service TEXT, start TEXT, minutes INTEGER,
            name TEXT, phone TEXT, created TEXT)"""
    )
    return conn


def busy_intervals(day: date) -> list[tuple[datetime, datetime]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT start, minutes FROM bookings WHERE start LIKE ?", (f"{day.isoformat()}%",)
        ).fetchall()
    return [(datetime.fromisoformat(s), datetime.fromisoformat(s) + timedelta(minutes=m)) for s, m in rows]


def free_slots(day: date, minutes: int) -> list[datetime]:
    busy = busy_intervals(day)
    slot = datetime.combine(day, datetime.min.time()).replace(hour=WORK_START)
    end_of_day = slot.replace(hour=WORK_END)
    now = datetime.now()
    result = []
    while slot + timedelta(minutes=minutes) <= end_of_day:
        slot_end = slot + timedelta(minutes=minutes)
        if slot > now and all(slot_end <= b0 or slot >= b1 for b0, b1 in busy):
            result.append(slot)
        slot += timedelta(minutes=SLOT_MINUTES)
    return result


# --- сценарий записи -----------------------------------------------------

class Booking(StatesGroup):
    service = State()
    day = State()
    time = State()
    name = State()
    phone = State()


WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    for key, (title, minutes, price) in SERVICES.items():
        kb.button(text=f"{title} · {minutes} мин · {price} ₽", callback_data=f"svc:{key}")
    kb.adjust(1)
    await message.answer(
        "Здравствуйте! Я помогу записаться в барбершоп.\nВыберите услугу:",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(Booking.service)


@router.callback_query(Booking.service, F.data.startswith("svc:"))
async def pick_service(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[1]
    await state.update_data(service=key)
    kb = InlineKeyboardBuilder()
    today = date.today()
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        if free_slots(d, SERVICES[key][1]):
            kb.button(text=f"{WEEKDAYS[d.weekday()]} {d:%d.%m}", callback_data=f"day:{d.isoformat()}")
    kb.adjust(4)
    await call.message.edit_text(f"Услуга: {SERVICES[key][0]}\nВыберите день:", reply_markup=kb.as_markup())
    await state.set_state(Booking.day)


@router.callback_query(Booking.day, F.data.startswith("day:"))
async def pick_day(call: CallbackQuery, state: FSMContext):
    day = date.fromisoformat(call.data.split(":", 1)[1])
    data = await state.get_data()
    slots = free_slots(day, SERVICES[data["service"]][1])
    if not slots:
        await call.answer("На этот день мест уже нет", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for s in slots:
        kb.button(text=f"{s:%H:%M}", callback_data=f"time:{s.isoformat()}")
    kb.adjust(4)
    await call.message.edit_text(f"{day:%d.%m} — выберите время:", reply_markup=kb.as_markup())
    await state.set_state(Booking.time)


@router.callback_query(Booking.time, F.data.startswith("time:"))
async def pick_time(call: CallbackQuery, state: FSMContext):
    await state.update_data(start=call.data.split(":", 1)[1])
    await call.message.edit_text("Как вас зовут?")
    await state.set_state(Booking.name)


@router.message(Booking.name, F.text)
async def get_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:60])
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await message.answer("Оставьте номер телефона — кнопкой ниже или текстом:", reply_markup=kb)
    await state.set_state(Booking.phone)


@router.message(Booking.phone)
async def get_phone(message: Message, state: FSMContext, bot: Bot):
    phone = message.contact.phone_number if message.contact else (message.text or "")
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10:
        await message.answer("Похоже, номер неполный. Попробуйте ещё раз.")
        return
    data = await state.get_data()
    title, minutes, price = SERVICES[data["service"]]
    start = datetime.fromisoformat(data["start"])

    # Слот могли занять, пока клиент вводил данные
    if start not in free_slots(start.date(), minutes):
        await message.answer("Увы, это время только что заняли. Начните заново: /start",
                             reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    with db() as conn:
        conn.execute(
            "INSERT INTO bookings (user_id, service, start, minutes, name, phone, created) VALUES (?,?,?,?,?,?,?)",
            (message.from_user.id, title, start.isoformat(), minutes, data["name"], phone,
             datetime.now().isoformat(timespec="seconds")),
        )
    await state.clear()
    await message.answer(
        f"✅ Готово! Вы записаны:\n{title}\n{start:%d.%m в %H:%M}\nСтоимость: {price} ₽\n\n"
        "Мои записи: /my",
        reply_markup=ReplyKeyboardRemove(),
    )
    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID, f"🆕 Новая запись\n{title} — {start:%d.%m %H:%M}\n{data['name']}, {phone}"
        )


@router.message(Command("my"))
async def my_bookings(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT service, start FROM bookings WHERE user_id=? AND start>=? ORDER BY start",
            (message.from_user.id, datetime.now().isoformat()),
        ).fetchall()
    if not rows:
        await message.answer("У вас нет предстоящих записей. Записаться: /start")
        return
    lines = [f"• {s} — {datetime.fromisoformat(t):%d.%m %H:%M}" for s, t in rows]
    await message.answer("Ваши записи:\n" + "\n".join(lines))


# --- администратор -------------------------------------------------------

@router.message(Command("export"), F.from_user.id == ADMIN_ID)
async def export_csv(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, service, start, name, phone, created FROM bookings ORDER BY start"
        ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["ID", "Услуга", "Время", "Имя", "Телефон", "Создано"])
    writer.writerows(rows)
    file = BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename="bookings.csv")
    await message.answer_document(file, caption=f"Всего записей: {len(rows)}")


@router.message(Command("today"), F.from_user.id == ADMIN_ID)
async def today(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT service, start, name, phone FROM bookings WHERE start LIKE ? ORDER BY start",
            (f"{date.today().isoformat()}%",),
        ).fetchall()
    text = "\n".join(f"{datetime.fromisoformat(t):%H:%M} — {s}, {n}, {p}" for s, t, n, p in rows)
    await message.answer(text or "Сегодня записей нет")


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
