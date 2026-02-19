import asyncio
import os
import time
import uuid
import functools
from concurrent.futures import ThreadPoolExecutor
from cachetools import TTLCache
import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    FSInputFile,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, ImageDraw, ImageFont

from configs.fonts import FONTS
from configs.layout import LAYOUT, BYBIT_CUSTOM_LAYOUT
from utils.draw_text import draw_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =====================================================
# ThreadPool для CPU-heavy PIL рендеринга
# =====================================================
_THREAD_POOL = ThreadPoolExecutor(max_workers=os.cpu_count() or 4)

# =====================================================
# Кэш цен (TTL 10 сек) и точности (TTL 1 час)
# =====================================================
_PRICE_CACHE: TTLCache = TTLCache(maxsize=512, ttl=10)
_PRECISION_CACHE: TTLCache = TTLCache(maxsize=512, ttl=3600)


# =====================================================
# Кэш шрифтов, шаблонов, иконок — грузятся ОДИН РАЗ
# =====================================================
@functools.lru_cache(maxsize=128)
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


@functools.lru_cache(maxsize=32)
def _load_template(path: str) -> Image.Image:
    return Image.open(path).convert("RGBA")


@functools.lru_cache(maxsize=64)
def _load_icon(path: str, size: int) -> Image.Image:
    icon = Image.open(path).convert("RGBA")
    return icon.resize((size, size), Image.LANCZOS)


# =====================================================
# FSM
# =====================================================
class CustomExchange(StatesGroup):
    username = State()
    side = State()
    symbol = State()
    entry = State()
    exit_price = State()
    leverage = State()
    referral = State()
    datetime_str = State()


class TradeForm(StatesGroup):
    exchange = State()
    symbol = State()
    side = State()
    entry = State()
    mark = State()
    amount = State()
    deposit = State()
    leverage = State()


class MarathonStatesGroup(StatesGroup):
    start_deposit = State()


BASE_H = 467


def scale_font(size: int, img_h: int) -> int:
    return max(10, int(size * img_h / BASE_H))


def px(val: float, size: int) -> int:
    return int(val * size)


# =====================================================
# BOT
# =====================================================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# =====================================================
# МАРАФОН (в памяти)
# =====================================================
MARATHON: dict[int, dict[str, float]] = {}

# =====================================================
# aiohttp сессия (переиспользуется для всех запросов)
# =====================================================
_HTTP_SESSION: aiohttp.ClientSession | None = None


async def get_http_session() -> aiohttp.ClientSession:
    global _HTTP_SESSION
    if _HTTP_SESSION is None or _HTTP_SESSION.closed:
        connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=5)
        _HTTP_SESSION = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _HTTP_SESSION


# =====================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================
async def safe_delete_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


def _cleanup_old_files(directory: str, prefix: str, max_age_seconds: int = 3600) -> None:
    try:
        now = time.time()
        for fname in os.listdir(directory):
            if fname.startswith(prefix):
                fpath = os.path.join(directory, fname)
                try:
                    if now - os.path.getmtime(fpath) > max_age_seconds:
                        os.remove(fpath)
                except OSError:
                    pass
    except Exception:
        pass


async def parse_float(message: Message) -> float | None:
    try:
        return float(message.text.replace(",", "."))
    except (ValueError, AttributeError):
        await message.answer("Введите число 🙏")
        return None


# =====================================================
# КЛАВИАТУРЫ (предсозданные — не пересоздавать каждый раз)
# =====================================================
restart_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="🔁 В начало", callback_data="restart")]]
)
exchange_kb = InlineKeyboardMarkup(
    inline_keyboard=[[
        InlineKeyboardButton(text="⚫ Bybit", callback_data="exchange_bybit"),
        InlineKeyboardButton(text="🔵 BingX", callback_data="exchange_bingx"),
    ]]
)
side_kb = InlineKeyboardMarkup(
    inline_keyboard=[[
        InlineKeyboardButton(text="📈 Long", callback_data="side_long"),
        InlineKeyboardButton(text="📉 Short", callback_data="side_short"),
    ]]
)
back_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]
)
mark_price_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📡 Взять цену с биржи", callback_data="get_mark_price")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ]
)
skip_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_field")]]
)

_MAIN_KB: InlineKeyboardMarkup | None = None


def get_main_kb() -> InlineKeyboardMarkup:
    global _MAIN_KB
    if _MAIN_KB is None:
        kb = InlineKeyboardBuilder()
        kb.button(text="📊 Bybit", callback_data="exchange_bybit")
        kb.button(text="📊 BingX", callback_data="exchange_bingx")
        kb.button(text="🎨 Кастом Bybit", callback_data="custom_bybit")
        kb.button(text="🎨 Кастом BingX", callback_data="custom_bingx")
        kb.button(text="🏁 Марафон", callback_data="marathon:menu")
        kb.adjust(1)
        _MAIN_KB = kb.as_markup()
    return _MAIN_KB


# =====================================================
# START / TEST
# =====================================================
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer("Выбери режим:", reply_markup=get_main_kb())


@dp.message(Command("test_bybit"))
async def test_bybit(message: Message):
    exchange = "bybit"
    amount = 100
    entry = 42000
    mark = 43250
    leverage = 20
    side = "long"
    qty = calculate_qty(exchange, amount, entry, leverage)
    cost = calculate_cost(exchange, amount, leverage)
    pnl_usdt, margin_pos, percent = calculate_pnl_linear(entry, mark, qty, side, leverage)
    pnl = percent
    fake_data = {
        "exchange": exchange, "symbol": "BTCUSDT", "side": side,
        "entry": entry, "mark": mark, "amount": amount, "deposit": 5000,
        "leverage": leverage, "qty": qty,
        "liquidation": calculate_liquidation(entry, leverage, side), "cost": cost,
    }
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(_THREAD_POOL, generate_trade_image, fake_data, percent, pnl, pnl_usdt)
    await message.answer_photo(FSInputFile(path))


@dp.message(Command("test_bingx"))
async def test_bingx(message: Message):
    exchange = "bingx"
    amount = 100
    entry = 42000
    mark = 43250
    leverage = 20
    side = "long"
    qty = calculate_qty(exchange, amount, entry, leverage)
    cost = calculate_cost(exchange, amount, leverage)
    pnl_usdt, margin_pos, percent = calculate_pnl_linear(entry, mark, qty, side, leverage)
    pnl = percent
    liquidation = calculate_liquidation(entry, leverage, side)
    fake_data = {
        "exchange": exchange, "symbol": "BTCUSDT", "side": side,
        "entry": entry, "mark": mark, "amount": amount, "deposit": 5000,
        "leverage": leverage, "qty": qty, "liquidation": liquidation, "cost": cost,
    }
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(_THREAD_POOL, generate_trade_image, fake_data, percent, pnl, pnl_usdt)
    await message.answer_photo(FSInputFile(path))


