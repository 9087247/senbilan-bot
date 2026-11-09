"""Namunaviy nakladnoy (720 000) asosidagi testlar.

Gemini javobi qo'lda yozilgan: bu testlar OCR sifatini emas, undan keyingi zanjirni tekshiradi.
DB testlari TEST_DATABASE_URL bo'lsa ishlaydi.
"""
import csv
import io
import os
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from dotenv import load_dotenv

from nakladnoy import matching
from nakladnoy.bot import confirm_blockers, render_card
from nakladnoy.config import Settings
from nakladnoy.csv_export import COLUMNS, build_csv
from nakladnoy.db import SCHEMA, Repo
from nakladnoy.extraction import (
    ExtractedDoc,
    ExtractedItem,
    blocking_errors,
    check_warnings,
    normalize,
    parse_date,
    parse_number,
    to_extracted,
)

TODAY = date(2026, 11, 4)
SETTINGS = Settings(
    telegram_token="", gemini_api_key="", gemini_model="", gemini_fallback_model="", database_url="",
    allowed_user_ids=frozenset(), currency="UZS", vat_rate="Без НДС",
    department="test sklad", responsible="Masul", csv_delimiter=";",
)


def photo_response() -> ExtractedDoc:
    return ExtractedDoc(
        is_nakladnaya=True,
        doc_date="03.11.2026",
        supplier="Тошматов Бахтиёр",
        receiver="Test Qabul",
        items=[
            ExtractedItem(name="А4 лист", unit="блок", qty="2", price="250.000", amount="500.000"),
            ExtractedItem(name="Офис дафтари", unit="шт", qty="4", price="35000", amount="140.000"),
            ExtractedItem(name="Офис папка", unit="шт", qty="2", price="40.000", amount="80000"),
        ],
        total="720.000",
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("250.000", "250000"), ("250,000", "250000"), ("35000", "35000"), ("1 250 000", "1250000"),
        ("1.250.000", "1250000"), ("2,5", "2.5"), ("2.5", "2.5"), ("0.125", "0.125"),
        ("1.234,50", "1234.50"), ("720.000 сум", "720000"), ("720.000.", "720000"),
    ],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", None, "abc", "12a", "-"])
def test_parse_number_rejects(raw):
    assert parse_number(raw) is None


def test_parse_date_is_day_first():
    assert parse_date("3-11-2026") == date(2026, 11, 3)
    assert parse_date("03.11.2026") == date(2026, 11, 3)
    assert parse_date("03.11.26") == date(2026, 11, 3)
    assert parse_date("2026-11-03") == date(2026, 11, 3)
    assert parse_date("31.02.2026") is None
    assert parse_date("3-11") is None


def test_photo_is_clean():
    doc = normalize(photo_response())
    assert blocking_errors(doc) == []
    assert check_warnings(doc, TODAY) == []
    assert doc.total == Decimal(720000)


def test_arithmetic_mismatch_warns():
    data = photo_response()
    data.items[2].amount = "10000"
    warnings = check_warnings(normalize(data), TODAY)
    assert any(w.startswith("3-qator") for w in warnings)
    assert any("Итого" in w for w in warnings)


def test_missing_values_block():
    data = photo_response()
    data.items[0].price = "?"
    data.doc_date = ""
    errors = blocking_errors(normalize(data))
    assert "Sana o'qilmadi" in errors
    assert any("1-qatorda" in e and "narxi" in e for e in errors)


def test_correction_round_trip():
    doc = normalize(photo_response())
    assert normalize(to_extracted(doc)).items == doc.items


CPS = [
    matching.Counterparty(1, "Тошматов Бахтиёр  (снабженец)", "100000001", "Договор № 1", None),
    matching.Counterparty(2, "ООО Рустамов Акмал (материал)", "100000002", "x", None),
    matching.Counterparty(3, '"ROADTRACK" MCHJ', "100000003", None, None),
    matching.Counterparty(4, "(BS) OLTIN YOL AVTO TRANS MCHJ (Кум)", "100000004", "x", None),
]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Тошматов Бахтиёр", 1),
        ("Тошмотов Бахтиёр", 1),
        ("Бахтиёр Тошматов", 1),
        ("Toshmatov Baxtiyor", 1),
        ("Рустамов Акмал", 2),
        ("ROAD TRACK", 3),
        ("Олтин Йўл Авто Транс", 4),
    ],
)
def test_auto_match(raw, expected):
    match = matching.auto_match(matching.rank(raw, CPS))
    assert match is not None and match.id == expected


