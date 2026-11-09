"""Kontragentlarni Excel'dan import qilish."""
from io import BytesIO
from unittest.mock import AsyncMock

import openpyxl
import pytest
import xlwt
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Document, Message, Update

from nakladnoy import matching
from nakladnoy.bot import confirm_blockers, render_card
from nakladnoy.counterparty_import import (
    ImportFormatError,
    ImportRow,
    is_real_inn,
    parse_workbook,
)
from nakladnoy.csv_export import COLUMNS, build_csv
from nakladnoy.extraction import normalize

from .test_bot_smoke import CHAT, NOW, OWNER, STRANGER, buttons, env, last, press, send  # noqa: F401
from .test_flow import DB_URL, SETTINGS, photo_response

HEADER = ["Контрагент", "ИНН", "Договор", "Комментарий"]
ROWS = [
    ["Отчёт по контрагентам"],
    [],
    HEADER,
    ["Yangi Firma MCHJ", 200000001.0, "Договор № 5", "izoh"],
    ["Ikkinchi Firma", "200 000 002", None, None],
    ["INNsiz Firma", None, None, None],
    ["Soxta bir", "888888888", None, None],
    ["Soxta ikki", "888888888", None, None],
    ["12345", "200000005", None, None],
    [None, "200000003", None, None],
    ["Takror", "000000004", "Договор № 7", "eski"],
    ["Takror", "400000001", None, None],
    [None, None, None, None],
]


def xlsx(rows) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def xls(rows) -> bytes:
    book = xlwt.Workbook()
    sheet = book.add_sheet("Лист1")
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if value is not None:
                sheet.write(r, c, value)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize("make", [xlsx, xls])
def test_parse_both_formats(make):
    result = parse_workbook(make(ROWS))
    by_name = {r.name: r for r in result.rows}
    assert set(by_name) == {
        "Yangi Firma MCHJ", "Ikkinchi Firma", "INNsiz Firma", "Soxta bir", "Soxta ikki", "Takror",
    }
    assert by_name["Yangi Firma MCHJ"] == ImportRow(
        "Yangi Firma MCHJ", "200000001", "Договор № 5", "izoh"
    )
    assert by_name["Ikkinchi Firma"].inn == "200000002"
    assert by_name["INNsiz Firma"].inn is None
    assert by_name["Soxta bir"].inn == by_name["Soxta ikki"].inn == "888888888"
    assert by_name["Takror"] == ImportRow("Takror", "400000001", "Договор № 7", "eski")
    assert result.duplicates == 1
    assert [n for n, _ in result.skipped] == [9, 10]


def test_real_inn_survives_later_fake_duplicate():
    data = xlsx([["Наименование", "ИНН"], ["Firma", "400000001"], ["Firma", "888888888"], ["Firma", None]])
    assert parse_workbook(data).rows[0].inn == "400000001"


@pytest.mark.parametrize(
    "inn,real",
    [
        ("400000001", True), ("30506199000012", True), ("111111111", False), ("888", False),
        ("123456789", False), ("000000004", False), ("20918КАА", False), ("", False), (None, False),
    ],
)
def test_is_real_inn(inn, real):
    assert is_real_inn(inn) is real


def _cp(i, name, contract):
    return matching.Counterparty(i, name, "300000099", contract, None)


def test_tie_between_1c_duplicates_prefers_the_one_with_contract():
    plain, with_contract = _cp(1, '"ACME" MCHJ', None), _cp(2, "ACME MCHJ (материал)", "Договор № 1")
    assert matching.auto_match(matching.rank("ACME", [plain, with_contract])) is with_contract


def test_tie_stays_manual_when_contract_does_not_decide():
    no_contracts = [_cp(1, '"ACME" MCHJ', None), _cp(2, "ACME MCHJ (материал)", None)]
    both = [_cp(1, '"ACME" MCHJ', "D1"), _cp(2, "ACME MCHJ (материал)", "D2")]
    assert matching.auto_match(matching.rank("ACME", no_contracts)) is None
    assert matching.auto_match(matching.rank("ACME", both)) is None


def test_columns_found_in_any_order_with_1c_names():
    data = xlsx(
        [
            ["Договор контрагента", "Длинное название", "Наименование", "ИНН контрагента"],
            ["Договор № 1", "uzun nom", "Firma", 300000001],
        ]
    )
    assert parse_workbook(data).rows == [ImportRow("Firma", "300000001", "Договор № 1", None)]


def test_1c_export_layout_uses_short_name():
    data = xlsx(
        [
            ["ИНН", "ПИНФЛ", "Физ лицо", "Наименование", "Наименование полное"],
            [None, None, "Нет", "Qisqa nom", "Uzun rasmiy nom"],
        ]
    )
    assert parse_workbook(data).rows == [ImportRow("Qisqa nom", None, None, None)]


def test_header_on_later_sheet():
    book = openpyxl.Workbook()
    book.active.append(["bo'sh varaq"])
    sheet = book.create_sheet("Asosiy")
    sheet.append(["Наименование", "ИНН"])
    sheet.append(["Firma", 300000002])
    buffer = BytesIO()
    book.save(buffer)
    assert parse_workbook(buffer.getvalue()).rows[0].inn == "300000002"


def test_no_header_is_reported():
    with pytest.raises(ImportFormatError, match="ustunlari topilmadi"):
        parse_workbook(xlsx([["a", "b"], [1, 2]]))