@dp.message(Command("test_bybit_custom"))
async def test_bybit_custom(message: Message):
    entry = 0.1068
    exit_price = 0.1092
    leverage_str = "50x"
    side = "long"
    leverage = float(leverage_str.replace("x", ""))
    pnl_percent = ((exit_price - entry) / entry * 100) * leverage if side == "long" else ((entry - exit_price) / entry * 100) * leverage
    image_data = {
        "username": "ТЕСТ ПОЛЬЗОВАТЕЛЬ", "symbol": "WLFIUSDT",
        "pnl": round(pnl_percent, 2), "entry": entry, "exit": exit_price,
        "leverage": leverage_str, "side": side, "referral": "D1BFA4", "datetime_str": "02/14 19:00",
    }
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bybit_image, image_data)
    await message.answer_photo(FSInputFile(path))


@dp.message(Command("test_bingx_custom"))
async def test_bingx_custom(message: Message):
    entry = 0.1068
    exit_price = 0.1092
    leverage_str = "50x"
    side = "long"
    leverage = float(leverage_str.replace("x", ""))
    pnl_percent = ((exit_price - entry) / entry * 100) * leverage if side == "long" else ((entry - exit_price) / entry * 100) * leverage
    image_data = {
        "username": "ТЕСТ ПОЛЬЗОВАТЕЛЬ", "symbol": "WLFIUSDT",
        "pnl": round(pnl_percent, 2), "entry": entry, "exit": exit_price,
        "leverage": leverage_str, "side": side, "referral": "D1BFA4", "datetime_str": "02/14 19:00",
    }
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bingx_image, image_data)
    await message.answer_photo(FSInputFile(path))


@dp.message(Command("test_all"))
async def test_all(message: Message):
    text = (
        "Тестовые команды:
"
        "/test_bybit_long
/test_bybit_short
"
        "/test_bingx_long
/test_bingx_short
"
        "/test_custom_bybit_long
/test_custom_bybit_short
"
        "/test_custom_bingx_long
/test_custom_bingx_short
"
    )
    await message.answer(text)


@dp.message(Command("test_bybit_long"))
async def test_bybit_long(message: Message):
    await _run_spot_test(message, exchange="bybit", side="long")


@dp.message(Command("test_bybit_short"))
async def test_bybit_short(message: Message):
    await _run_spot_test(message, exchange="bybit", side="short")


@dp.message(Command("test_bingx_long"))
async def test_bingx_long(message: Message):
    await _run_spot_test(message, exchange="bingx", side="long")


@dp.message(Command("test_bingx_short"))
async def test_bingx_short(message: Message):
    await _run_spot_test(message, exchange="bingx", side="short")


async def _run_spot_test(message: Message, exchange: str, side: str):
    amount = 100
    entry = 42000
    mark = 43250 if side == "long" else 41000
    leverage = 20
    qty = calculate_qty(exchange, amount, entry, leverage)
    cost = calculate_cost(exchange, amount, leverage)
    pnl_usdt, margin_pos, percent = calculate_pnl_linear(entry, mark, qty, side, leverage)
    pnl = percent
    liquidation = calculate_liquidation(entry, leverage, side)
    data = {
        "exchange": exchange, "symbol": "PYTHUSDT", "side": side,
        "entry": entry, "mark": mark, "amount": amount, "deposit": 50,
        "leverage": leverage, "qty": qty, "liquidation": liquidation, "cost": cost,
    }
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(_THREAD_POOL, generate_trade_image, data, percent, pnl, pnl_usdt)
    await message.answer_photo(FSInputFile(path))
    user_id = message.from_user.id
    marathon = MARATHON.get(user_id)
    if marathon is not None:
        marathon["balance"] += round(pnl_usdt, 2)
        start_v = marathon["start"]
        balance = marathon["balance"]
        pnl_total = balance - start_v
        pnl_pct = (pnl_total / start_v * 100) if start_v else 0.0
        await message.answer(
            f"🏁 Марафон
Старт: {start_v:.2f} USDT
"
            f"Текущий баланс: {balance:.2f} USDT
"
            f"Итог: {pnl_total:+.2f} USDT ({pnl_pct:+.2f}%)"
        )


@dp.message(Command("test_custom_bybit_long"))
async def test_custom_bybit_long(message: Message):
    await _run_custom_test(message, exchange="bybit", side="long")


@dp.message(Command("test_custom_bybit_short"))
async def test_custom_bybit_short(message: Message):
    await _run_custom_test(message, exchange="bybit", side="short")


@dp.message(Command("test_custom_bingx_long"))
async def test_custom_bingx_long(message: Message):
    await _run_custom_test(message, exchange="bingx", side="long")


@dp.message(Command("test_custom_bingx_short"))
async def test_custom_bingx_short(message: Message):
    await _run_custom_test(message, exchange="bingx", side="short")


async def _run_custom_test(message: Message, exchange: str, side: str):
    entry = 0.1068
    exit_price = 0.1092 if side == "long" else 0.1040
    leverage_str = "50x"
    leverage = float(leverage_str.replace("x", ""))
    pnl_percent = ((exit_price - entry) / entry * 100) * leverage if side == "long" else ((entry - exit_price) / entry * 100) * leverage
    image_data = {
        "username": "ТЕСТ ПОЛЬЗОВАТЕЛЬ", "symbol": "PYTHUSDT",
        "pnl": round(pnl_percent, 2), "entry": entry, "exit": exit_price,
        "leverage": leverage_str, "side": side, "referral": "D1BFA4", "datetime_str": "02/14 19:00",
    }
    loop = asyncio.get_event_loop()
    if exchange == "bingx":
        path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bingx_image, image_data)
    else:
        path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bybit_image, image_data)
    await message.answer_photo(FSInputFile(path))


