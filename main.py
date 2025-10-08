import asyncio
import os
import csv
from pathlib import Path
from datetime import datetime, date
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command

# ================== КОНФИГ =====================
API_TOKEN = os.getenv("TGTOKEN")  # токен бота

# Группа, где менеджер(ы) кидают суммы (+/-)
MANAGER_CHAT_ID = -1002759641457

# Кто может вызывать отчёт/удаление (твоя учётка)
ADMIN_CHAT_ID = [5682655968, 7400953103]  # список

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_CHAT_ID

# Часовой пояс для "сегодня"
REPORT_TZ = os.getenv("REPORT_TZ", "Europe/Moscow")

# Ставки выплат партнёру (в руб./за 1 юань)
PAY_NO_DISCOUNT_RUB_PER_CNY = 0.15   # без скидки
PAY_DISCOUNT_RUB_PER_CNY   = 0.10    # со скидкой

# Надбавка к курсу в проверочных формулах (в руб./за 1 юань)
CHECK_ADD_NO_DISCOUNT = 0.10
CHECK_ADD_DISCOUNT    = 0.05

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "report.db"
LOG_CSV = DATA_DIR / "log.csv"  # опционально (необязательный csv-лог)

# ================== БОТ ==================
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ================== КЛАВИАТУРЫ ==================
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Расчёт прибыли")],
    ],
    resize_keyboard=True
)
cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🚫 Отмена")]],
    resize_keyboard=True,
    one_time_keyboard=True
)

# ================== FSM ДЛЯ РАСЧЁТА ==================
class ProfitStates(StatesGroup):
    rate_ge_30000 = State()
    rate_10000_30000 = State()
    rate_3000_10000 = State()
    rate_1000_3000 = State()
    rate_lt_1000 = State()
    amounts_mixed = State()   # единый столбик сумм

# ================== УТИЛИТЫ ==================
def _tznow() -> datetime:
    return datetime.now(ZoneInfo(REPORT_TZ))

def _today() -> date:
    return _tznow().date()

def _parse_float(text: str):
    t = text.strip().replace(",", ".")
    try:
        v = float(t)
        return v if v > 0 else None
    except ValueError:
        return None

def _format_rub(x: float) -> str:
    return f"{x:,.2f} ₽".replace(",", " ")

def _format_cny(x: float) -> str:
    return f"{x:,.2f} ¥".replace(",", " ")

def _pick_rate_for_amount(cny_amount: float, rates: dict) -> float:
    if cny_amount >= 30000:
        return rates["ge_30000"]
    if 10000 <= cny_amount < 30000:
        return rates["r_10000_30000"]
    if 3000 <= cny_amount < 10000:
        return rates["r_3000_10000"]
    if 1000 <= cny_amount < 3000:
        return rates["r_1000_3000"]
    return rates["lt_1000"]

def _parse_mixed_lines(text: str) -> tuple[list[float], list[float]]:
    """
    Возвращает (без_скидки, со_скидкой) из форматов:
      bs1000  | bs 1000  -> без скидки
      s1000   | s 1000   -> со скидкой
    Регистр не важен. Остальные строки игнорируются.
    """
    no_disc, disc = [], []
    for raw in text.replace("\t", "\n").splitlines():
        line = raw.strip().lower().replace(",", ".")
        if not line:
            continue

        # допускаем разделение пробелом: 'bs 1000' / 's 1000'
        parts = line.split()
        token = "".join(parts)  # склеиваем, чтобы 'bs 1000' -> 'bs1000'

        def parse_amount(s: str) -> float | None:
            # вытащим число из хвоста токена
            num = "".join(ch for ch in s if (ch.isdigit() or ch in "."))
            if not num:
                return None
            try:
                v = float(num)
                return v if v > 0 else None
            except ValueError:
                return None

        if token.startswith("bs"):
            val = parse_amount(token[2:])
            if val is not None:
                no_disc.append(val)
            continue

        if token.startswith("s"):
            val = parse_amount(token[1:])
            if val is not None:
                disc.append(val)
            continue

        # всё остальное теперь игнорируем
    return no_disc, disc


