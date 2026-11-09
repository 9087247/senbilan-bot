"""Handlerlarni soxta Telegram sessiyasi va soxta Gemini bilan oxirigacha o'tkazadi."""
import os
from datetime import datetime, timezone
from io import BytesIO

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage
from aiogram.types import Chat, Message, PhotoSize, Update, User
from aiogram.types import CallbackQuery as CQ
from dotenv import load_dotenv

from nakladnoy.bot import build_dispatcher
from nakladnoy.config import Settings
from nakladnoy.db import Repo

from .test_flow import photo_response, reset

load_dotenv()
DB_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL yo'q")

OWNER = User(id=42, is_bot=False, first_name="Owner")
STRANGER = User(id=99, is_bot=False, first_name="Stranger")
CHAT = Chat(id=42, type="private")
NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, EditMessageText, SendDocument)):
            return Message(message_id=len(self.calls), date=NOW, chat=CHAT).as_(bot)
        raise AssertionError(f"Kutilmagan so'rov: {method}")


class FakeBot(Bot):
    async def download(self, file, *args, **kwargs):
        return BytesIO(b"image")


class FakeExtractor:
    def __init__(self):
        self.instructions = []
        self.response = photo_response()

    async def extract(self, image, mime_type):
        return self.response

    async def correct(self, doc, instruction):
        self.instructions.append(instruction)
        doc.items[1].qty = "5"
        doc.items[1].amount = "175000"
        doc.total = "755000"
        return doc


@pytest_asyncio.fixture
async def env():
    repo = await Repo.connect(DB_URL)
    await reset(repo)
    session = RecordingSession()
    bot = FakeBot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    settings = Settings(
        telegram_token="42:TEST", gemini_api_key="", gemini_model="", gemini_fallback_model="",
        database_url=DB_URL,
        allowed_user_ids=frozenset({42}), currency="UZS", vat_rate="Без НДС",
        department="test sklad", responsible="Masul", csv_delimiter=";",
    )
    extractor = FakeExtractor()
    dp = build_dispatcher(settings, repo, extractor)
    yield dp, bot, session, repo, extractor
    await repo.close()


_update_id = 0


async def send(dp, bot, user=OWNER, text=None, photo=False):
    global _update_id
    _update_id += 1
    message = Message(
        message_id=_update_id, date=NOW, chat=CHAT, from_user=user, text=text,
        photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)] if photo else None,
        entities=(
            [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
            if text and text.startswith("/")
            else None
        ),
    )
    await dp.feed_update(bot, Update(update_id=_update_id, message=message))


async def press(dp, bot, data, user=OWNER):
    global _update_id
    _update_id += 1
    message = Message(message_id=1, date=NOW, chat=CHAT, text="karta")
    callback = CQ(id=str(_update_id), from_user=user, chat_instance="c", data=data, message=message)
    await dp.feed_update(bot, Update(update_id=_update_id, callback_query=callback))


def last(session, method_type):
    return [c for c in session.calls if isinstance(c, method_type)][-1]


def buttons(call):
    return [b.callback_data for row in call.reply_markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_photo_to_csv(env):
    dp, bot, session, repo, _ = env

    await send(dp, bot, photo=True)
    card = last(session, EditMessageText)
    assert "Тошматов Бахтиёр" in card.text and "100000001" in card.text
    assert "Jami: 720000" in card.text
    assert "⚠️" not in card.text and "⛔" not in card.text
    assert "ok:1" in buttons(card)

    await press(dp, bot, "ed:1")
    await send(dp, bot, text="2-qator soni 5, summasi 175000")
    card = last(session, EditMessageText)
    assert "Jami: 755000" in card.text and "⚠️" not in card.text

    await press(dp, bot, "ok:1")
    assert "Tasdiqlandi" in last(session, EditMessageText).text
    await press(dp, bot, "ok:1")
    assert "ko'rib chiqilgan" in last(session, AnswerCallbackQuery).text

    await send(dp, bot, text="/navbat")
    assert "#1 03.11.2026" in last(session, SendMessage).text

    await send(dp, bot, text="/csv")
    document = last(session, SendDocument)
    assert document.caption == "1 ta nakladnoy, jami 755000"
    assert document.document.filename.endswith("_1.csv")

    await send(dp, bot, text="/csv")
    assert "Navbat bo'sh" in last(session, SendMessage).text
    await send(dp, bot, text="/qayta")
    assert len([c for c in session.calls if isinstance(c, SendDocument)]) == 2


@pytest.mark.asyncio
async def test_stranger_is_rejected(env):
    dp, bot, session, repo, _ = env
    await send(dp, bot, user=STRANGER, photo=True)
    assert "Ruxsat yo'q" in last(session, SendMessage).text
    assert "99" in last(session, SendMessage).text
    assert await repo.get(1) is None

    await send(dp, bot, photo=True)
    session.calls.clear()
    await press(dp, bot, "ok:1", user=STRANGER)
    assert (await repo.get(1)).status == "draft"


@pytest.mark.asyncio
async def test_unknown_supplier_needs_pick(env):
    dp, bot, session, repo, extractor = env
    extractor.response.supplier = "Yangi Firma"

    await send(dp, bot, photo=True)
    card = last(session, EditMessageText)
    assert "Kontragent tanlanmagan" in card.text
    assert "ok:1" not in buttons(card)

    await press(dp, bot, "ok:1")
    assert (await repo.get(1)).status == "draft"

    await send(dp, bot, text="/kontragent Yangi Firma | 123456789 | Договор № 1")
    assert "Saqlandi" in last(session, SendMessage).text
    new_cp = next(c for c in await repo.counterparties() if c.inn == "123456789")

    await press(dp, bot, f"cp:1:{new_cp.id}")
    card = last(session, EditMessageText)
    assert "123456789" in card.text and "ok:1" in buttons(card)