# =====================================================
# МАРАФОН
# =====================================================
@dp.callback_query(F.data == "marathon:menu")
async def marathon_menu(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    marathon = MARATHON.get(user_id)
    if marathon is None:
        await call.message.answer("Марафон ещё не запущен.

Отправь стартовый депозит (например, 100).")
        await state.set_state(MarathonStatesGroup.start_deposit)
    else:
        start_v = marathon["start"]
        balance = marathon["balance"]
        pnl_total = balance - start_v
        pnl_pct = (pnl_total / start_v * 100) if start_v else 0.0
        kb = InlineKeyboardBuilder()
        kb.button(text="🚀 Сделка в марафоне", callback_data="marathon:start")
        kb.button(text="🛑 Выключить марафон", callback_data="marathon:stop")
        kb.adjust(1)
        await call.message.answer(
            f"🏁 Марафон
Старт: {start_v:.2f} USDT
"
            f"Текущий баланс: {balance:.2f} USDT
"
            f"Итог: {pnl_total:+.2f} USDT ({pnl_pct:+.2f}%)",
            reply_markup=kb.as_markup(),
        )
    await call.answer()


@dp.message(MarathonStatesGroup.start_deposit)
async def marathon_set_start(message: Message, state: FSMContext):
    try:
        start_v = float(message.text.replace(",", "."))
        if start_v <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи положительное число, например: 100")
        return
    user_id = message.from_user.id
    MARATHON[user_id] = {"start": start_v, "balance": start_v}
    await state.clear()
    await message.answer(
        f"Марафон запущен!

Стартовый депозит: {start_v:.2f} USDT.
"
        "Теперь сделки в марафоне будут считать результат от этого депозита.
"
        "Сейчас выбери биржу для первой сделки в марафоне."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Bybit", callback_data="exchange_bybit")
    kb.button(text="📊 BingX", callback_data="exchange_bingx")
    kb.adjust(1)
    await message.answer("Выбери биржу для сделки в марафоне:", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "marathon:start")
async def marathon_start(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    if user_id not in MARATHON:
        await call.message.answer("Сначала запусти марафон и задай стартовый депозит через 🏁 Марафон.")
        await call.answer()
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Bybit", callback_data="exchange_bybit")
    kb.button(text="📊 BingX", callback_data="exchange_bingx")
    kb.adjust(1)
    await call.message.answer("Выбери биржу для сделки в марафоне:", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data == "marathon:stop")
async def marathon_stop(call: CallbackQuery, state: FSMContext):
    MARATHON.pop(call.from_user.id, None)
    await state.clear()
    await call.message.answer("Марафон выключен. Теперь сделки считаются без марафона.")
    await call.answer()


# =====================================================
# НАВИГАЦИЯ TRADEFORM
# =====================================================
@dp.callback_query(lambda c: c.data == "restart")
async def restart(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Выбери режим:", reply_markup=get_main_kb())
    await call.answer()


@dp.callback_query(lambda c: c.data == "back")
async def go_back(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    prev = data.get("prev_state")
    steps = {
        TradeForm.symbol: ("Введи монету (например BTCUSDT)", TradeForm.symbol, None),
        TradeForm.side: ("Выбери направление 👇", TradeForm.side, side_kb),
        TradeForm.entry: ("Введите цену входа:", TradeForm.entry, back_kb),
        TradeForm.mark: ("Введите цену маркировки:", TradeForm.mark, mark_price_kb),
        TradeForm.amount: ("На какую сумму заходишь? (USDT)", TradeForm.amount, back_kb),
        TradeForm.deposit: ("Какой депозит? (USDT)", TradeForm.deposit, back_kb),
        TradeForm.leverage: ("Введите плечо (например 10)", TradeForm.leverage, back_kb),
    }
    step = steps.get(prev)
    if step:
        text, st, kb = step
        await show_step(call.message, state, text, kb)
        await state.set_state(st)
    else:
        await call.message.answer("Выбери режим:", reply_markup=get_main_kb())
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("exchange_"))
async def exchange_selected(call: CallbackQuery, state: FSMContext):
    await state.update_data(exchange=call.data.split("_")[1], prev_state=TradeForm.exchange)
    await show_step(call.message, state, "Введи монету (например BTCUSDT)")
    await state.set_state(TradeForm.symbol)
    await call.answer()


@dp.message(TradeForm.symbol)
async def get_symbol(message: Message, state: FSMContext):
    symbol = message.text.upper()
    data = await state.get_data()
    exchange = data.get("exchange")
    precision = await async_get_price_precision(exchange, symbol)
    await state.update_data(symbol=symbol, price_precision=precision, prev_state=TradeForm.symbol)
    await safe_delete_message(message)
    await show_step(message, state, "Выбери направление 👇", side_kb)
    await state.set_state(TradeForm.side)


@dp.callback_query(TradeForm.side, lambda c: c.data in ("side_long", "side_short"))
async def side_selected(call: CallbackQuery, state: FSMContext):
    side = "long" if call.data == "side_long" else "short"
    await state.update_data(side=side, prev_state=TradeForm.side)
    await show_step(call.message, state, "Введите цену входа:", back_kb)
    await state.set_state(TradeForm.entry)
    await call.answer()


@dp.message(TradeForm.entry)
async def get_entry(message: Message, state: FSMContext):
    value = await parse_float(message)
    if value is None:
        return
    await state.update_data(entry=value, prev_state=TradeForm.entry)
    await safe_delete_message(message)
    await show_step(message, state, "Введите цену маркировки:", mark_price_kb)
    await state.set_state(TradeForm.mark)


@dp.message(TradeForm.mark)
async def get_mark(message: Message, state: FSMContext):
    value = await parse_float(message)
    if value is None:
        return
    await state.update_data(mark=value, prev_state=TradeForm.mark)
    await safe_delete_message(message)
    await show_step(message, state, "На какую сумму заходишь? (USDT)", back_kb)
    await state.set_state(TradeForm.amount)


@dp.message(TradeForm.amount)
async def get_amount(message: Message, state: FSMContext):
    value = await parse_float(message)
    if value is None:
        return
    await state.update_data(amount=value, prev_state=TradeForm.amount)
    await safe_delete_message(message)
    user_id = message.from_user.id
    marathon = MARATHON.get(user_id)
    if marathon is not None:
        await state.update_data(deposit=marathon["balance"], prev_state=TradeForm.deposit)
        await show_step(message, state, "Введите плечо (например 10)", back_kb)
        await state.set_state(TradeForm.leverage)
        return
    await show_step(message, state, "Какой депозит? (USDT)", back_kb)
    await state.set_state(TradeForm.deposit)


@dp.message(TradeForm.deposit)
async def get_deposit(message: Message, state: FSMContext):
    value = await parse_float(message)
    if value is None:
        return
    await state.update_data(deposit=value, prev_state=TradeForm.deposit)
    await safe_delete_message(message)
    await show_step(message, state, "Введите плечо (например 10)", back_kb)
    await state.set_state(TradeForm.leverage)


@dp.message(TradeForm.leverage)
async def get_leverage(message: Message, state: FSMContext):
    try:
        leverage = int(message.text)
        if leverage <= 0 or leverage > 125:
            raise ValueError
    except ValueError:
        await message.answer("Введите число от 1 до 125")
        return
    await safe_delete_message(message)
    data = await state.get_data()
    user_id = message.from_user.id
    marathon = MARATHON.get(user_id)
    if marathon is not None:
        data["deposit"] = marathon["balance"]

    qty = calculate_qty(data["exchange"], data["amount"], data["entry"], leverage)
    cost = calculate_cost(data["exchange"], data["amount"], leverage)
    pnl_usdt, margin_pos, percent = calculate_pnl_linear(
        data["entry"], data["mark"], qty, data["side"], leverage
    )
    pnl = percent
    liquidation = calculate_liquidation(data["entry"], leverage, data["side"])
    data.update(leverage=leverage, qty=qty, liquidation=liquidation, cost=cost)

    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(_THREAD_POOL, generate_trade_image, data, percent, pnl, pnl_usdt)
    await message.answer_photo(FSInputFile(path), reply_markup=restart_kb)

    if marathon is not None:
        marathon["balance"] += pnl_usdt
        start_v = marathon["start"]
        balance = marathon["balance"]
        pnl_total = balance - start_v
        pnl_pct = (pnl_total / start_v * 100) if start_v else 0.0
        await message.answer(
            f"🏁 Марафон
Старт: {start_v:.2f} USDT
"
            f"Текущий баланс: {balance:.2f} USDT
"
            f"Итог: {pnl_total:+.2f} USDT ({pnl_pct:+.2f}%)"
        )
    await state.clear()


# =====================================================
# API: ASYNC цены и точность (с кэшем)
# =====================================================
async def async_get_mark_price(exchange: str, symbol: str) -> float | None:
    cache_key = f"price:{exchange}:{symbol}"
    if cache_key in _PRICE_CACHE:
        return _PRICE_CACHE[cache_key]
    try:
        session = await get_http_session()
        if exchange == "bybit":
            async with session.get(
                "https://api.bybit.com/v5/market/tickers",
                params={"category": "linear", "symbol": symbol}
            ) as r:
                data = await r.json()
            price = float(data["result"]["list"][0]["markPrice"])
        elif exchange == "bingx":
            sym = symbol.replace("USDT", "-USDT") if "-" not in symbol else symbol
            async with session.get(
                "https://open-api.bingx.com/openApi/swap/v2/quote/price",
                params={"symbol": sym}
            ) as r:
                data = await r.json()
            price = float(data["data"]["price"])
        else:
            return None
        _PRICE_CACHE[cache_key] = price
        return price
    except Exception as e:
        print("MARK PRICE ERROR:", e)
        return None


async def async_get_price_precision(exchange: str, symbol: str) -> int | None:
    cache_key = f"precision:{exchange}:{symbol}"
    if cache_key in _PRECISION_CACHE:
        return _PRECISION_CACHE[cache_key]
    try:
        session = await get_http_session()
        if exchange == "bybit":
            async with session.get(
                "https://api.bybit.com/v5/market/instruments-info",
                params={"category": "linear", "symbol": symbol}
            ) as r:
                data = await r.json()
            tick = data["result"]["list"][0]["priceFilter"]["tickSize"]
            precision = len(tick.split(".")[1].rstrip("0")) if "." in tick else 0
        elif exchange == "bingx":
            async with session.get("https://open-api.bingx.com/openApi/swap/v2/quote/contracts") as r:
                data = await r.json()
            precision = next(
                (int(item["pricePrecision"]) for item in data["data"] if item["symbol"] == symbol), 2
            )
        else:
            return None
        _PRECISION_CACHE[cache_key] = precision
        return precision
    except Exception as e:
        print("PRECISION ERROR:", e)
        return None


# =====================================================
# КНОПКА: взять цену с биржи
# =====================================================
@dp.callback_query(lambda c: c.data == "get_mark_price")
async def get_mark_from_exchange(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    exchange = data.get("exchange")
    symbol = data.get("symbol")
    if not exchange or not symbol:
        await call.answer("Нет данных", show_alert=True)
        return
    price = await async_get_mark_price(exchange, symbol)
    if price is None:
        await call.answer("Не удалось получить цену", show_alert=True)
        return
    await state.update_data(mark=price, prev_state=TradeForm.mark)
    try:
        await call.message.delete()
    except Exception:
        pass
    await show_step(call.message, state, "На какую сумму заходишь? (USDT)", back_kb)
    await state.set_state(TradeForm.amount)
    await call.answer("Цена получена ✅")


# =====================================================
# РАСЧЁТЫ
# =====================================================
def calculate_qty(exchange: str, amount: float, entry: float, leverage: int) -> float:
    qty = amount * leverage / entry
    if exchange == "bybit":
        return round(qty, 4)
    if exchange == "bingx":
        return round(qty, 2)
    return round(qty, 4)


def format_price(value: float, precision: int | None = None) -> str:
    if precision is not None:
        return f"{value:,.{precision}f}"
    if value == 0:
        return "0"
    if value >= 1000:
        return f"{value:,.2f}"
    elif value >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    else:
        return f"{value:.8f}".rstrip("0").rstrip(".")


def calculate_liquidation(entry: float, leverage: int | float, side: str, mm: float = 0.005) -> float:
    if side == "long":
        return entry * (1 - 1 / leverage + mm)
    else:
        return entry * (1 + 1 / leverage - mm)


def calculate_cost(exchange: str, amount: float, leverage: int | float) -> float:
    return round(amount * leverage, 2)


def calculate_pnl_linear(
    entry: float, mark: float, qty: float, side: str, leverage: float
) -> tuple[float, float, float]:
    if side not in ("long", "short"):
        raise ValueError("side must be 'long' or 'short'")
    pnl_usd = qty * (mark - entry) if side == "long" else qty * (entry - mark)
    margin = entry * qty / leverage if leverage else 0.0
    pnl_percent = (pnl_usd / margin * 100) if margin > 0 else 0.0
    return round(pnl_usd, 4), round(margin, 4), round(pnl_percent, 2)


def calculate_pnl(entry: float, mark: float, side: str, leverage: float) -> tuple[float, float]:
    pnl_usd, margin, pnl_percent = calculate_pnl_linear(entry, mark, 1.0, side, leverage)
    return pnl_percent, pnl_usd


# =====================================================
# SUMMARY / show_step
# =====================================================
def build_summary(data: dict) -> str:
    text = "📊 Уже введено:
"
    if "exchange" in data:
        text += f"🏦 Биржа: {data['exchange'].title()}
"
    if "symbol" in data:
        text += f"🪙 Монета: {data['symbol']}
"
    if "side" in data:
        text += f"📈 Направление: {'Лонг' if data['side'] == 'long' else 'Шорт'}
"
    if "entry" in data:
        text += f"🎯 Вход: {data['entry']}
"
    if "mark" in data:
        text += f"📍 Марк: {data['mark']}
"
    if "amount" in data:
        text += f"💰 Сумма: {data['amount']} USDT
"
    if "deposit" in data:
        text += f"🏦 Депозит: {data['deposit']} USDT
"
    return text


def build_custom_summary(data: dict) -> str:
    exchange = (data or {}).get("exchange", "bybit").title()
    text = f"📊 КАСТОМ {exchange}

"
    if not data:
        return text
    if "username" in data:
        text += f"👤 {data['username']}
"
    if "symbol" in data:
        text += f"🪙 {data['symbol']}
"
    if "side" in data:
        side_emoji = "📈" if data["side"] == "long" else "📉"
        text += f"{side_emoji} {'Лонг' if data['side'] == 'long' else 'Шорт'}
"
    if "entry" in data:
        text += f"💰 Вход: {data['entry']}
"
    if "exit" in data:
        text += f"🚪 Выход: {data['exit']}
"
    if "leverage" in data:
        text += f"⚙️ {data['leverage']}
"
    if "referral" in data:
        text += f"👥 Рефкод: {data['referral']}
"
    if "datetime_str" in data:
        text += f"🕒 {data['datetime_str']}
"
    return text


_PRETTY_QUESTIONS = {
    "Введи монету (например BTCUSDT)": "🪙 Введите монету:",
    "Выбери направление 👇": "📈 Направление сделки:",
    "Введите цену входа:": "💰 Цена входа:",
    "Введите цену маркировки:": "📍 Цена сейчас:",
    "На какую сумму заходишь? (USDT)": "💵 Сумма (USDT):",
    "Какой депозит? (USDT)": "🏦 Депозит (USDT):",
    "Введите плечо (например 10)": "⚙️ Плечо:",
}


async def show_step(
    message: Message,
    state: FSMContext,
    question: str,
    keyboard: InlineKeyboardMarkup | None = None,
):
    data = await state.get_data()
    if "username" in data and data.get("exchange") in ("bybit", "bingx"):
        summary = build_custom_summary(data)
    else:
        summary = build_summary(data)
    question_text = _PRETTY_QUESTIONS.get(question, f"❓ {question}")
    last_msg_id = data.get("last_bot_msg_id") or data.get("custom_last_msg_id")
    if last_msg_id:
        try:
            await message.bot.delete_message(message.chat.id, last_msg_id)
        except Exception:
            pass
    msg = await message.answer(
        f"{summary}
{question_text}", parse_mode="HTML", reply_markup=keyboard
    )
    await state.update_data(last_bot_msg_id=msg.message_id, custom_last_msg_id=msg.message_id)


# =====================================================
# РЕНДЕР ОБЫЧНОЙ КАРТИНКИ
# =====================================================
def draw_gray_box(
    draw: ImageDraw.ImageDraw, x: int, y: int, text: str,
    font: ImageFont.FreeTypeFont, cfg: dict,
):
    padding_x = cfg.get("pad_x", 16)
    padding_y = cfg.get("pad_y", 10)
    radius = cfg.get("radius", 14)
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x1 = x - w // 2 - padding_x
    y1 = y - h // 2 - padding_y
    x2 = x + w // 2 + padding_x
    y2 = y + h // 2 + padding_y
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=(80, 80, 80))
    draw.text((x, y), text, fill=(255, 255, 255), font=font, anchor="mm")


def draw_side_badge(
    draw: ImageDraw.ImageDraw, x: int, y: int, text: str,
    color: tuple, exchange: str, fonts_cfg: dict, cfg: dict | None = None,
):
    img_h = draw.im.size[1]
    badge_size = fonts_cfg["sizes"]["badge"]
    badge_style = fonts_cfg.get("badge_style", "outline")
    font = _load_font(
        os.path.join(BASE_DIR, fonts_cfg["files"]["regular"]),
        scale_font(badge_size, img_h),
    )
    if exchange == "bingx" and cfg is not None:
        box_w = cfg.get("w", 140)
        box_h = cfg.get("h", 48)
        radius = cfg.get("radius", 18)
    else:
        padding_x = 16
        padding_y = 18
        radius = cfg.get("radius", 20) if cfg is not None else 20
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        box_w = text_w + padding_x * 2
        box_h = text_h + padding_y * 1.5
    x1 = x - box_w // 2
    y1 = y - box_h // 2
    x2 = x1 + box_w
    y2 = y1 + box_h
    if badge_style == "filled":
        fill_color = color
        text_color = (255, 255, 255)
    else:
        fill_color = (30, 30, 30)
        text_color = color
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=fill_color)
    center_x = (x1 + x2) / 2
    center_y = (y1 + y2) / 2
    draw.text((center_x, center_y), text, fill=text_color, font=font, anchor="mm")


def clear_by_layout(img: Image.Image, draw: ImageDraw.ImageDraw, layout: dict, key: str):
    cfg = layout.get(key)
    if cfg is None:
        return
    iw, ih = img.size
    x = px(cfg["x"], iw)
    y = px(cfg["y"], ih)
    cw = px(cfg["w"], iw)
    ch = px(cfg["h"], ih)
    if "bg_x" in cfg and "bg_y" in cfg:
        bgx = px(cfg["bg_x"], iw)
        bgy = px(cfg["bg_y"], ih)
        bg = img.getpixel((bgx, bgy))
    else:
        bg = img.getpixel((x + 2, y + 2))
    draw.rectangle((x, y, x + cw, y + ch), fill=bg)


def draw_bingx_icon(
    img: Image.Image, symbol: str, layout: dict,
    font: ImageFont.FreeTypeFont, w: int, h: int,
):
    cfg = layout.get("symbol_icon")
    if not cfg:
        return
    icon_path = os.path.join(BASE_DIR, "assets", "bingx", "icon.png")
    if not os.path.exists(icon_path):
        return
    size = int(cfg.get("size", 24))
    icon = _load_icon(icon_path, size)
    x = int(cfg["x"] * w) + cfg.get("dx", 0)
    y = int(cfg["y"] * h) + cfg.get("dy", 0)
    dummy = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(dummy)
    bbox = d.textbbox((0, 0), symbol, font=font)
    text_width = bbox[2] - bbox[0]
    gap = cfg.get("gap", 8)
    x += text_width + gap
    img.paste(icon, (x, y), icon)


def generate_trade_image(data: dict, percent: float, pnl: float, pnl_usdt: float) -> str:
    exchange = data["exchange"]
    template_path = os.path.join(BASE_DIR, "assets", exchange, "template.png")
    output_dir = os.path.join(BASE_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"result_{uuid.uuid4().hex[:8]}.png")

    cfg = FONTS[exchange]
    layout = LAYOUT[exchange]
    font_regular = os.path.join(BASE_DIR, cfg["files"]["regular"])
    font_bold = os.path.join(BASE_DIR, cfg["files"]["bold"])
    sizes = cfg["sizes"]

    img = _load_template(template_path).copy()
    draw = ImageDraw.Draw(img)

    clear_keys = [
        "clear_symbol", "clear_leverage", "clear_side_badge", "clear_entry",
        "clear_mark", "clear_pnl", "clear_qty", "clear_liq", "clear_margin", "clear_risk",
    ]
    for key in clear_keys:
        if exchange == "bybit" and key == "clear_margin":
            continue
        clear_by_layout(img, draw, layout, key)

    WHITE = (255, 255, 255)
    GREEN = (0, 200, 120)
    RED = (230, 60, 60)
    ORANGE = (245, 166, 89)

    side_color = GREEN if data["side"] == "long" else RED
    pnl_color = GREEN if pnl >= 0 else RED

    symbol_font = _load_font(font_bold, sizes["symbol"])
    pnl_font = _load_font(font_bold, sizes["pnl"])
    lev_font = _load_font(font_regular, sizes["leverage"])

    w, h = img.size

    if exchange == "bingx":
        draw_bingx_icon(img, data["symbol"], layout, symbol_font, w, h)

    def pos(c: dict) -> tuple[int, int]:
        return int(c["x"] * w) + c.get("dx", 0), int(c["y"] * h) + c.get("dy", 0)

    symbol_text = data["symbol"]
    badge_text = "Лонг" if data["side"] == "long" else "Шорт"
    pnl_text = f"{pnl_usdt:+.2f}$ ({pnl:+.2f}%)"
    lev_text = f"Кросс {data['leverage']}x" if exchange == "bybit" else ""

    symbol_x, symbol_y = pos(layout["symbol"])
    pnl_x, pnl_y = pos(layout["pnl"])
    lev_x, lev_y = pos(layout["leverage"])
    badge_x, badge_y = pos(layout["side_badge"])

    draw.text((symbol_x, symbol_y), symbol_text, fill=WHITE, font=symbol_font,
              anchor=layout["symbol"]["anchor"])

    if exchange == "bybit":
        sym_bbox = draw.textbbox((0, 0), symbol_text, font=symbol_font)
        sym_width = sym_bbox[2] - sym_bbox[0]
        gap = 75
        layout_dx = layout["side_badge"].get("dx", 0)
        badge_x_final = symbol_x + sym_width + gap + layout_dx
        badge_y_final = badge_y
    else:
        badge_x_final = badge_x
        badge_y_final = badge_y

    draw_side_badge(draw, badge_x_final, badge_y_final, badge_text, side_color,
                    exchange, cfg, layout.get("side_badge"))

    draw.text((pnl_x, pnl_y), pnl_text, fill=pnl_color, font=pnl_font,
              anchor=layout["pnl"]["anchor"])
    draw.text((lev_x, lev_y), lev_text, fill=WHITE, font=lev_font,
              anchor=layout["leverage"]["anchor"])

    if exchange == "bingx":
        badge_font = _load_font(font_regular, sizes["leverage"])
        mx, my = pos(layout["margin_mode"])
        lbx, lby = pos(layout["leverage_bingx"])
        draw_gray_box(draw, mx, my, "Кросс", badge_font, layout["margin_mode"])
        draw_gray_box(draw, lbx, lby, f"{data['leverage']}x", badge_font, layout["leverage_bingx"])

    qty_text = f"{data['qty']:.4f}" if exchange == "bybit" else f"{data['qty']:.2f}"

    if exchange == "bingx":
        margin_text = f"{data['amount']:.2f}"
        draw_text(draw, layout, "margin", margin_text, font_regular, sizes["qty"], WHITE, w, h)

    draw_text(draw, layout, "qty", qty_text, font_regular, sizes["qty"], WHITE, w, h)

    precision = data.get("price_precision")
    draw_text(draw, layout, "entry", format_price(data["entry"], precision), font_regular, sizes["entry"], WHITE, w, h)
    draw_text(draw, layout, "mark", format_price(data["mark"], precision), font_regular, sizes["mark"], WHITE, w, h)
    draw_text(draw, layout, "liq", format_price(data["liquidation"], precision), font_regular, sizes["liq"], ORANGE, w, h)

    if exchange == "bingx" and "risk" in layout:
        entry_v = float(data.get("entry") or 0)
        qty_val = float(data.get("qty") or 0)
        margin_val = float(data.get("amount") or 0)
        position_margin = entry_v * qty_val
        if position_margin == 0 or margin_val == 0:
            risk_text = "--"
            risk_value = None
        else:
            risk = margin_val / position_margin * 100.0
            if round(risk, 2) == 0:
                risk_text = "--"
                risk_value = None
            else:
                risk_text = f"{risk:.2f}%"
                risk_value = risk
        rx, ry = pos(layout["risk"])
        risk_font = _load_font(font_regular, sizes["leverage"])
        if risk_value is None:
            risk_color = ORANGE
        elif risk_value <= 40:
            risk_color = GREEN
        elif risk_value <= 70:
            risk_color = ORANGE
        else:
            risk_color = RED
        draw.text((rx, ry), risk_text, fill=risk_color, font=risk_font,
                  anchor=layout["risk"]["anchor"])

    img.save(output_path)
    _cleanup_old_files(os.path.dirname(output_path), prefix="result_")
    return output_path


# =====================================================
# КАСТОМНЫЕ КАРТИНКИ
# =====================================================
def draw_custom_bingx_lines(
    img: Image.Image, data: dict, layout: dict,
    font_side: ImageFont.FreeTypeFont, font_symbol: ImageFont.FreeTypeFont,
    w: int, h: int,
) -> None:
    symbol = data["symbol"]
    cfg = layout.get("lines")
    if not cfg:
        return
    line_path = os.path.join(BASE_DIR, "assets", "bingx", "line.png")
    if not os.path.exists(line_path):
        return
    size = int(cfg.get("size", 80))
    line = _load_icon(line_path, size)
    base_x = int(cfg["x"] * w + cfg.get("dx", 0))
    base_y = int(cfg["y"] * h + cfg.get("dy", 0))
    dummy = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(dummy)
    bbox_sym = d.textbbox((0, 0), symbol, font=font_symbol)
    sym_width = bbox_sym[2] - bbox_sym[0]
    gap = cfg.get("gap", 10)
    spacing = cfg.get("spacing", 221)
    x1 = base_x + sym_width + gap
    y1 = base_y
    x2 = x1 + size + spacing
    y2 = base_y
    img.paste(line, (x1, y1), line)
    img.paste(line, (x2, y2), line)
    draw = ImageDraw.Draw(img)
    side_cfg = layout.get("side_position", {})
    side_x = int(side_cfg.get("x", 0.5) * w)
    side_y = int(side_cfg.get("y", 0.335) * h)
    side_text = "Long" if data.get("side") == "long" else "Short"
    side_color = (0, 200, 120) if data.get("side") == "long" else (230, 60, 60)
    draw.text((side_x, side_y), side_text, fill=side_color, font=font_side,
              anchor=side_cfg.get("anchor", "lm"))
    lev_cfg = layout.get("leverage_position", {})
    lev_x = int(lev_cfg.get("x", 0.15) * w)
    lev_y = int(lev_cfg.get("y", 0.335) * h)
    lev_raw = str(data.get("leverage", ""))
    lev_num = lev_raw.replace("x", "").upper()
    lev_text = f"{lev_num}X" if lev_num else ""
    if lev_text:
        draw.text((lev_x, lev_y), lev_text, fill=(255, 255, 255), font=font_side,
                  anchor=lev_cfg.get("anchor", "lm"))


def generate_custom_bybit_image(data: dict) -> str:
    pnl_raw = data["pnl"]
    try:
        pnl = float(str(pnl_raw).replace("%", "").replace(",", "."))
    except ValueError:
        pnl = 0.0
    template_side = "long" if pnl >= 0 else "short"
    template_path = os.path.join(BASE_DIR, "assets", "bybit", f"screenshot_{template_side}.png")
    output_dir = os.path.join(BASE_DIR, "images")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"custom_bybit_{uuid.uuid4().hex[:8]}.png")

    img = _load_template(template_path).copy()
    w, h = img.size
    draw = ImageDraw.Draw(img)
    cfg = FONTS["custom_bybit"]
    layout = BYBIT_CUSTOM_LAYOUT["bybit"]

    icon_path = os.path.join(BASE_DIR, "assets", "bybit", "icon.png")
    cfg_icon = layout.get("symbol_icon")
    if os.path.exists(icon_path) and cfg_icon:
        size = cfg_icon.get("size", 60)
        icon = _load_icon(icon_path, size)
        ix = int(cfg_icon["x"] * w) + cfg_icon.get("dx", 0)
        iy = int(cfg_icon["y"] * h) + cfg_icon.get("dy", 0)
        img.paste(icon, (ix, iy), icon)
        draw = ImageDraw.Draw(img)

    username_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["regular"]), cfg["sizes"]["username"])
    symbol_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["bold"]), cfg["sizes"]["symbol"])

    pnl_abs = abs(pnl)
    if pnl_abs > 99:
        pnl_size = 80
    elif pnl_abs > 49:
        pnl_size = 100
    else:
        pnl_size = cfg["sizes"]["pnl"]
    pnl_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["bold"]), pnl_size)

    entry_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["bold"]), cfg["sizes"]["entry"])
    exit_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["bold"]), cfg["sizes"]["exit"])
    lev_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["regular"]), cfg["sizes"]["leverage_text"])
    small_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["regular"]), cfg["sizes"].get("small_text", 28))

    WHITE = (255, 255, 255)
    GRAY = (150, 150, 150)

    def pos(c: dict) -> tuple[int, int]:
        return int(c["x"] * w) + c.get("dx", 0), int(c["y"] * h) + c.get("dy", 0)

    if "username" in data and "username" in layout:
        draw.text(pos(layout["username"]), data["username"], fill=WHITE, font=username_font, anchor="lm")
    if "symbol" in layout:
        draw.text(pos(layout["symbol"]), data["symbol"], fill=WHITE, font=symbol_font, anchor="lm")
    if "pnl" in layout:
        pnl_text = f"{pnl:+.2f}%"
        pnl_color = (0, 200, 120) if pnl >= 0 else (230, 60, 60)
        draw.text(pos(layout["pnl"]), pnl_text, fill=pnl_color, font=pnl_font, anchor="lm")
    if "entry" in layout:
        draw.text(pos(layout["entry"]), format_price(data["entry"]), fill=WHITE, font=entry_font, anchor="lm")
    if "exit" in layout:
        draw.text(pos(layout["exit"]), format_price(data["exit"]), fill=WHITE, font=exit_font, anchor="lm")

    if "cross_leverage" in layout:
        direction_text = "Лонг" if data["side"] == "long" else "Шорт"
        leverage_num = float(str(data["leverage"]).replace("x", ""))
        lev_text = f"{direction_text} {leverage_num:.1f}X"
        base_x = layout["cross_leverage"]["x"] * w
        symbol_len = len(data["symbol"])
        shift_x = symbol_len * 10 + 100
        lev_x = base_x + shift_x
        lev_y = layout["cross_leverage"]["y"] * h
        lev_pos = (lev_x, lev_y)
        padding_x, padding_y = 16, 10
        bbox = draw.textbbox((0, 0), lev_text, font=lev_font)
        box_w = bbox[2] - bbox[0] + padding_x * 2
        box_h = bbox[3] - bbox[1] + padding_y * 2
        x1, y1 = lev_pos[0] - box_w // 2, lev_pos[1] - box_h // 2
        x2, y2 = x1 + box_w, y1 + box_h
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle([x1, y1, x2, y2], radius=65, fill=(35, 35, 35, 100))
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)
        text_color = (0, 200, 120) if data["side"] == "long" else (230, 60, 60)
        draw.text(lev_pos, lev_text, fill=text_color, font=lev_font, anchor="mm")

    img.save(output_path)
    _cleanup_old_files(os.path.dirname(output_path), prefix="custom_bybit_")
    return output_path