@pytest.mark.parametrize("raw", ["Каримов Жасур", "", "?"])
def test_unknown_supplier_is_not_auto_matched(raw):
    assert matching.auto_match(matching.rank(raw, CPS)) is None


load_dotenv()
DB_URL = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL yo'q")


TEST_SEED = """
INSERT INTO counterparties (name, inn, contract, comment) VALUES
    ('Тошматов Бахтиёр  (снабженец)', '100000001', 'Договор № 1 от 01.01.2026г.', NULL),
    ('ООО Рустамов Акмал (материал)', '100000002', 'Договор № 2', 'ООО Рустамов Акмал'),
    ('"ROADTRACK" MCHJ', '100000003', NULL, 'ROAD TRAK'),
    ('(BS) OLTIN YOL AVTO TRANS MCHJ (Кум)', '100000004', 'Договор № 8', NULL)
"""


async def reset(r: Repo) -> None:
    await r.pool.execute(
        "TRUNCATE documents, document_items, exports, counterparties RESTART IDENTITY CASCADE"
    )
    await r.pool.execute(SCHEMA.read_text(encoding="utf-8"))
    await r.pool.execute(TEST_SEED)


@pytest_asyncio.fixture
async def repo():
    r = await Repo.connect(DB_URL)
    await reset(r)
    yield r
    await r.close()


async def _confirmed_photo_doc(repo: Repo) -> int:
    doc = normalize(photo_response())
    match = matching.auto_match(matching.rank(doc.supplier, await repo.counterparties()))
    assert match.inn == "100000001"
    doc_id = await repo.create_draft(doc, match.id, 42, "file")
    assert confirm_blockers(await repo.get(doc_id)) == []
    assert await repo.set_status(doc_id, "draft", "confirmed")
    return doc_id


@needs_db
@pytest.mark.asyncio
async def test_csv_matches_1c_row_from_screenshot(repo):
    doc_id = await _confirmed_photo_doc(repo)
    export_id, docs = await repo.create_export(42)

    raw = build_csv(docs, SETTINGS)
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert rows[0] == COLUMNS
    assert len(rows) == 4
    idx = {name: COLUMNS.index(name) for name in COLUMNS}
    first = rows[1]
    assert first[idx["Date"]] == "03.11.2026"
    assert first[idx["ИНН"]] == "100000001"
    assert first[idx["Контрагент"]] == "Тошматов Бахтиёр  (снабженец)"
    assert first[idx["Договор"]] == "Договор № 1 от 01.01.2026г."
    assert first[idx["Сумма документа"]] == "720000"
    assert first[idx["Валюта"]] == "UZS"
    assert first[idx["Ставки НДС"]] == "Без НДС"
    assert first[idx["Дата вх.док-та"]] == "03.11.2026"
    assert first[idx["ID загрузки"]] == str(doc_id)
    assert [r[idx["Наименование"]] for r in rows[1:]] == ["А4 лист", "Офис дафтари", "Офис папка"]
    assert [r[idx["Сумма"]] for r in rows[1:]] == ["500000", "140000", "80000"]
    assert [r[idx["Цена"]] for r in rows[1:]] == ["250000", "35000", "40000"]

    assert export_id == 1
    assert await repo.confirmed() == []
    assert await repo.create_export(42) is None
    assert (await repo.last_export())[0] == export_id


@needs_db
@pytest.mark.asyncio
async def test_duplicate_detected_and_state_guards(repo):
    first = await _confirmed_photo_doc(repo)
    second = await repo.create_draft(
        normalize(photo_response()), (await repo.get(first)).counterparty.id, 42, "file2"
    )
    stored = await repo.get(second)
    assert await repo.find_duplicates(stored) == [first]
    assert f"#{first}" in render_card(stored, [first])

    assert await repo.set_status(second, "draft", "cancelled")
    assert not await repo.set_status(second, "draft", "confirmed")
    assert not await repo.set_counterparty(first, 1)
    assert not await repo.update_draft(first, normalize(photo_response()), None)


@needs_db
@pytest.mark.asyncio
async def test_counterparty_without_contract_only_warns(repo):
    cp = next(c for c in await repo.counterparties() if c.inn == "100000003")
    doc_id = await repo.create_draft(normalize(photo_response()), cp.id, 42, None)
    stored = await repo.get(doc_id)
    assert confirm_blockers(stored) == []
    assert "⚠️ Договор yo" in render_card(stored, [])

    await repo.upsert_counterparty(cp.name, cp.inn, "Договор № 18-T", None)
    stored = await repo.get(doc_id)
    assert "⚠️ Договор yo" not in render_card(stored, [])
    assert stored.counterparty.comment == "ROAD TRAK"