# ================== БД ==================
async def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS entries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            is_discount INTEGER NOT NULL, -- 0 без скидки, 1 со скидкой
            chat_id INTEGER NOT NULL,
            msg_id INTEGER NOT NULL,
            sender_id INTEGER
        )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_entries_msg ON entries(chat_id, msg_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_entries_sender_date ON entries(sender_id, date)")
        await db.commit()

async def replace_message_entries(chat_id: int, msg_id: int, sender_id: int | None,
                                  no_list: list[float], disc_list: list[float]):
    """Полностью заменяет записи, привязанные к (chat_id,msg_id) на новые значения."""
    now = _tznow()
    d = _today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM entries WHERE chat_id=? AND msg_id=?", (chat_id, msg_id))
        for a in no_list:
            await db.execute(
                "INSERT INTO entries(ts,date,amount,is_discount,chat_id,msg_id,sender_id) VALUES(?,?,?,?,?,?,?)",
                (now.isoformat(), d, float(a), 0, chat_id, msg_id, sender_id)
            )
        for a in disc_list:
            await db.execute(
                "INSERT INTO entries(ts,date,amount,is_discount,chat_id,msg_id,sender_id) VALUES(?,?,?,?,?,?,?)",
                (now.isoformat(), d, float(a), 1, chat_id, msg_id, sender_id)
            )
        await db.commit()

async def clear_today() -> tuple[int, float, float]:
    """Удаляет все записи за текущие сутки. Возвращает (count, sum_no, sum_disc)."""
    d = _today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT 
              COUNT(*),
              COALESCE(SUM(CASE WHEN is_discount=0 THEN amount END),0),
              COALESCE(SUM(CASE WHEN is_discount=1 THEN amount END),0)
            FROM entries WHERE date=?
        """, (d,)) as cur:
            row = await cur.fetchone()
        cnt, sum_no, sum_disc = row or (0, 0.0, 0.0)
        await db.execute("DELETE FROM entries WHERE date=?", (d,))
        await db.commit()
    return int(cnt), float(sum_no), float(sum_disc)

# --- вместо delete_by_msg_id ---
async def delete_by_msg_id(chat_id: int, msg_id: int) -> tuple[int, float, float]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT 
              COUNT(*),
              COALESCE(SUM(CASE WHEN is_discount=0 THEN amount END),0),
              COALESCE(SUM(CASE WHEN is_discount=1 THEN amount END),0)
            FROM entries WHERE chat_id=? AND msg_id=?
        """, (chat_id, msg_id)) as cur:
            row = await cur.fetchone()
        cnt, sum_no, sum_disc = (row or (0, 0.0, 0.0))
        await db.execute("DELETE FROM entries WHERE chat_id=? AND msg_id=?", (chat_id, msg_id))
        await db.commit()
    return int(cnt), float(sum_no), float(sum_disc)