def generate_custom_bingx_image(data: dict) -> str:
    pnl_raw = data["pnl"]
    try:
        pnl = float(str(pnl_raw).replace("%", "").replace(",", "."))
    except ValueError:
        pnl = 0.0
    template_side = "long" if pnl >= 0 else "short"
    template_path = os.path.join(BASE_DIR, "assets", "bingx", f"screenshot_{template_side}.png")
    output_dir = os.path.join(BASE_DIR, "images")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"custom_bingx_{uuid.uuid4().hex[:8]}.png")

    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Создай {template_path}")

    img = _load_template(template_path).copy()
    draw = ImageDraw.Draw(img)
    w, h = img.size
    cfg = FONTS["custom_bingx"]
    layout = BYBIT_CUSTOM_LAYOUT["bingx"]

    username_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["regular"]), cfg["sizes"]["username"])
    symbol_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["bold"]), cfg["sizes"]["symbol"])
    pnl_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["bold"]), cfg["sizes"]["pnl"])
    entry_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["bold"]), cfg["sizes"]["entry"])
    exit_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["bold"]), cfg["sizes"]["exit"])
    lev_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["regular"]), cfg["sizes"]["leverage_text"])
    small_font = _load_font(os.path.join(BASE_DIR, cfg["files"]["regular"]), cfg["sizes"].get("leverage_text", 36))

    draw_custom_bingx_lines(img, data, layout, small_font, symbol_font, w, h)

    WHITE = (255, 255, 255)
    GREEN = (0, 200, 120)
    RED = (230, 60, 60)
    GRAY = (150, 150, 150)

    def pos(c: dict) -> tuple[int, int]:
        return int(c["x"] * w), int(c["y"] * h)

    if "username" in data and "username" in layout:
        draw.text(pos(layout["username"]), data["username"], fill=WHITE, font=username_font)
    if "symbol" in layout:
        draw.text(pos(layout["symbol"]), data["symbol"], fill=WHITE, font=symbol_font)
    if "pnl" in layout:
        pnl_text = f"{pnl:+.2f}%"
        pnl_color = GREEN if pnl >= 0 else RED
        draw.text(pos(layout["pnl"]), pnl_text, fill=pnl_color, font=pnl_font)
    if "entry" in layout:
        draw.text(pos(layout["entry"]), format_price(data["entry"]), fill=WHITE, font=entry_font)
    if "exit" in layout:
        draw.text(pos(layout["exit"]), format_price(data["exit"]), fill=WHITE, font=exit_font)

    datetime_text = data.get("datetime_str", "").strip()
    referral_code = data.get("referral", "").strip()
    if datetime_text and "datetime" in layout:
        draw.text(pos(layout["datetime"]), datetime_text, fill=GRAY, font=small_font)
    if referral_code and "referral" in layout:
        draw.text(pos(layout["referral"]), referral_code, fill=WHITE, font=small_font)

    img.save(output_path)
    _cleanup_old_files(os.path.dirname(output_path), prefix="custom_bingx_")
    return output_path


