import asyncio
import logging
import logging.handlers
import os
import re
import socket
import sys
import time

from dotenv import load_dotenv

load_dotenv()  # обязательно до импорта ai.py, иначе GEMINI_API_KEY будет пустым

# Защита от повторного запуска: если что-то на компьютере (антивирус, вотчер,
# повторный ручной запуск) поднимет второй процесс бота, он не должен начать
# параллельный polling с тем же токеном — это ломает диалоги (дублирующиеся
# ответы, рассинхронизированная память). Порт держим забинженным, пока жив процесс.
_singleton_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _singleton_lock.bind(("127.0.0.1", 47632))
except OSError:
    logging.basicConfig(level=logging.INFO)
    logging.error("Бот уже запущен в другом процессе — выхожу, чтобы не дублировать polling")
    sys.exit(1)

from aiohttp import web
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    BotCommandScopeChat,
)

from ai import ask_ai, ask_followup, analyze_message
from storage import (
    get_mode,
    set_mode,
    add_message,
    get_history,
    get_full_history,
    set_payment_pending,
    is_payment_pending,
    confirm_payment,
    get_profile,
    update_profile,
    is_profile_complete,
    mark_confirm_asked,
    mark_confirmed,
    mark_declined,
    set_profile_fields,
    get_stats,
    get_confirmed_leads,
)

# Ответ на карточку, который целиком (а не подстрокой) совпадает с одним из этих вариантов,
# считаем подтверждением без запроса к ИИ. Всё остальное разбирает ИИ (analyze_message)
YES_EXACT = {
    "да", "верно", "все верно", "да верно", "да все верно", "все так", "да все так",
    "правильно", "все правильно", "да правильно", "ок", "окей", "подтверждаю", "ага", "точно",
}

# Сообщение целиком из одного такого слова/фразы — явная просьба позвать человека, без ИИ
# (бот сам подсказывает клиенту написать «менеджер», и это должно работать даже при сбое ИИ).
# Остальные случаи («позовите кого-нибудь живого», злость, вопросы о скидках) определяет ИИ
MANAGER_EXACT = {
    "менеджер", "оператор", "человек", "живой человек", "позовите менеджера", "позови менеджера",
    "нужен менеджер", "хочу менеджера", "менеджера", "менеджера пожалуйста",
}

DEFAULT_INTEREST = "Пакет привлечения клиентов"

DECLINE_WORDS = (
    "не хочу", "не интересно", "неинтересно", "не нужно", "не надо",
    "передумал", "передумала", "откажусь", "отказываюсь", "не буду",
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID")  # твой личный chat_id или id группы менеджеров
KASPI_LINK = os.getenv("KASPI_LINK", os.getenv("KASPI_PHONE", "ссылка_на_оплату"))

# ManyChat дёргает этот вебхук через блок "External Request" для Instagram-диалогов.
# Токен нужен, чтобы эндпоинт не мог дёргать кто попало из интернета (он публичный)
MANYCHAT_WEBHOOK_TOKEN = os.getenv("MANYCHAT_WEBHOOK_TOKEN")
# Railway сам задаёт PORT — слушаем его; локально по-прежнему WEBHOOK_PORT/8000
WEBHOOK_PORT = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT", "8000"))
# API-ключ ManyChat (Settings → API). Если задан — отвечаем в Instagram через ManyChat API,
# а не в ответе на External Request. Это снимает таймаут ManyChat (~10 сек): ИИ может думать
# сколько нужно, а ещё бот начинает сам писать Instagram-клиентам (дожим, «подключаю специалиста»,
# подтверждение оплаты)
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY", "")
MANYCHAT_API_URL = "https://api.manychat.com/fb/sending/sendContent"

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, "bot.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        ),
    ],
)

# Ловим кандидата в номер телефона как он есть в сообщении, без нормализации формата
PHONE_CANDIDATE_PATTERN = re.compile(r"(\+?\d[\d\-\s\(\)]{7,16}\d)")


def _digits_only(text: str) -> str:
    return re.sub(r"\D", "", text)


def validate_phone(raw: str) -> str | None:
    """
    Проверяет, похож ли кандидат на настоящий казахстанский номер, а не на
    случайный набор цифр от тролля (111111111, 123456789 и т.п.).
    Возвращает номер в формате +7XXXXXXXXXX или None, если это не похоже на номер.
    """
    digits = _digits_only(raw)
    if len(digits) == 11 and digits[0] in "78":
        body = digits[1:]
    elif len(digits) == 10:
        body = digits
    else:
        return None
    if body[0] != "7":  # у казахстанских номеров код оператора всегда начинается на 7
        return None
    if len(set(body)) == 1:  # все цифры одинаковые, например 7777777777
        return None
    if body in "01234567890123" or body in "98765432109876":  # цифры подряд
        return None
    return "+7" + body