@pytest.mark.parametrize("data", [b"not excel", b"PK broken zip", b"\xd0\xcf\x11\xe0broken"])
def test_bad_file_is_reported(data):
    with pytest.raises(ImportFormatError):
        parse_workbook(data)


async def send_file(dp, bot, data, name="kontragentlar.xlsx", user=OWNER):
    bot.download = AsyncMock(return_value=BytesIO(data))
    message = Message(
        message_id=1000, date=NOW, chat=CHAT, from_user=user,
        document=Document(file_id="x", file_unique_id="y", file_name=name),
    )
    await dp.feed_update(bot, Update(update_id=1000, message=message))


needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL yo'q")


@needs_db
@pytest.mark.asyncio
async def test_import_writes_only_after_confirm(env):  # noqa: F811
    dp, bot, session, repo, _ = env
    before = len(await repo.counterparties())

    await send(dp, bot, text="/admin")
    panel = last(session, SendMessage)
    assert f"Kontragentlar: {before}" in panel.text
    assert buttons(panel) == ["adm:import", "adm:list"]

    await send_file(dp, bot, xlsx(ROWS))
    preview = last(session, EditMessageText)
    assert "Yangi: 6" in preview.text
    assert "O'tkazib yuborildi: 2" in preview.text
    assert "ИНН yo'q yoki soxta: 3" in preview.text
    assert buttons(preview) == ["imp:ok", "imp:no"]
    assert len(await repo.counterparties()) == before

    await press(dp, bot, "imp:ok")
    assert "6 ta kontragent" in last(session, EditMessageText).text
    assert len(await repo.counterparties()) == before + 6

    await press(dp, bot, "imp:ok")
    assert len(await repo.counterparties()) == before + 6


@needs_db
@pytest.mark.asyncio
async def test_import_keeps_existing_values_and_can_be_cancelled(env):  # noqa: F811
    dp, bot, session, repo, _ = env
    name = "Тошматов Бахтиёр  (снабженец)"
    await send_file(dp, bot, xlsx([["Наименование", "ИНН", "Договор"], [name, None, None]]))
    assert "Yangilanadi: 1" in last(session, EditMessageText).text
    await press(dp, bot, "imp:ok")
    cp = next(c for c in await repo.counterparties() if c.name == name)
    assert cp.inn == "100000001" and cp.contract == "Договор № 1 от 01.01.2026г."

    await send_file(dp, bot, xlsx([["Наименование", "ИНН"], ["Bekor", 300000009]]))
    await press(dp, bot, "imp:no")
    assert all(c.name != "Bekor" for c in await repo.counterparties())


@needs_db
@pytest.mark.asyncio
async def test_stranger_cannot_import(env):  # noqa: F811
    dp, bot, session, repo, _ = env
    before = len(await repo.counterparties())
    await send_file(dp, bot, xlsx(ROWS), user=STRANGER)
    await press(dp, bot, "imp:ok", user=STRANGER)
    assert "Ruxsat yo'q" in last(session, SendMessage).text
    assert len(await repo.counterparties()) == before


@needs_db
@pytest.mark.asyncio
async def test_counterparty_without_inn_reaches_csv(env):  # noqa: F811
    _, _, _, repo, _ = env
    await repo.import_counterparties([ImportRow("INNsiz Firma", None, "Договор № 9", None)])
    cp = next(c for c in await repo.counterparties() if c.name == "INNsiz Firma")
    doc_id = await repo.create_draft(normalize(photo_response()), cp.id, 42, None)
    stored = await repo.get(doc_id)
    assert confirm_blockers(stored) == []
    assert "ИНН: yo'q" in render_card(stored, [])
    assert await repo.set_status(doc_id, "draft", "confirmed")
    _, docs = await repo.create_export(42)
    rows = build_csv(docs, SETTINGS).decode("utf-8-sig").splitlines()
    assert rows[1].split(";")[COLUMNS.index("ИНН")] == ""


@needs_db
@pytest.mark.asyncio
async def test_manual_add_does_not_touch_shared_fake_inn(env):  # noqa: F811
    _, _, _, repo, _ = env
    await repo.import_counterparties(
        [ImportRow("Soxta bir", "888888888", "D1", None), ImportRow("Soxta ikki", "888888888", "D2", None)]
    )
    await repo.upsert_counterparty("Uchinchi", "888888888", "D3", None)
    by_name = {c.name: c.contract for c in await repo.counterparties()}
    assert (by_name["Soxta bir"], by_name["Soxta ikki"], by_name["Uchinchi"]) == ("D1", "D2", "D3")

    await repo.upsert_counterparty("Yangi nom", "100000002", "D4", None)
    names = {c.inn: c.name for c in await repo.counterparties() if c.inn == "100000002"}
    assert names == {"100000002": "Yangi nom"}


@needs_db
@pytest.mark.asyncio
async def test_list_is_capped_and_searchable(env):  # noqa: F811
    dp, bot, session, repo, _ = env
    await repo.import_counterparties(
        [ImportRow(f"Firma raqam {i:03}", None, None, None) for i in range(100)]
        + [ImportRow("Zarafshon Savdo", None, None, None)]
    )
    await send(dp, bot, text="/kontragentlar")
    text = last(session, SendMessage).text
    assert "Jami 105 ta" in text and text.count("\n   ") == 30

    await send(dp, bot, text="/kontragentlar Зарафшон савдо")
    text = last(session, SendMessage).text
    assert text.startswith("Zarafshon Savdo")
