import asyncio
import os
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

# ======== НАСТРОЙКИ ========
API_TOKEN = os.getenv("TGTOKEN")

# Ставки выплат партнёру (в руб./за 1 юань)
PAY_NO_DISCOUNT_RUB_PER_CNY = 0.15   # без скидки
PAY_DISCOUNT_RUB_PER_CNY   = 0.10   # со скидкой

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
    keyboard=[
        [KeyboardButton(text="🚫 Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

# ======== STATES ========
class ProfitStates(StatesGroup):
    rate_ge_30000 = State()
    rate_10000_30000 = State()
    rate_3000_10000 = State()
    rate_1000_3000 = State()
    rate_lt_1000 = State()
    amounts_no_discount = State()
    amounts_discount = State()

# ======== УТИЛИТЫ ========
def _parse_float(text: str) -> float | None:
    # поддержка запятых и точек
    t = text.strip().replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None

def _parse_multiline_numbers(text: str) -> list[float]:
    lines = [l.strip() for l in text.replace("\t", "\n").split("\n")]
    nums: list[float] = []
    for ln in lines:
        if not ln:
            continue
        # пробуем вытащить первое валидное число из строки
        ln_norm = ln.replace(",", ".")
        # оставим только цифры, точку и минус и пробелы
        # но проще: попытаемся по целой строке
        try:
            val = float(ln_norm)
            nums.append(val)
            continue
        except ValueError:
            # попробовать оставить только допустимые символы
            filtered = "".join(ch for ch in ln_norm if (ch.isdigit() or ch in ".-"))
            if filtered in ("", ".", "-"):
                continue
            try:
                val = float(filtered)
                nums.append(val)
            except ValueError:
                # игнорируем мусорные строки
                pass
    return nums

def _format_rub(x: float) -> str:
    return f"{x:,.2f} ₽".replace(",", " ").replace(".00", ".00")

def _format_cny(x: float) -> str:
    return f"{x:,.2f} ¥".replace(",", " ")

def _pick_rate_for_amount(cny_amount: float, rates: dict) -> float:
    """
    rates: {
      'ge_30000', 'r_10000_30000', 'r_3000_10000', 'r_1000_3000', 'lt_1000'
    }
    """
    if cny_amount >= 30000:
        return rates["ge_30000"]
    if 10000 <= cny_amount < 30000:
        return rates["r_10000_30000"]
    if 3000 <= cny_amount < 10000:
        return rates["r_3000_10000"]
    if 1000 <= cny_amount < 3000:
        return rates["r_1000_3000"]
    return rates["lt_1000"]

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
    if rate is None or rate <= 0:
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
    if rate is None or rate <= 0:
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
    if rate is None or rate <= 0:
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
    if rate is None or rate <= 0:
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
    if rate is None or rate <= 0:
        await message.answer("Введите положительное число, например: 12.00")
        return
    data = await state.get_data()
    rates = data.get("rates", {})
    rates["lt_1000"] = rate
    await state.update_data(rates=rates)
    await message.answer(
        "Введите <b>в столбик</b> суммы юаней заявок, которые были <b>по курсу без скидок</b>.\n"
        "Пример:\n<code>1500\n3500\n12000\n...</code>"
    )
    await state.set_state(ProfitStates.amounts_no_discount)

@dp.message(ProfitStates.amounts_no_discount)
async def amounts_no_discount(message: Message, state: FSMContext):
    vals = _parse_multiline_numbers(message.text)
    if not vals:
        await message.answer("Не нашёл чисел. Введите суммы юаней по одной в строке.")
        return
    await state.update_data(amounts_no_discount=vals)
    await message.answer(
        "Теперь введите <b>в столбик</b> суммы юаней заявок, которые были <b>по курсу со скидкой</b>.\n"
        "Пример:\n<code>900\n2600\n8000\n...</code>"
    )
    await state.set_state(ProfitStates.amounts_discount)

@dp.message(ProfitStates.amounts_discount)
async def amounts_discount(message: Message, state: FSMContext):
    vals = _parse_multiline_numbers(message.text)
    if not vals:
        await message.answer("Не нашёл чисел. Введите суммы юаней по одной в строке.")
        return

    data = await state.get_data()
    rates: dict = data["rates"]
    amounts_no = data.get("amounts_no_discount", [])
    amounts_disc = vals

    sum_no = sum(amounts_no)
    sum_disc = sum(amounts_disc)
    total_cny = sum_no + sum_disc

    payout_no = sum_no * PAY_NO_DISCOUNT_RUB_PER_CNY
    payout_disc = sum_disc * PAY_DISCOUNT_RUB_PER_CNY
    payout_total = payout_no + payout_disc

    # Проверочные строки (в рублях) по формулам
    lines_check_no = []
    for a in amounts_no:
        r = _pick_rate_for_amount(a, rates)
        rub = a * (r + CHECK_ADD_NO_DISCOUNT)
        lines_check_no.append(f"{a:,.2f} ¥ × ({r:.4f} + {CHECK_ADD_NO_DISCOUNT:.2f}) = {_format_rub(rub)}".replace(",", " "))

    lines_check_disc = []
    for a in amounts_disc:
        r = _pick_rate_for_amount(a, rates)
        rub = a * (r + CHECK_ADD_DISCOUNT)
        lines_check_disc.append(f"{a:,.2f} ¥ × ({r:.4f} + {CHECK_ADD_DISCOUNT:.2f}) = {_format_rub(rub)}".replace(",", " "))

    # Красивый вывод
    msg = []
    msg.append("<b>Итоги за день</b>")
    msg.append("")
    msg.append("<b>Курсы (₽/¥):</b>")
    msg.append(f"больше 30000 ¥: <b>{rates['ge_30000']}</b>")
    msg.append(f"10000–30000 ¥: <b>{rates['r_10000_30000']}</b>")
    msg.append(f"3000–10000 ¥: <b>{rates['r_3000_10000']}</b>")
    msg.append(f"1000–3000 ¥: <b>{rates['r_1000_3000']}</b>")
    msg.append(f"до 1000 ¥: <b>{rates['lt_1000']}</b>")
    msg.append("")
    msg.append("<b>Суммы в юанях:</b>")
    msg.append(f"Без скидки: {_format_cny(sum_no)}")
    msg.append(f"Со скидкой: {_format_cny(sum_disc)}")
    msg.append(f"Всего: <b>{_format_cny(total_cny)}</b>")
    msg.append("")
    msg.append("<b>Выплаты партнёру:</b>")
    msg.append(f"Без скидки: {_format_cny(sum_no)} × {PAY_NO_DISCOUNT_RUB_PER_CNY:.2f} ₽/¥ = <b>{_format_rub(payout_no)}</b>")
    msg.append(f"Со скидкой: {_format_cny(sum_disc)} × {PAY_DISCOUNT_RUB_PER_CNY:.2f} ₽/¥ = <b>{_format_rub(payout_disc)}</b>")
    msg.append(f"Итого к выплате: <b>{_format_rub(payout_total)}</b>")
    msg.append("")
    msg.append("<b>Проверьте суммы в рублях (без скидки):</b>")
    if lines_check_no:
        msg.extend("• " + s for s in lines_check_no)
    else:
        msg.append("—")
    msg.append("")
    msg.append("<b>Проверьте суммы в рублях (со скидкой):</b>")
    if lines_check_disc:
        msg.extend("• " + s for s in lines_check_disc)
    else:
        msg.append("—")

    # Отправляем и сбрасываем состояние
    text = "\n".join(msg)
    if len(text) > 3900:
        # Если вдруг слишком длинно — присечём проверочные части
        text = "\n".join(msg[:msg.index("<b>Проверьте суммы в рублях (без скидки):</b>")])
        text += "\n\n<i>Список проверочных расчётов слишком длинный. Введите меньше строк или разделите на несколько запусков.</i>"

    await message.answer(text, reply_markup=main_kb)
    await state.clear()

# ======== ЗАПУСК ========
async def main():
    if not API_TOKEN:
        raise RuntimeError("Не задан токен в переменной окружения TGTOKEN_TEST")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