def find_phone(text: str) -> tuple[str | None, bool]:
    """
    Возвращает (валидный_номер_или_None, похоже_ли_вообще_на_попытку_ввести_номер).
    Второй флаг нужен, чтобы отличить "номера в сообщении вообще нет" от
    "клиент написал что-то похожее на номер, но это явно не он".
    """
    match = PHONE_CANDIDATE_PATTERN.search(text)
    if not match:
        return None, False
    raw = match.group(1)
    if len(_digits_only(raw)) not in (10, 11):
        return None, False
    return validate_phone(raw), True


def _normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def _has_phrase(text: str, phrases) -> bool:
    """Есть ли в тексте одна из фраз ЦЕЛЫМИ словами: «позже» не найдётся в «позжесть»,
    а «думаю» — в «передумаю»."""
    t = _normalize(text)
    return any(re.search(rf"(?<!\w){re.escape(_normalize(p))}(?!\w)", t) for p in phrases)


def _is_exact(text: str, variants: set[str]) -> bool:
    """Всё сообщение целиком (без знаков препинания) совпадает с одним из вариантов."""
    t = " ".join(re.sub(r"[^\w\s]", " ", _normalize(text)).split())
    return t in variants


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# aiogram обрабатывает апдейты в отдельных asyncio-тасках параллельно. Если один
# клиент шлёт два сообщения быстро подряд, без этого лока оба запускают handle_text
# ОДНОВРЕМЕННО: оба читают одну и ту же историю, оба параллельно дёргают Gemini
# и оба шлют ответ клиенту — привет, задвоенные/рассинхронизированные сообщения.
# Лок на user_id гарантирует, что сообщения одного клиента обрабатываются строго
# по очереди, а разные клиенты по-прежнему не блокируют друг друга.
_user_locks: dict[int, asyncio.Lock] = {}


def _get_user_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


# ---------- Мягкий дожим ----------
# Если клиент говорит "подумаю" и надолго замолкает, через пару часов шлём одно
# ненавязчивое напоминание о себе (без давления и без "только сегодня"). Как только
# клиент напишет что угодно ещё, запланированное напоминание отменяется.

STALLING_WORDS = (
    "подумаю", "подумать", "надо подумать", "дайте подумать", "думаю",
    "созвонимся", "напишу сам", "напишу позже", "напишем",
    "попозже", "позже напишу", "не сейчас", "чуть позже", "позже",
)

FOLLOWUP_DELAY_SECONDS = 3 * 60 * 60  # 3 часа — достаточно, чтобы не выглядеть навязчивым

_followup_tasks: dict[int, asyncio.Task] = {}


def _cancel_followup(user_id: int) -> None:
    task = _followup_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


