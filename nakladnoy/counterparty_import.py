"""1C'dan yuklab olingan Excel fayldagi kontragentlarni o'qish (.xlsx va .xls).

Kalit 1C'dagi nom: bazada ko'p kontragentning ИНН'si yo'q yoki soxta (111111111, 888 ...),
bir ИНН o'nlab kontragentda takrorlanadi.
"""
import re
from dataclasses import dataclass, field, replace
from io import BytesIO

import openpyxl
import xlrd

# Birinchi mos kelgani olinadi: 1C jurnalidagi nom "Наименование", "Полное" esa boshqacha bo'ladi.
NAME_HEADERS = ("контрагент", "наименование", "полное наименование", "название", "kontragent", "nomi")
COMMENT_HEADERS = ("комментарий", "примечание", "izoh")
HEADER_SEARCH_ROWS = 30
INN_HEADER = re.compile(r"(?<!\w)(инн|стир|inn|stir)(?!\w)")
HAS_LETTER = re.compile(r"[^\W\d_]")
INN_FORMAT = re.compile(r"\d{9}|\d{14}")
FAKE_INNS = {"123456789", "987654321"}


class ImportFormatError(ValueError):
    """Foydalanuvchiga ko'rsatiladigan xato."""


@dataclass(frozen=True)
class ImportRow:
    name: str
    inn: str | None
    contract: str | None
    comment: str | None


@dataclass
class ImportResult:
    rows: list[ImportRow] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    duplicates: int = 0


def is_real_inn(inn: str | None) -> bool:
    return (
        bool(inn)
        and bool(INN_FORMAT.fullmatch(inn))
        and not inn.startswith("0")
        and len(set(inn)) > 1
        and inn not in FAKE_INNS
    )


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).replace("\xa0", " ").strip()


def _read_sheets(data: bytes) -> list[list[list]]:
    try:
        if data[:2] == b"PK":
            book = openpyxl.load_workbook(BytesIO(data), data_only=True)
            return [[list(r) for r in sheet.iter_rows(values_only=True)] for sheet in book.worksheets]
        if data[:4] == b"\xd0\xcf\x11\xe0":
            book = xlrd.open_workbook(file_contents=data)
            return [
                [sheet.row_values(i) for i in range(sheet.nrows)] for sheet in book.sheets()
            ]
    except Exception as e:
        raise ImportFormatError("Excel faylni ochib bo'lmadi, fayl buzilgan bo'lishi mumkin.") from e
    raise ImportFormatError("Bu Excel fayl emas. .xlsx yoki .xls yuboring.")


def _find_columns(row: list) -> dict[str, int] | None:
    cells = [re.sub(r"\s+", " ", _text(c).lower()) for c in row]
    inn = next((i for i, c in enumerate(cells) if INN_HEADER.search(c)), None)
    if inn is None:
        return None
    contract = next((i for i, c in enumerate(cells) if "договор" in c or "shartnoma" in c), None)
    name = next(
        (
            i
            for alias in NAME_HEADERS
            for i, c in enumerate(cells)
            if c.startswith(alias) and i not in (inn, contract)
        ),
        None,
    )
    if name is None:
        return None
    comment = next(
        (i for i, c in enumerate(cells) if c.startswith(COMMENT_HEADERS) and i != name), None
    )
    return {"name": name, "inn": inn, "contract": contract, "comment": comment}


def _cell(row: list, index: int | None) -> str:
    return _text(row[index]) if index is not None and index < len(row) else ""


def parse_workbook(data: bytes) -> ImportResult:
    """Sarlavhani topadi va qatorlarni nom bo'yicha yagona qilib qaytaradi."""
    for rows in _read_sheets(data):
        for header_index, header in enumerate(rows[:HEADER_SEARCH_ROWS]):
            columns = _find_columns(header)
            if columns is not None:
                return _parse_rows(rows, header_index, columns)
    raise ImportFormatError(
        "Sarlavhada kontragent nomi va ИНН ustunlari topilmadi. "
        "Ustunlar «Контрагент» (yoki «Наименование») va «ИНН» deb nomlangan bo'lishi kerak."
    )


def _parse_rows(rows: list[list], header_index: int, columns: dict[str, int]) -> ImportResult:
    result = ImportResult()
    by_name: dict[str, ImportRow] = {}
    for number, row in enumerate(rows[header_index + 1 :], header_index + 2):
        name = _cell(row, columns["name"])
        inn = re.sub(r"\s", "", _cell(row, columns["inn"])) or None
        if not name and not inn:
            continue
        if not name:
            result.skipped.append((number, "nomi yo'q"))
            continue
        if not HAS_LETTER.search(name):
            result.skipped.append((number, f"nomi noto'g'ri: {name}"))
            continue
        item = ImportRow(
            name, inn, _cell(row, columns["contract"]) or None, _cell(row, columns["comment"]) or None
        )
        previous = by_name.get(name)
        if previous is not None:
            result.duplicates += 1
            item = replace(
                item,
                inn=previous.inn if is_real_inn(previous.inn) else item.inn or previous.inn,
                contract=item.contract or previous.contract,
                comment=item.comment or previous.comment,
            )
        by_name[name] = item
    result.rows = list(by_name.values())
    return result
