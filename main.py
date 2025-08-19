import asyncio
import os
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

# ======== НАСТРОЙКИ ========
API_TOKEN = os.getenv("TGTOKEN")  # задай токен в окружении

# Ставки выплат партнёру (в руб./за 1 юань)
PAY_NO_DISCOUNT_RUB_PER_CNY = 0.15   # без скидки
PAY_DISCOUNT_RUB_PER_CNY   = 0.10    # со скидкой

# Надбавка к курсу в проверочных формулах (в руб./за 1 юань)
CHECK_ADD_NO_DISCOUNT = 0.10
CHECK_ADD_DISCOUNT    = 0.05

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ======== КЛАВИАТУРЫ ========
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

# ======== СОСТОЯНИЯ ========
class ProfitStates(StatesGroup):
    rate_ge_30000 = State()
    rate_10000_30000 = State()
    rate_3000_10000 = State()
    rate_1000_3000 = State()
    rate_lt_1000 = State()
    amounts_mixed = State()   # единый столбик сумм

# ======== УТИЛИТЫ ========
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
    Возвращает (без_скидки, со_скидкой).
    Поддерживает:
      +1500    -> без скидки
      -2600    -> со скидкой
      1500 ns  / 1500 no / 1500 без
      2600 s   / 2600 со / 2600 скидка
      просто 1500 -> по умолчанию БЕЗ скидки
    Мусорные строки игнорируются.
    """
    no_disc, disc = [], []
    for raw in text.replace("\t", "\n").splitlines():
        line = raw.strip().lower().replace(",", ".")
        if not line:
            continue

        mark = None
        if line.startswith("+"):
            mark = "no"
            line = line[1:].strip()
        elif line.startswith("-"):
            mark = "disc"
            line = line[1:].strip()

        parts = line.split()
        num_part = parts[0] if parts else ""
        try:
            amount = float(num_part)
        except ValueError:
            filt = "".join(ch for ch in num_part if ch.isdigit() or ch in ".-")
            try:
                amount = float(filt)
            except ValueError:
                continue

        if amount <= 0:
            continue

        if mark is None and len(parts) > 1:
            tag = parts[1]
            if tag in ("s", "so", "скидка", "со", "с", "disc", "d"):
                mark = "disc"
            elif tag in ("ns", "no", "без", "b", "nd"):
                mark = "no"

        if mark == "disc":
            disc.append(amount)
        else:
            no_disc.append(amount)
    return no_disc, disc

# ======== ХЭНДЛЕРЫ ========
@dp.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Привет! Этот бот считает ежедневную выплату партнёру по обмену. Выбери действие:",
                         reply_markup=main_kb)

@dp.message(F.text == "🚫 Отмена")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. Возвращаю в главное меню.", reply_markup=main_kb)

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
        "• <b>+1500</b> — без скидки\n"
        "• <b>-2600</b> — со скидкой\n"
        "Также можно: <code>1500 ns</code> (без) или <code>2600 s</code> (со).\n"
        "Пример:\n<code>\n+1500\n-900\n+12000\n-2600\n</code>",
        reply_markup=cancel_kb
    )
    await state.set_state(ProfitStates.amounts_mixed)

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
    msg.append("<b>Итоги за день</b>\n")
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
        cut_idx = msg.index("<b>Проверьте суммы в рублях (без скидки):</b>")
        text = "\n".join(msg[:cut_idx]) + "\n\n<i>Список проверочных расчётов слишком длинный. Уменьшите количество строк.</i>"

    await message.answer(text, reply_markup=main_kb)
    await state.clear()

# ======== ЗАПУСК ========
async def main():
    if not API_TOKEN:
        raise RuntimeError("Не задан токен в переменной окружения TGTOKEN_TEST")
    # снимаем возможный webhook, чтобы не было Conflict
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