async def _send_followup(user_id: int) -> None:
    try:
        await asyncio.sleep(FOLLOWUP_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    # Пока ждали, клиент мог написать сам, попасть к менеджеру или подтвердить заявку —
    # тогда напоминание уже не нужно
    if get_mode(user_id) != "ai":
        return
    profile = get_profile(user_id)
    if profile.get("confirmed"):
        return
    history = get_history(user_id)
    if not history:
        return
    try:
        reply = await ask_followup(history, profile)
    except Exception:
        logging.exception("Не удалось сгенерировать дожимное сообщение для %s", user_id)
        return
    add_message(user_id, "assistant", reply)
    await _send_to_client(user_id, reply)


def _schedule_followup(user_id: int) -> None:
    if _is_instagram(user_id) and not MANYCHAT_API_KEY:
        # Без ManyChat API написать Instagram-клиенту первым нельзя — не планируем
        return
    _cancel_followup(user_id)
    _followup_tasks[user_id] = asyncio.create_task(_send_followup(user_id))


def _is_instagram(user_id: int) -> bool:
    # Instagram-подписчики ManyChat хранятся с минусом (см. _ig_key) — это гарантирует,
    # что их id никогда не столкнётся с настоящими Telegram user_id (те всегда положительные),
    # без миграции существующей схемы/данных в bot.db
    return user_id < 0


def _channel_label(user_id: int) -> str:
    return "📷 Instagram" if _is_instagram(user_id) else "✈️ Telegram"


async def _manychat_send(subscriber_id: int, texts: list[str]) -> bool:
    """Отправка сообщений Instagram-клиенту через ManyChat API. True — если ушло."""
    if not MANYCHAT_API_KEY or not texts:
        return False
    payload = {
        "subscriber_id": abs(int(subscriber_id)),
        "data": {
            "version": "v2",
            "content": {"type": "instagram", "messages": [{"type": "text", "text": t} for t in texts]},
        },
    }
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.post(MANYCHAT_API_URL, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status != 200 or '"success"' not in body:
                    logging.error("ManyChat sendContent %s: %s", resp.status, body[:500])
                    return False
        return True
    except Exception:
        logging.exception("Не удалось отправить сообщение в ManyChat для %s", subscriber_id)
        return False


async def _send_to_client(user_id: int, text: str) -> None:
    """Проактивное сообщение клиенту. Telegram — напрямую; Instagram — через ManyChat API
    (если задан MANYCHAT_API_KEY), иначе менеджер отвечает сам в Live Chat."""
    if _is_instagram(user_id):
        if not await _manychat_send(user_id, [text]):
            logging.info(
                "Instagram-клиент %s: сообщение не отправлено, ответьте через ManyChat Live Chat: %s",
                user_id, text,
            )
        return
    try:
        await bot.send_message(user_id, text)
    except Exception:
        logging.exception("Не удалось отправить сообщение клиенту %s", user_id)


def format_profile(user_id: int, profile: dict, reason: str | None = None) -> str:
    if profile.get("confirmed"):
        status = "✅ Подтверждено"
    elif profile.get("declined"):
        status = "❌ Отказался"
    else:
        status = "🤔 Думает"
    text = (
        f"📋 Заявка от клиента {user_id} ({_channel_label(user_id)})\n"
        f"Имя: {profile.get('name') or '—'}\n"
        f"Телефон: {profile.get('phone') or '—'}\n"
        f"Услуга: {profile.get('interest') or '—'}\n"
        f"Статус: {status}"
    )
    if reason:
        text += f"\nПричина: {reason}"
    return text


def _is_manager_chat(chat_id) -> bool:
    return bool(MANAGER_CHAT_ID) and str(chat_id) == str(MANAGER_CHAT_ID)


def _is_manager(message: Message) -> bool:
    return _is_manager_chat(message.chat.id)


# ---------- Кнопки для менеджера ----------
# Инлайн-кнопки едут прямо на карточке клиента — не нужно набирать /takeover <id>
# руками. Постоянная кнопка "Статистика" снизу экрана — через reply-клавиатуру,
# она не привязана к конкретному сообщению и остаётся видимой всегда.

MANAGER_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📋 Заявки")]],
    resize_keyboard=True,
)


def _card_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🙋 Взять чат", callback_data=f"takeover:{user_id}"),
            InlineKeyboardButton(text="🔓 Вернуть ИИ", callback_data=f"release:{user_id}"),
        ],
        [InlineKeyboardButton(text="💬 История", callback_data=f"history:{user_id}")],
    ])


def _payment_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"confirm_payment:{user_id}")]
    ])


async def notify_manager_profile(user_id: int, profile: dict, reason: str | None = None) -> None:
    if MANAGER_CHAT_ID:
        await bot.send_message(
            MANAGER_CHAT_ID,
            format_profile(user_id, profile, reason),
            reply_markup=_card_keyboard(user_id),
        )


def _stats_text() -> str:
    stats = get_stats()
    return (
        "📊 Статистика заявок\n"
        f"Всего диалогов: {stats['total']}\n"
        f"✅ Подтвердили: {stats['confirmed']}\n"
        f"❌ Отказались: {stats['declined']}\n"
        f"🤔 Думают / в процессе: {stats['thinking']}"
    )


def _leads_text() -> str:
    leads = get_confirmed_leads()
    if not leads:
        return "Подтверждённых заявок пока нет."
    lines = [
        f"• {l['name']} — {l['phone']} — {l['interest']} (id {l['user_id']})"
        for l in leads
    ]
    return "📋 Подтверждённые заявки (свежие сверху):\n\n" + "\n".join(lines)