# =====================================================
# CUSTOM EXCHANGE FSM
# =====================================================
@dp.callback_query(F.data == "custom_bybit")
async def start_custom_bybit(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(exchange="bybit")
    msg = await cb.message.answer("👤 Введите имя пользователя:")
    await state.update_data(custom_last_msg_id=msg.message_id)
    await state.set_state(CustomExchange.username)


@dp.callback_query(F.data == "custom_bingx")
async def start_custom_bingx(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(exchange="bingx")
    msg = await cb.message.answer("👤 Введите имя пользователя:")
    await state.update_data(custom_last_msg_id=msg.message_id)
    await state.set_state(CustomExchange.username)


@dp.message(CustomExchange.username)
async def custom_username(msg: Message, state: FSMContext):
    await state.update_data(username=msg.text.strip())
    await safe_delete_message(msg)
    data = await state.get_data()
    summary = build_custom_summary(data)
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception:
            pass
    new = await msg.answer(f"{summary}
📈 Выбери направление сделки:", reply_markup=side_kb)
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.side)


@dp.callback_query(CustomExchange.side)
async def custom_side(call: CallbackQuery, state: FSMContext):
    if call.data == "side_long":
        side = "long"
    elif call.data == "side_short":
        side = "short"
    else:
        await call.answer("❌ Ошибка кнопки")
        return
    await state.update_data(side=side, prev_state=CustomExchange.side)
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    data = await state.get_data()
    summary = build_custom_summary(data)
    new = await call.message.answer(f"{summary}
🪙 Торговая пара (например BTCUSDT):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.symbol)


@dp.message(CustomExchange.symbol)
async def custom_symbol(msg: Message, state: FSMContext):
    await state.update_data(symbol=msg.text.upper())
    await safe_delete_message(msg)
    data = await state.get_data()
    summary = build_custom_summary(data)
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception:
            pass
    new = await msg.answer(f"{summary}
Цена входа (через точку, например 123456.12):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.entry)


@dp.message(CustomExchange.entry)
async def custom_entry(msg: Message, state: FSMContext):
    value = await parse_float(msg)
    if value is None:
        return
    await state.update_data(entry=value)
    await safe_delete_message(msg)
    data = await state.get_data()
    summary = build_custom_summary(data)
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception:
            pass
    new = await msg.answer(f"{summary}
Цена выхода (через точку, например 123456.12):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.exit_price)


@dp.message(CustomExchange.exit_price)
async def custom_exit(msg: Message, state: FSMContext):
    value = await parse_float(msg)
    if value is None:
        return
    await state.update_data(exit=value)
    await safe_delete_message(msg)
    data = await state.get_data()
    summary = build_custom_summary(data)
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception:
            pass
    new = await msg.answer(f"{summary}
Плечо (например 20):")
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.leverage)


@dp.message(CustomExchange.leverage)
async def custom_leverage(msg: Message, state: FSMContext):
    await state.update_data(leverage=msg.text.strip())
    await safe_delete_message(msg)
    data = await state.get_data()
    summary = build_custom_summary(data)
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception:
            pass
    new = await msg.answer(
        f"{summary}
Введите реферальный код (например D1BFA4):",
        reply_markup=skip_kb,
    )
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.referral)


@dp.callback_query(CustomExchange.referral, F.data == "skip_field")
async def skip_referral(call: CallbackQuery, state: FSMContext):
    await state.update_data(referral="")
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    data = await state.get_data()
    summary = build_custom_summary(data)
    new = await call.message.answer(
        "Введите дату и время (например 14/02 19:00):", reply_markup=skip_kb
    )
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.datetime_str)


