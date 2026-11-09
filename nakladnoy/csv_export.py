"""1C "Поступление материалов" jurnali ustunlari tartibidagi CSV.

Bir tovar qatori = bir CSV qatori, sarlavha maydonlari har qatorda takrorlanadi.
"ID загрузки" bir hujjat qatorlarini guruhlash uchun. O'zbek harflari (Қ, ғ) cp1251'ga sig'maydi,
shuning uchun BOM bilan UTF-8.
"""
import csv
import io
from decimal import Decimal

from .config import Settings
from .db import StoredDocument
from .extraction import lines_total

# Skrinshotda kesilgan sarlavhalar ko'ringan qismi bilan yozilgan.
COLUMNS = [
    "Date", "Number", "Биржевая", "ИНН", "Биржевая", "Контрагент", "Договор", "Сумма документа",
    "Валюта", "Сумма в валюте", "Подразделение", "Ставки НДС", "№ вх.док-та", "Дата вх.док-та",
    "Доверенность", "Таможня", "Договор та", "Утверждено охранни", "Ответственный",
    "Дата последнего изменения", "Комментарий",
    "ID загрузки", "Наименование", "Ед.изм", "Кол-во", "Цена", "Сумма",
]


def format_number(value: Decimal | None) -> str:
    """1C rus lokali uchun o'nli vergul; ming ajratuvchisiz."""
    if value is None:
        return ""
    return format(value.normalize(), "f").replace(".", ",")


def build_csv(documents: list[StoredDocument], settings: Settings) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=settings.csv_delimiter, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for stored in documents:
        doc, cp = stored.doc, stored.counterparty
        date_text = doc.doc_date.strftime("%d.%m.%Y")
        total = format_number(lines_total(doc))
        for item in doc.items:
            writer.writerow([
                date_text, "", "", cp.inn or "", "", cp.name, cp.contract or "", total,
                settings.currency, "", settings.department, settings.vat_rate, "", date_text,
                "", "", "", "", settings.responsible, "", cp.comment or "",
                stored.id, item.name, item.unit,
                format_number(item.qty), format_number(item.price), format_number(item.amount),
            ])
    return buffer.getvalue().encode("utf-8-sig")