# --- вместо undo_last_for_sender ---
async def undo_last_for_sender(sender_id: int) -> tuple[bool, int, float, float, int]:
    d = _today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT msg_id
            FROM entries
            WHERE sender_id=? AND date=?
            ORDER BY ts DESC
            LIMIT 1
        """, (sender_id, d)) as cur:
            row = await cur.fetchone()
        if not row:
            return False, 0, 0.0, 0.0, 0
        msg_id = int(row[0])

        async with db.execute("""
            SELECT 
              COUNT(*),
              COALESCE(SUM(CASE WHEN is_discount=0 THEN amount END),0),
              COALESCE(SUM(CASE WHEN is_discount=1 THEN amount END),0)
            FROM entries WHERE msg_id=? AND date=? AND sender_id=?
        """, (msg_id, d, sender_id)) as cur2:
            row2 = await cur2.fetchone()
        cnt, sum_no, sum_disc = (row2 or (0, 0.0, 0.0))

        await db.execute("DELETE FROM entries WHERE msg_id=? AND date=? AND sender_id=?", (msg_id, d, sender_id))
        await db.commit()
    return True, int(cnt), float(sum_no), float(sum_disc), msg_id


# --- вместо aggregate_for_day ---
async def aggregate_for_day(day: date) -> dict:
    totals = {
        "sum_no": 0.0, "sum_disc": 0.0, "total_cny": 0.0,
        "payout_no": 0.0, "payout_disc": 0.0, "payout_total": 0.0
    }
    d = day.isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT
              COALESCE(SUM(CASE WHEN is_discount=0 THEN amount END),0),
              COALESCE(SUM(CASE WHEN is_discount=1 THEN amount END),0)
            FROM entries WHERE date=?
        """, (d,)) as cur:
            row = await cur.fetchone()
        sum_no, sum_disc = row or (0.0, 0.0)

    totals["sum_no"] = float(sum_no)
    totals["sum_disc"] = float(sum_disc)
    totals["total_cny"] = totals["sum_no"] + totals["sum_disc"]
    totals["payout_no"] = totals["sum_no"] * PAY_NO_DISCOUNT_RUB_PER_CNY
    totals["payout_disc"] = totals["sum_disc"] * PAY_DISCOUNT_RUB_PER_CNY
    totals["payout_total"] = totals["payout_no"] + totals["payout_disc"]
    return totals


def format_daily_report(day: date, totals: dict) -> str:
    return (
        f"<b>Ежедневный отчёт за {day.strftime('%d.%m.%Y')}</b>\n\n"
        f"<b>Суммы в юанях:</b>\n"
        f"Без скидки: {_format_cny(totals['sum_no'])}\n"
        f"Со скидкой: {_format_cny(totals['sum_disc'])}\n"
        f"Всего: <b>{_format_cny(totals['total_cny'])}</b>\n\n"
        f"<b>Выплаты партнёру:</b>\n"
        f"Без скидки: {_format_rub(totals['payout_no'])}\n"
        f"Со скидкой: {_format_rub(totals['payout_disc'])}\n"
        f"Итого к выплате: <b>{_format_rub(totals['payout_total'])}</b>\n"
    )
# === Команды в ЛС и в группе ===
@dp.message(Command("myid"))
async def myid(message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(f"user_id: <code>{uid}</code>\nchat_id: <code>{message.chat.id}</code>")

@dp.message(Command("clear_today"))
async def clear_today_cmd(message: Message):
    uid = message.from_user.id if message.from_user else 0
    if uid not in ADMIN_CHAT_ID and message.chat.id not in ADMIN_CHAT_ID:
        return

    cnt, sum_no, sum_disc = await clear_today()
    await message.answer(
        f"Очищено за сегодня: {cnt} записей.\n"
        f"Было: без скидки {_format_cny(sum_no)}, со скидкой {_format_cny(sum_disc)}."
    )

@dp.message(Command("report_today"))
async def report_today(message: Message):
    # разрешаем админу из любого чата
    uid = message.from_user.id if message.from_user else 0
    if uid not in ADMIN_CHAT_ID and message.chat.id not in ADMIN_CHAT_ID:
        return
    totals = await aggregate_for_day(_today())
    await message.answer(format_daily_report(_today(), totals))

# === Команды именно в группе менеджера ===
@dp.message(F.chat.id == MANAGER_CHAT_ID, Command("undo"))
async def undo_cmd(message: Message):
    if not message.from_user:
        return
    ok, cnt, sum_no, sum_disc, msg_id = await undo_last_for_sender(message.from_user.id)
    if not ok:
        await message.reply("Нечего отменять за сегодня.")
        return
    await message.reply(
        f"Отменено (msg_id={msg_id}): {cnt} строк. "
        f"Минус: без скидки {_format_cny(sum_no)}, со скидкой {_format_cny(sum_disc)}."
    )

# ================== ХЭНДЛЕРЫ: ОБЩЕЕ МЕНЮ ==================
@dp.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Этот бот считает выплаты партнёру и собирает обмены из группы менеджера.\n"
        "Меню — ниже.",
        reply_markup=main_kb
    )

