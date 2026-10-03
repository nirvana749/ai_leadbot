"""
Прогон тестовых диалогов через _process_message без Telegram и без настоящей базы.

Запуск:  python test_dialogs.py

- База — временный файл (bot.db не трогается).
- Отправка в Telegram подменена: всё, что бот послал бы менеджеру, печатается в консоль.
- Polling НЕ запускается, поэтому работающему на Railway боту это не мешает.
- Запросы к ИИ настоящие (нужен GEMINI_API_KEY в .env).
"""

import asyncio
import os
import sys
import tempfile

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_tmp_dir = tempfile.mkdtemp(prefix="bot_test_")
os.environ["DB_PATH"] = os.path.join(_tmp_dir, "test.db")  # до импорта main/storage
os.environ["MANAGER_CHAT_ID"] = "999"                      # фейковый чат менеджера

import main  # noqa: E402
from storage import get_mode, get_profile  # noqa: E402


class FakeBot:
    """Вместо Telegram — печать в консоль."""

    async def send_message(self, chat_id, text, **kwargs):
        print(f"      [→ менеджеру {chat_id}] " + text.replace("\n", "\n" + " " * 26))

    async def send_chat_action(self, *args, **kwargs):
        pass

    async def forward_message(self, *args, **kwargs):
        pass


main.bot = FakeBot()


def _short_profile(user_id: int) -> str:
    p = get_profile(user_id)
    flags = [k for k in ("confirm_asked", "confirmed", "declined") if p[k]]
    return (f"имя={p['name']!r} телефон={p['phone']!r} услуга={p['interest']!r} "
            f"флаги={flags} режим={get_mode(user_id)}")


# Бесплатный Gemini — 15 запросов в минуту на ключ (общий с ботом на Railway!),
# а одно сообщение — до 2 запросов. Пауза, чтобы не съесть лимит у живых клиентов.
PAUSE_SECONDS = 15


async def say(user_id: int, text: str) -> list[str]:
    await asyncio.sleep(PAUSE_SECONDS)
    channel = "instagram" if user_id < 0 else "telegram"
    print(f"\n  👤 Клиент: {text}")
    replies = await main._process_message(user_id, text, channel=channel)
    for r in replies:
        print("  🤖 Бот:   " + r.replace("\n", "\n" + " " * 12))
    if not replies:
        print("  🤖 Бот:   (молчит — диалог у менеджера)")
    print(f"      [профиль] {_short_profile(user_id)}")
    return replies


results: list[tuple[str, bool]] = []


def check(title: str, ok: bool) -> None:
    results.append((title, ok))
    print(f"  {'✅' if ok else '❌'} {title}")


async def run() -> None:
    print("=" * 70 + "\n1) Клиент сразу готов заказать (Instagram, id < 0)\n" + "=" * 70)
    u = -1001
    r = await say(u, "Здравствуйте, хочу заказать пакет. Меня зовут Айгерим, 87011234567")
    p = get_profile(u)
    check("сразу карточка на подтверждение", p["confirm_asked"] and any("всё верно" in x for x in r))
    check("нет вопроса про бизнес", not any("бизнес" in x.lower() for x in r))
    check("услуга по умолчанию — пакет", p["interest"] == main.DEFAULT_INTEREST)

    print("\n" + "=" * 70 + "\n2) Исправление телефона в карточке\n" + "=" * 70)
    await say(u, "не верно, телефон 87779998877")
    p = get_profile(u)
    check("телефон обновился", p["phone"] == "+77779998877")
    check("имя и услуга не стёрлись", p["name"] == "Айгерим" and p["interest"] == main.DEFAULT_INTEREST)
    check("заявка ещё не подтверждена", not p["confirmed"])
    await say(u, "да")
    check("после «да» заявка подтверждена", get_profile(u)["confirmed"])

    print("\n" + "=" * 70 + "\n3) «а когда начнём?» на карточку — не подтверждение\n" + "=" * 70)
    u = 1002
    await say(u, "Здравствуйте, хочу заказать пакет. Меня зовут Айгерим, 87011234567")
    await say(u, "а когда начнём?")
    p = get_profile(u)
    check("не подтверждено", not p["confirmed"])
    check("данные не стёрлись", p["name"] == "Айгерим" and p["phone"] == "+77011234567")

    print("\n" + "=" * 70 + "\n4) «а менеджер у вас есть?» — бот отвечает сам\n" + "=" * 70)
    u = 1003
    await say(u, "а менеджер у вас есть?")
    check("не переключил на менеджера", get_mode(u) == "ai")

    print("\n" + "=" * 70 + "\n5) Злой клиент — зовём человека с причиной\n" + "=" * 70)
    u = 1004
    r = await say(u, "вы мне уже третий раз не отвечаете, это издевательство")
    check("переключил на менеджера", get_mode(u) == "manager" and r == [main.HANDOVER_TEXT])

    print("\n" + "=" * 70 + "\n6) Доп.: «хочу заказать» без данных — просит только имя и телефон\n" + "=" * 70)
    u = 1005
    r = await say(u, "Хочу заказать у вас рекламу, как оформить?")
    check("просит имя и телефон, без вопросов про бизнес",
          get_mode(u) == "ai" and any("телефон" in x for x in r) and not any("бизнес" in x.lower() for x in r))

    print("\n" + "=" * 70 + "\n7) Доп.: проверка целыми словами (без ИИ)\n" + "=" * 70)
    check("«когда» — не «да»", not main._is_exact("а когда начнём?", main.YES_EXACT))
    check("«Да, всё верно!» — подтверждение", main._is_exact("Да, всё верно!", main.YES_EXACT))
    check("«передумаю» не находит «думаю»", not main._has_phrase("я передумаю", ["думаю"]))
    check("«подумаю» находится", main._has_phrase("Ладно, я подумаю.", main.STALLING_WORDS))
    check("«позжесть» не находит «позже»", not main._has_phrase("позжесть", ["позже"]))
    check("«а менеджер у вас есть?» — не команда", not main._is_exact("а менеджер у вас есть?", main.MANAGER_EXACT))

    print("\n" + "=" * 70)
    passed = sum(ok for _, ok in results)
    print(f"Итого: {passed}/{len(results)} проверок прошло")
    for title, ok in results:
        if not ok:
            print(f"  ❌ {title}")


if __name__ == "__main__":
    asyncio.run(run())