@dp.message(CustomExchange.referral)
async def custom_referral(msg: Message, state: FSMContext):
    await state.update_data(referral=msg.text.strip())
    await safe_delete_message(msg)
    data = await state.get_data()
    summary = build_custom_summary(data)
    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception:
            pass
    new = await msg.answer("Введите дату и время (например 02/14 19:00):", reply_markup=skip_kb)
    await state.update_data(custom_last_msg_id=new.message_id)
    await state.set_state(CustomExchange.datetime_str)


@dp.callback_query(CustomExchange.datetime_str, F.data == "skip_field")
async def skip_datetime(call: CallbackQuery, state: FSMContext):
    await state.update_data(datetime_str="")
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await custom_finish(call.message, state)


@dp.message(CustomExchange.datetime_str)
async def custom_finish(msg: Message, state: FSMContext):
    text_input = getattr(msg, "text", None)
    if text_input:
        await state.update_data(datetime_str=text_input.strip())
        await safe_delete_message(msg)
    data = await state.get_data()
    exchange = data.get("exchange", "bybit")
    entry = data["entry"]
    exit_price = data["exit"]
    side = data["side"]
    leverage_raw = str(data.get("leverage") or "1").strip().lower().replace("x", "")
    try:
        leverage = float(leverage_raw) if leverage_raw else 1.0
    except ValueError:
        leverage = 1.0
    if side == "long":
        pnl_percent = ((exit_price - entry) / entry * 100) * leverage
    else:
        pnl_percent = ((entry - exit_price) / entry * 100) * leverage
    leverage_formatted = f"{leverage:.1f}x"
    image_data = {
        "username": data["username"],
        "symbol": data["symbol"],
        "pnl": round(pnl_percent, 2),
        "entry": entry,
        "exit": exit_price,
        "side": side,
    }
    loop = asyncio.get_event_loop()
    if exchange == "bingx":
        image_data["leverage"] = data["leverage"]
        image_data["referral"] = data.get("referral", "")
        image_data["datetime_str"] = data.get("datetime_str", "")
        path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bingx_image, image_data)
    else:
        image_data["leverage"] = leverage_formatted
        path = await loop.run_in_executor(_THREAD_POOL, generate_custom_bybit_image, image_data)

    last_id = data.get("custom_last_msg_id")
    if last_id:
        try:
            await msg.bot.delete_message(msg.chat.id, last_id)
        except Exception:
            pass
    await msg.answer_photo(FSInputFile(path), reply_markup=restart_kb)
    await state.clear()


# =====================================================
# ЗАПУСК
# =====================================================
async def on_startup():
    await get_http_session()


async def on_shutdown():
    global _HTTP_SESSION
    if _HTTP_SESSION and not _HTTP_SESSION.closed:
        await _HTTP_SESSION.close()
    _THREAD_POOL.shutdown(wait=False)


async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