@dp.message(F.text == "🚫 Отмена")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. Возвращаю в главное меню.", reply_markup=main_kb)

# ================== ХЭНДЛЕРЫ: РАСЧЁТ ПО КУРСАМ ==================
@dp.message(F.text == "📊 Расчёт прибыли")
async def start_profit_calc(message: Message, state: FSMContext):
    await state.clear()
    await state.update_data(rates={})
    await message.answer("Какой был курс сегодня <b>от 30000 юаней</b>? (в ₽ за 1 ¥)", reply_markup=cancel_kb)
    await state.set_state(ProfitStates.rate_ge_30000)

@dp.message(ProfitStates.rate_ge_30000)
async def rate_ge_30000(message: Message, state: FSMContext):
    rate = _parse_float(message.text)
    if rate is None:
        await message.answer("Введите положительное число, например: 11.75")
        return
    data = await state.get_data()
    rates = data.get("rates", {})
    rates["ge_30000"] = rate
    await state.update_data(rates=rates)
    await message.answer("Какой курс был <b>от 10000 до 30000 юаней</b>? (₽/¥)")
    await state.set_state(ProfitStates.rate_10000_30000)

@dp.message(ProfitStates.rate_10000_30000)
async def rate_10000_30000(message: Message, state: FSMContext):
    rate = _parse_float(message.text)
    if rate is None:
        await message.answer("Введите положительное число, например: 11.80")
        return
    data = await state.get_data()
    rates = data.get("rates", {})
    rates["r_10000_30000"] = rate
    await state.update_data(rates=rates)
    await message.answer("Какой был курс <b>от 3000 до 10000 юаней</b>? (₽/¥)")
    await state.set_state(ProfitStates.rate_3000_10000)

@dp.message(ProfitStates.rate_3000_10000)
async def rate_3000_10000(message: Message, state: FSMContext):
    rate = _parse_float(message.text)
    if rate is None:
        await message.answer("Введите положительное число, например: 11.85")
        return
    data = await state.get_data()
    rates = data.get("rates", {})
    rates["r_3000_10000"] = rate
    await state.update_data(rates=rates)
    await message.answer("Какой был курс <b>от 1000 до 3000 юаней</b>? (₽/¥)")
    await state.set_state(ProfitStates.rate_1000_3000)

@dp.message(ProfitStates.rate_1000_3000)
async def rate_1000_3000(message: Message, state: FSMContext):
    rate = _parse_float(message.text)
    if rate is None:
        await message.answer("Введите положительное число, например: 11.90")
        return
    data = await state.get_data()
    rates = data.get("rates", {})
    rates["r_1000_3000"] = rate
    await state.update_data(rates=rates)
    await message.answer("Какой был курс <b>до 1000 юаней</b>? (₽/¥)")
    await state.set_state(ProfitStates.rate_lt_1000)

@dp.message(ProfitStates.rate_lt_1000)
async def rate_lt_1000(message: Message, state: FSMContext):
    rate = _parse_float(message.text)
    if rate is None:
        await message.answer("Введите положительное число, например: 12.00")
        return
    data = await state.get_data()
    rates = data.get("rates", {})
    rates["lt_1000"] = rate
    await state.update_data(rates=rates)
    await message.answer(
        "Введите <b>в ОДИН столбик</b> суммы юаней.\n"
        "Отмечайте тип заявки:\n"
        "• <b>bs1500</b> — без скидки\n"
        "• <b>s2600</b> — со скидкой\n"
        "Пример:\n<code>\nbs1500\ns900\nbs12000\ns2600\n</code>",
        reply_markup=cancel_kb
    )
    await state.set_state(ProfitStates.amounts_mixed)