async def _send_history(chat_id, user_id: int) -> None:
    history = get_full_history(user_id)
    if not history:
        await bot.send_message(chat_id, f"Переписки с {user_id} не найдено.")
        return

    speakers = {"user": "Клиент", "assistant": "Бот"}
    lines = [f"[{m['ts']}] {speakers.get(m['role'], m['role'])}: {m['content']}" for m in history]

    header = f"💬 Переписка с {user_id}\n\n"
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await bot.send_message(chat_id, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await bot.send_message(chat_id, chunk)


async def _takeover(user_id: int) -> None:
    set_mode(user_id, "manager")
    _cancel_followup(user_id)
    handoff_note = (
        "Это клиент из Instagram — отвечай ему напрямую в Live Chat в ManyChat."
        if _is_instagram(user_id) else
        "Отвечай в личку боту, он перешлёт клиенту (см. forward_to_client)."
    )
    await bot.send_message(MANAGER_CHAT_ID, f"Бот замолчал для {user_id}. {handoff_note}")
    await bot.send_message(MANAGER_CHAT_ID, format_profile(user_id, get_profile(user_id)))
    await _send_to_client(user_id, "Секунду, вас подключает наш специалист 🙌")


async def _release(user_id: int) -> None:
    set_mode(user_id, "ai")
    await bot.send_message(MANAGER_CHAT_ID, f"ИИ снова отвечает {user_id}.")


async def _confirm_payment(user_id: int) -> None:
    confirm_payment(user_id)
    _cancel_followup(user_id)
    await bot.send_message(MANAGER_CHAT_ID, f"Оплата {user_id} подтверждена.")
    await _send_to_client(user_id, "Оплата подтверждена, спасибо! 🎉 Дальше расскажу, что происходит.")


# ---------- Команды менеджера (на случай, если удобнее текстом) ----------

@dp.message(Command("takeover"))
async def cmd_takeover(message: Message):
    """Менеджер забирает диалог себе: /takeover 123456789"""
    if not _is_manager(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: /takeover <user_id>")
        return
    await _takeover(int(parts[1]))


@dp.message(Command("release"))
async def cmd_release(message: Message):
    """Менеджер возвращает диалог ИИ: /release 123456789"""
    if not _is_manager(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: /release <user_id>")
        return
    await _release(int(parts[1]))


@dp.message(Command("confirm_payment"))
async def cmd_confirm_payment(message: Message):
    """Менеджер подтверждает оплату вручную: /confirm_payment 123456789"""
    if not _is_manager(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: /confirm_payment <user_id>")
        return
    await _confirm_payment(int(parts[1]))


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Сводка по всем клиентам: сколько подтвердили/отказались/ещё думают."""
    if not _is_manager(message):
        return
    await message.answer(_stats_text())


@dp.message(F.text == "📊 Статистика")
async def btn_stats(message: Message):
    if not _is_manager(message):
        return
    await message.answer(_stats_text())


@dp.message(Command("leads"))
async def cmd_leads(message: Message):
    """Список подтверждённых заявок с контактами."""
    if not _is_manager(message):
        return
    await message.answer(_leads_text())


@dp.message(F.text == "📋 Заявки")
async def btn_leads(message: Message):
    if not _is_manager(message):
        return
    await message.answer(_leads_text())


@dp.message(Command("history"))
async def cmd_history(message: Message):
    """Полная переписка с клиентом: /history 123456789"""
    if not _is_manager(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: /history <user_id>")
        return
    await _send_history(message.chat.id, int(parts[1]))


@dp.callback_query(F.data.startswith("takeover:"))
async def cb_takeover(callback: CallbackQuery):
    if not _is_manager_chat(callback.message.chat.id):
        await callback.answer()
        return
    user_id = int(callback.data.split(":", 1)[1])
    await _takeover(user_id)
    await callback.answer("Чат взят")


@dp.callback_query(F.data.startswith("release:"))
async def cb_release(callback: CallbackQuery):
    if not _is_manager_chat(callback.message.chat.id):
        await callback.answer()
        return
    user_id = int(callback.data.split(":", 1)[1])
    await _release(user_id)
    await callback.answer("ИИ снова отвечает")


@dp.callback_query(F.data.startswith("history:"))
async def cb_history(callback: CallbackQuery):
    if not _is_manager_chat(callback.message.chat.id):
        await callback.answer()
        return
    user_id = int(callback.data.split(":", 1)[1])
    await _send_history(callback.message.chat.id, user_id)
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_payment:"))
async def cb_confirm_payment(callback: CallbackQuery):
    if not _is_manager_chat(callback.message.chat.id):
        await callback.answer()
        return
    user_id = int(callback.data.split(":", 1)[1])
    await _confirm_payment(user_id)
    await callback.answer("Оплата подтверждена")


# ---------- Клиентские сообщения ----------

@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    if _is_manager(message):
        await message.answer(
            "Привет! Это панель управления ботом.\n\n"
            "Кнопка «📊 Статистика» всегда снизу. На карточке клиента — кнопки «Взять чат», "
            "«Вернуть ИИ» и «История» (не нужно набирать команды вручную).",
            reply_markup=MANAGER_MENU,
        )
        return
    set_mode(message.from_user.id, "ai")
    await message.answer(
        "Привет! Я Эдуард, digital-маркетолог, а это мой личный ИИ-ассистент — "
        "он на связи 24/7 и ответит на любые вопросы. Если понадобится живой человек — просто напиши об этом.\n\n"
        "Чем я могу вам помочь?"
    )


@dp.message(F.text.lower().in_({"оплатить", "оплата", "как оплатить"}))
async def request_payment(message: Message):
    user_id = message.from_user.id
    set_payment_pending(user_id)
    await message.answer(
        f"Для оплаты перейди по ссылке:\n{KASPI_LINK}\n\n"
        "После оплаты пришли сюда скриншот чека — менеджер подтвердит."
    )


@dp.message(F.photo)
async def receive_payment_screenshot(message: Message):
    user_id = message.from_user.id
    try:
        if is_payment_pending(user_id):
            await message.answer("Скриншот получен, жду подтверждения от менеджера 🙏")
            if MANAGER_CHAT_ID:
                await bot.forward_message(MANAGER_CHAT_ID, message.chat.id, message.message_id)
                await bot.send_message(
                    MANAGER_CHAT_ID,
                    f"Чек от {user_id}.",
                    reply_markup=_payment_keyboard(user_id),
                )
        else:
            await message.answer("Фото получил, но оплату не ждал — если это чек, напиши 'оплата'.")
    except Exception:
        await _reply_with_fallback(message, user_id)


async def _reply_with_fallback(message: Message, user_id: int) -> None:
    # Последняя линия обороны: что бы ни случилось внутри хендлера (не только сбой
    # Gemini, а вообще любое необработанное исключение), клиент не должен остаться
    # без ответа и решить, что бот завис. aiogram сам по себе только залогирует
    # исключение из хендлера и промолчит клиенту — этого недостаточно.
    logging.exception("Необработанная ошибка при обработке сообщения от %s", user_id)
    try:
        await message.answer(
            "Секунду, у меня техническая заминка — уже разбираюсь. "
            "Если срочно, напишите «менеджер» 🙏"
        )
    except Exception:
        logging.exception("Не удалось отправить сообщение о заминке %s", user_id)


CONFIRM_PROMPT_TEMPLATE = (
    "Проверьте, пожалуйста, я всё верно записал?\n\n"
    "Имя: {name}\n"
    "Телефон: {phone}\n"
    "Услуга: {interest}\n\n"
    "Если всё верно — напишите «да»."
)

CONFIRM_REMINDER = "И гляньте, пожалуйста, данные для заявки выше: если всё верно — напишите «да», если нет — просто поправьте."

HANDOVER_TEXT = "Подключаю живого специалиста, минутку 🙌"

TECH_ISSUE_TEXT = "Секунду, у меня техническая заминка — уже разбираюсь. Если срочно, напишите «менеджер» 🙏"

PHONE_TYPO_TEXT = "Кажется, в номере опечатка — пришлите, пожалуйста, в формате +7 7XX XXX XX XX 🙏"


def _confirm_card(profile: dict) -> str:
    return CONFIRM_PROMPT_TEMPLATE.format(**profile)


def _missing_fields_text(profile: dict) -> str:
    """Быстрый путь: клиент готов заказать — просим одним сообщением только то, чего не хватает."""
    missing = []
    if not profile.get("name"):
        missing.append("как вас зовут")
    if not profile.get("phone"):
        missing.append("номер телефона")
    return (
        f"Отлично, оформим заявку на «{profile.get('interest') or DEFAULT_INTEREST}» 🙌 "
        f"Напишите, пожалуйста, {' и '.join(missing)}."
    )


async def _handover_to_manager(user_id: int, reason: str) -> list[str]:
    set_mode(user_id, "manager")
    _cancel_followup(user_id)
    if MANAGER_CHAT_ID:
        await bot.send_message(MANAGER_CHAT_ID, f"🙋 Клиенту {_channel_label(user_id)} {user_id} нужен менеджер.")
        await notify_manager_profile(user_id, get_profile(user_id), reason=reason)
    return [HANDOVER_TEXT]


async def _confirm_lead(user_id: int) -> list[str]:
    mark_confirmed(user_id)
    _cancel_followup(user_id)
    await notify_manager_profile(user_id, get_profile(user_id))
    return ["Отлично, спасибо! Передал заявку менеджеру, он свяжется с вами в ближайшее время 🙌"]


def _apply_corrections(user_id: int, phone: str | None, looked_like_phone: bool, corrections: dict) -> list[str]:
    """Клиент поправил данные в карточке — обновляем только то, что он исправил, и показываем карточку снова."""
    fields = {k: v for k, v in corrections.items() if v}
    if phone:
        fields["phone"] = phone
    elif looked_like_phone:
        return [PHONE_TYPO_TEXT]
    if not fields:
        return ["Подскажите, пожалуйста, что именно исправить — просто напишите правильные данные."]
    set_profile_fields(user_id, fields)
    return ["Исправил 👍\n\n" + _confirm_card(get_profile(user_id))]


async def _process_message(user_id: int, text: str, *, channel: str) -> list[str]:
    """
    Обрабатывает одно входящее текстовое сообщение клиента независимо от канала
    (Telegram или Instagram через ManyChat) и возвращает список сообщений для
    отправки клиенту по порядку (может быть пустым — в ручном режиме отвечает
    менеджер, а не бот).
    """
    if channel == "telegram":
        # Клиент написал сам — значит, дожимное напоминание, если оно было
        # запланировано с прошлого раза, больше не нужно
        _cancel_followup(user_id)

    mode = get_mode(user_id)

    if mode == "manager":
        # В ручном режиме бот молчит, но пересылает менеджеру, если тот не в диалоге
        if MANAGER_CHAT_ID:
            await bot.send_message(MANAGER_CHAT_ID, f"[{_channel_label(user_id)} {user_id}] {text}")
        # Телефон продолжаем ловить и в ручном режиме, чтобы данные не терялись
        # (карточку менеджеру не шлём повторно — он уже в диалоге с клиентом)
        phone, _ = find_phone(text)
        if phone:
            update_profile(user_id, {"phone": phone})
        return []

    add_message(user_id, "user", text)
    replies = await _reply_to_client(user_id, text, channel=channel)
    # Пишем в историю всё, что ушло клиенту (карточки, «подключаю специалиста» и т.д.), —
    # иначе ИИ и менеджер в /history не видят, что бот уже показал карточку или попросил телефон
    for reply in replies:
        add_message(user_id, "assistant", reply)
    return replies


async def _reply_to_client(user_id: int, text: str, *, channel: str) -> list[str]:
    # Телефон ловим как есть, без ИИ — просто по паттерну цифр в сообщении,
    # но проверяем, что это похоже на настоящий номер, а не на цифры от тролля
    # Карточку менеджеру пока не шлём — только когда клиент подтвердит все данные разом
    phone, looked_like_phone = find_phone(text)
    if phone:
        update_profile(user_id, {"phone": phone})
    elif looked_like_phone and not get_profile(user_id).get("phone"):
        return [PHONE_TYPO_TEXT]

    # Сообщение целиком — «менеджер»: зовём человека сразу, без ИИ
    if _is_exact(text, MANAGER_EXACT):
        return await _handover_to_manager(user_id, "Клиент попросил менеджера")

    profile = get_profile(user_id)
    awaiting_confirmation = profile.get("confirm_asked") and not profile.get("confirmed")

    # Простое «да» на карточку подтверждаем без лишнего запроса к ИИ
    if awaiting_confirmation and _is_exact(text, YES_EXACT):
        return await _confirm_lead(user_id)

    # Если телефон только что стал последним недостающим полем — профиль уже полный.
    # Не гоняем его через обычный ответ ИИ (он не знает про подтверждение и может
    # преждевременно попрощаться, как будто заявка уже отправлена) — сразу спрашиваем "всё верно?"
    if is_profile_complete(profile) and not profile.get("confirmed") and not profile.get("confirm_asked"):
        mark_confirm_asked(user_id)
        return [_confirm_card(profile)]

    history = get_history(user_id)
    if channel == "telegram":
        # "Печатает..." — чтобы клиент видел, что бот уже работает над ответом, а не завис
        await bot.send_chat_action(user_id, "typing")

    # Ответ клиенту и разбор сообщения запускаем параллельно — так ответ не ждёт вдвое дольше.
    # Сначала дожидаемся разбора: если он решит, что ответ ИИ не нужен (зовём человека,
    # клиент подтвердил карточку, быстрый путь к заявке) — ответ просто отменяем.
    # В ask_ai передаём уже известный профиль, чтобы бот не переспрашивал то, что клиент уже сказал.
    reply_task = asyncio.create_task(ask_ai(history, profile))

    async def get_reply() -> str | None:
        try:
            return await reply_task
        except Exception:
            # ИИ недоступен (сеть/квота/сбой) — вызывающий код даст клиенту понятную реакцию
            logging.exception("Не удалось получить ответ ИИ для %s (%s)", user_id, channel)
            return None

    try:
        analysis = await analyze_message(history, profile, awaiting_confirmation=awaiting_confirmation)
    except Exception:
        # Разбор не удался — просто отвечаем как обычно, ничего не теряя из данных клиента
        logging.exception("Не удалось разобрать сообщение %s (%s)", user_id, channel)
        analysis = None

    if analysis is None:
        if awaiting_confirmation and phone and phone != profile.get("phone"):
            # ИИ недоступен, но клиент прислал на карточку новый номер — это точно исправление
            reply_task.cancel()
            return _apply_corrections(user_id, phone, looked_like_phone, {})
        reply = await get_reply()
        return [reply] if reply else [TECH_ISSUE_TEXT]

    update_profile(user_id, {"name": analysis["name"], "interest": analysis["interest"]})

    if analysis["needs_human"]:
        reply_task.cancel()
        return await _handover_to_manager(user_id, analysis["human_reason"])

    if awaiting_confirmation:
        action = analysis["confirmation"]
        if action == "confirm":
            reply_task.cancel()
            return await _confirm_lead(user_id)
        if action == "correct":
            reply_task.cancel()
            return _apply_corrections(user_id, phone, looked_like_phone, analysis["corrections"])
        reply = await get_reply()
        if action == "decline":
            # Данные не стираем: если клиент передумает обратно, достаточно будет сказать «да»
            mark_declined(user_id)
            return [reply] if reply else [TECH_ISSUE_TEXT]
        # Клиент спросил о другом — отвечаем и напоминаем про карточку (если ИИ сам уже
        # не попросил написать «да»), данные не трогаем
        if not reply:
            return [TECH_ISSUE_TEXT]
        return [reply] if _has_phrase(reply, ["да"]) else [reply, CONFIRM_REMINDER]

    ready = analysis["ready_to_order"]
    profile = get_profile(user_id)
    if not profile.get("confirmed") and not profile.get("interest") and (
        ready or (profile.get("name") and profile.get("phone"))
    ):
        # Клиент готов заказать (или уже оставил контакты), но услугу не назвал — значит, пакет
        update_profile(user_id, {"interest": DEFAULT_INTEREST})
        profile = get_profile(user_id)

    # Как только собраны имя, телефон и услуга — переспрашиваем клиента, всё ли верно
    if is_profile_complete(profile) and not profile.get("confirmed") and not profile.get("confirm_asked"):
        mark_confirm_asked(user_id)
        card = _confirm_card(profile)
        if ready and not analysis["asks_question"]:
            reply_task.cancel()
            return ["Отлично, оформляю заявку 🙌\n\n" + card]
        reply = await get_reply()
        return ([reply] if reply else []) + [card]

    # Быстрый путь: клиент готов заказать, а данных не хватает — не продаём дальше
    # и не спрашиваем про бизнес, просим одним сообщением только недостающее
    if ready and not profile.get("confirmed"):
        ask = _missing_fields_text(profile)
        if not analysis["asks_question"]:
            reply_task.cancel()
            return [ask]
        reply = await get_reply()
        if not reply:
            return [ask]
        # ИИ, отвечая на вопрос, мог сам уже попросить контакты — тогда не дублируем просьбу
        already_asked = (not profile.get("phone") and _has_phrase(reply, ["телефон", "телефона", "номер"])) or (
            profile.get("phone") and _has_phrase(reply, ["зовут", "имя"])
        )
        return [reply] if already_asked else [reply, ask]

    reply = await get_reply()
    if not reply:
        return [TECH_ISSUE_TEXT]

    # Клиент явно взял паузу подумать — планируем одно мягкое напоминание через пару часов,
    # если он сам не напишет раньше (тогда _cancel_followup выше его отменит)
    if _has_phrase(text, STALLING_WORDS):
        _schedule_followup(user_id)

    # Явный отказ — помечаем статус для карточки/статистики, но не обрываем диалог:
    # ИИ сам мягко отработает возражение по своим правилам (см. системный промпт)
    if _has_phrase(text, DECLINE_WORDS):
        mark_declined(user_id)

    return [reply]


@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    try:
        async with _get_user_lock(user_id):
            replies = await _process_message(user_id, message.text, channel="telegram")
        for chunk in replies:
            await message.answer(chunk)
    except Exception:
        await _reply_with_fallback(message, user_id)


def _ig_key(subscriber_id) -> int:
    # Instagram-подписчики ManyChat всегда положительные — храним с минусом, чтобы
    # гарантированно не столкнуться с настоящими Telegram user_id (тоже всегда
    # положительные), не трогая существующую схему и данные в bot.db
    return -abs(int(subscriber_id))


async def manychat_webhook(request: web.Request) -> web.Response:
    """
    Эндпоинт для блока "External Request" в ManyChat: на каждое сообщение клиента
    в Instagram ManyChat шлёт сюда POST с subscriber_id и текстом, мы прогоняем его
    через ту же логику, что и Telegram, и возвращаем ответ в формате ManyChat v2,
    чтобы он сразу ушёл клиенту в Instagram.

    Настройка в ManyChat (External Request):
    - Method: POST, URL: https://<ваш-домен-или-ngrok>/manychat/webhook
    - Header: X-Webhook-Token: <значение MANYCHAT_WEBHOOK_TOKEN из .env>
    - Body (JSON): {"subscriber_id": "{{subscriber_id}}", "text": "{{last_input_text}}"}
    - Response type: оставить как есть — тело ответа уже в формате Dynamic Block (v2)
    """
    if MANYCHAT_WEBHOOK_TOKEN:
        token = request.headers.get("X-Webhook-Token") or request.query.get("token")
        if token != MANYCHAT_WEBHOOK_TOKEN:
            return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json body"}, status=400)

    subscriber_id = data.get("subscriber_id") or data.get("user_id") or data.get("id")
    text = str(data.get("text") or data.get("last_input_text") or "").strip()

    if not subscriber_id or not text:
        return web.json_response({"error": "subscriber_id and text are required"}, status=400)

    try:
        user_id = _ig_key(subscriber_id)
    except (TypeError, ValueError):
        return web.json_response({"error": "subscriber_id must be numeric"}, status=400)

    if MANYCHAT_API_KEY:
        # Быстрый режим: сразу отвечаем ManyChat «принято», а ответ ИИ отправляем
        # следом через ManyChat API — так не упираемся в таймаут External Request
        asyncio.create_task(_process_instagram_async(user_id, subscriber_id, text))
        return web.json_response({"version": "v2", "content": {"type": "instagram", "messages": []}})

    try:
        async with _get_user_lock(user_id):
            replies = await _process_message(user_id, text, channel="instagram")
    except Exception:
        logging.exception("Необработанная ошибка в ManyChat-вебхуке для %s", subscriber_id)
        replies = ["Секунду, у меня техническая заминка — уже разбираюсь 🙏"]

    return web.json_response({
        "version": "v2",
        "content": {"type": "instagram", "messages": [{"type": "text", "text": r} for r in replies]},
    })


async def _process_instagram_async(user_id: int, subscriber_id, text: str) -> None:
    try:
        async with _get_user_lock(user_id):
            replies = await _process_message(user_id, text, channel="instagram")
    except Exception:
        logging.exception("Необработанная ошибка при обработке Instagram-сообщения %s", subscriber_id)
        replies = ["Секунду, у меня техническая заминка — уже разбираюсь 🙏"]
    if replies:
        await _manychat_send(user_id, replies)


# Ловит всё, что не подошло под текст/фото выше (голосовые, стикеры, видео, файлы),
# чтобы клиент не решил, что бот завис — раньше такие сообщения просто проглатывались
@dp.message()
async def handle_other(message: Message):
    user_id = message.from_user.id
    if get_mode(user_id) == "manager":
        if MANAGER_CHAT_ID:
            await bot.forward_message(MANAGER_CHAT_ID, message.chat.id, message.message_id)
        return
    await message.answer(
        "Пока понимаю только текстовые сообщения (и скриншот чека при оплате) — "
        "напишите, пожалуйста, словами 🙂"
    )


MANAGER_COMMANDS = [
    BotCommand(command="stats", description="Статистика заявок"),
    BotCommand(command="leads", description="Подтверждённые заявки с контактами"),
    BotCommand(command="history", description="Переписка с клиентом: /history <id>"),
    BotCommand(command="takeover", description="Забрать диалог себе: /takeover <id>"),
    BotCommand(command="release", description="Вернуть диалог ИИ: /release <id>"),
    BotCommand(command="confirm_payment", description="Подтвердить оплату: /confirm_payment <id>"),
]


async def _health(request: web.Request) -> web.Response:
    # Открой адрес сервера в браузере — если видишь {"ok": true}, бот запущен
    return web.json_response({"ok": True, "manychat_api": bool(MANYCHAT_API_KEY)})


async def main():
    if MANAGER_CHAT_ID:
        try:
            await bot.set_my_commands(MANAGER_COMMANDS, scope=BotCommandScopeChat(chat_id=int(MANAGER_CHAT_ID)))
        except Exception:
            logging.exception("Не удалось выставить меню команд для менеджера")
    try:
        await bot.set_my_short_description(
            short_description="Digital-маркетолог Эдуард. Личный ИИ-ассистент 24/7: консультирует "
            "и оформляет заявки на ИИ-видео, таргет и чат-ботов."
        )
        await bot.set_my_description(
            description="Здесь отвечает мой личный ИИ-ассистент — он на связи круглосуточно, "
            "расскажет про пакет привлечения клиентов (ИИ-видео + таргет + чат-бот) и оформит заявку. "
            "В любой момент можно написать «менеджер», чтобы подключился живой человек."
        )
    except Exception:
        logging.exception("Не удалось обновить описание бота")

    app = web.Application()
    app.router.add_post("/manychat/webhook", manychat_webhook)
    app.router.add_get("/", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    logging.info("ManyChat-вебхук слушает на 0.0.0.0:%s/manychat/webhook", WEBHOOK_PORT)

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logging.exception("Бот упал с необработанной ошибкой — перезапускаю через 5 секунд")
            time.sleep(5)
            continue
        else:
            break