@dp.message(F.text == "/myid")
async def myid(message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(f"user_id: <code>{uid}</code>\nchat_id: <code>{message.chat.id}</code>")

@dp.message(ProfitStates.amounts_mixed)
async def amounts_mixed(message: Message, state: FSMContext):
    no_list, disc_list = _parse_mixed_lines(message.text)
    if not no_list and not disc_list:
        await message.answer("Не нашёл чисел. Пример:\n<code>\n+1500\n-900\n+12000\n-2600\n</code>")
        return

    data = await state.get_data()
    rates: dict = data["rates"]

    sum_no = sum(no_list)
    sum_disc = sum(disc_list)
    total_cny = sum_no + sum_disc

    payout_no = sum_no * PAY_NO_DISCOUNT_RUB_PER_CNY
    payout_disc = sum_disc * PAY_DISCOUNT_RUB_PER_CNY
    payout_total = payout_no + payout_disc

    # (опционально) CSV-лог одного расчёта:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_CSV.exists():
        with LOG_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["ts_iso","date","sum_no","sum_disc","total_cny","payout_no","payout_disc","payout_total"])
    with LOG_CSV.open("a", newline="", encoding="utf-8") as f:
        now = _tznow()
        w = csv.writer(f, delimiter=";")
        w.writerow([now.isoformat(), _today().isoformat(), f"{sum_no:.6f}", f"{sum_disc:.6f}",
                    f"{total_cny:.6f}", f"{payout_no:.6f}", f"{payout_disc:.6f}", f"{payout_total:.6f}"])

    # Проверочные строки
    lines_check_no = []
    for a in no_list:
        r = _pick_rate_for_amount(a, rates)
        rub = a * (r + CHECK_ADD_NO_DISCOUNT)
        lines_check_no.append(f"{a:,.2f} ¥ × ({r:.4f} + {CHECK_ADD_NO_DISCOUNT:.2f}) = {_format_rub(rub)}".replace(",", " "))

    lines_check_disc = []
    for a in disc_list:
        r = _pick_rate_for_amount(a, rates)
        rub = a * (r + CHECK_ADD_DISCOUNT)
        lines_check_disc.append(f"{a:,.2f} ¥ × ({r:.4f} + {CHECK_ADD_DISCOUNT:.2f}) = {_format_rub(rub)}".replace(",", " "))

    msg = []
    msg.append("<b>Итоги за день (ввод из диалога)</b>\n")
    msg.append("<b>Курсы (₽/¥):</b>")
    msg.append(f"≥ 30000 ¥: <b>{rates['ge_30000']}</b>")
    msg.append(f"10000–30000 ¥: <b>{rates['r_10000_30000']}</b>")
    msg.append(f"3000–10000 ¥: <b>{rates['r_3000_10000']}</b>")
    msg.append(f"1000–3000 ¥: <b>{rates['r_1000_3000']}</b>")
    msg.append(f"&lt; 1000 ¥: <b>{rates['lt_1000']}</b>\n")  # экранируем <

    msg.append("<b>Суммы в юанях:</b>")
    msg.append(f"Без скидки: {_format_cny(sum_no)}")
    msg.append(f"Со скидкой: {_format_cny(sum_disc)}")
    msg.append(f"Всего: <b>{_format_cny(total_cny)}</b>\n")

    msg.append("<b>Выплаты партнёру:</b>")
    msg.append(f"Без скидки: {_format_cny(sum_no)} × {PAY_NO_DISCOUNT_RUB_PER_CNY:.2f} ₽/¥ = <b>{_format_rub(payout_no)}</b>")
    msg.append(f"Со скидкой: {_format_cny(sum_disc)} × {PAY_DISCOUNT_RUB_PER_CNY:.2f} ₽/¥ = <b>{_format_rub(payout_disc)}</b>")
    msg.append(f"Итого к выплате: <b>{_format_rub(payout_total)}</b>\n")

    msg.append("<b>Проверьте суммы в рублях (без скидки):</b>")
    msg.extend("• " + s for s in lines_check_no) if lines_check_no else msg.append("—")
    msg.append("\n<b>Проверьте суммы в рублях (со скидкой):</b>")
    msg.extend("• " + s for s in lines_check_disc) if lines_check_disc else msg.append("—")

    text = "\n".join(msg)
    if len(text) > 3900:
        cut_label = "<b>Проверьте суммы в рублях (без скидки):</b>"
        cut_idx = msg.index(cut_label) if cut_label in msg else len(msg)
        text = "\n".join(msg[:cut_idx]) + "\n\n<i>Список проверочных расчётов слишком длинный. Уменьшите количество строк.</i>"

    await message.answer(text, reply_markup=main_kb)
    await state.clear()

# ================== ГРУППА МЕНЕДЖЕРА: НОВЫЕ СООБЩЕНИЯ ==================
@dp.message(F.chat.id == MANAGER_CHAT_ID, F.text)
async def manager_group_listener(message: Message):
    no_list, disc_list = _parse_mixed_lines(message.text)
    if not no_list and not disc_list:
        return  # игнорим нерелевантные сообщения

    await replace_message_entries(
        chat_id=message.chat.id,
        msg_id=message.message_id,
        sender_id=message.from_user.id if message.from_user else None,
        no_list=no_list,
        disc_list=disc_list
    )

    await message.reply(
        f"Принято: +{len(no_list)} без скидки, -{len(disc_list)} со скидкой. "
        f"Сумма: {_format_cny(sum(no_list) + sum(disc_list))}"
    )

# ================== ГРУППА МЕНЕДЖЕРА: РЕДАКТИРОВАННЫЕ СООБЩЕНИЯ ==================
@dp.edited_message(F.chat.id == MANAGER_CHAT_ID, F.text)
async def manager_group_edited(message: Message):
    no_list, disc_list = _parse_mixed_lines(message.text)
    # Если отредактировали в нерелевантное — просто удалим прежние записи по msg_id
    await replace_message_entries(
        chat_id=message.chat.id,
        msg_id=message.message_id,
        sender_id=message.from_user.id if message.from_user else None,
        no_list=no_list,
        disc_list=disc_list
    )
    await bot.send_message(
        chat_id=message.chat.id,
        text=f"Обновлено для msg_id={message.message_id}: +{len(no_list)}, -{len(disc_list)}."
    )

# ================== КОМАНДЫ: ОТЧЁТ/УДАЛЕНИЕ/ОТМЕНА ==================
def _is_admin_context(message: Message) -> bool:
    uid = message.from_user.id if message.from_user else 0
    return uid in ADMIN_CHAT_ID or message.chat.id in ADMIN_CHAT_ID


@dp.message(F.text == "/report_today")
async def report_today(message: Message):
    if not _is_admin_context(message):
        return
    totals = await aggregate_for_day(_today())
    txt = format_daily_report(_today(), totals)
    await message.answer(txt)

@dp.message(F.text.startswith("/delete"))
async def delete_msg(message: Message):
    if not _is_admin_context(message):
        return
    parts = message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: <code>/delete &lt;msg_id&gt;</code>")
        return
    msg_id = int(parts[1])
    cnt, sum_no, sum_disc = await delete_by_msg_id(MANAGER_CHAT_ID, msg_id)
    await message.answer(
        f"Удалено {cnt} строк по msg_id={msg_id}. "
        f"Снято: без скидки {_format_cny(sum_no)}, со скидкой {_format_cny(sum_disc)}."
    )

@dp.message(F.text == "/undo")
async def undo_cmd(message: Message):
    # Разрешаем только из группы менеджера и только автору отменять своё
    if message.chat.id != MANAGER_CHAT_ID or not message.from_user:
        return
    ok, cnt, sum_no, sum_disc, msg_id = await undo_last_for_sender(message.from_user.id)
    if not ok:
        await message.reply("Нечего отменять за сегодня.")
        return
    await message.reply(
        f"Отменено (msg_id={msg_id}): {cnt} строк. "
        f"Минус: без скидки {_format_cny(sum_no)}, со скидкой {_format_cny(sum_disc)}."
    )

# ================== main() ==================
async def main():
    if not API_TOKEN:
        raise RuntimeError("Не задан токен в переменной окружения TGTOKEN_TEST")
    await init_db()
    # убрать возможный webhook, чтобы не было конфликтов при polling
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
