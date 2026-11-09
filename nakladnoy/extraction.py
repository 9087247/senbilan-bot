"""Gemini javobining sxemasi va undagi matnlarni son/sanaga aylantirish."""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field


TASHKENT = timezone(timedelta(hours=5))


def today_local() -> date:
    return datetime.now(TASHKENT).date()


class ExtractedItem(BaseModel):
    name: str = Field(description="Nomi/Наименование, qog'ozda qanday yozilgan bo'lsa shunday")
    unit: str = Field(description="Ulch.bir/Ед.изм: шт, блок, кг ...")
    qty: str = Field(description="Soni/Кол-во, qog'ozdagi ko'rinishida")
    price: str = Field(description="Narxi/Цена, qog'ozdagi ko'rinishida")
    amount: str = Field(description="Summasi/Сумма, qog'ozdagi ko'rinishida")


class ExtractedDoc(BaseModel):
    is_nakladnaya: bool = Field(description="Rasm yuk xati/nakladnoymi")
    doc_date: str = Field(description="Sana DD.MM.YYYY ko'rinishida; o'qilmasa bo'sh")
    supplier: str = Field(description="Kimdan/От кого")
    receiver: str = Field(description="Kimga/Кому")
    items: list[ExtractedItem]
    total: str = Field(description="Итого/jami summa; yozilmagan bo'lsa bo'sh")
    unclear: list[str] = Field(
        default_factory=list,
        description="Aniq o'qib bo'lmagan joylar ro'yxati, qisqa, o'zbekcha",
    )


@dataclass
class Item:
    name: str
    unit: str
    qty: Decimal | None
    price: Decimal | None
    amount: Decimal | None


@dataclass
class Document:
    doc_date: date | None
    supplier: str
    receiver: str
    total: Decimal | None
    items: list[Item] = field(default_factory=list)
    unclear: list[str] = field(default_factory=list)


def parse_number(raw: str | None) -> Decimal | None:
    """"250.000" va "250,000" = 250000; "2,5" = 2.5. Qo'lyozmada nuqta odatda ming ajratuvchi."""
    if not raw:
        return None
    s = re.sub(r"\s", "", raw).lower()
    s = re.sub(r"(сум|сўм|so'?m|uzs)\.?$", "", s).rstrip(".,")
    if not re.fullmatch(r"\d[\d.,]*", s):
        return None
    dots, commas = s.count("."), s.count(",")
    if dots and commas:
        decimal_sep = "." if s.rfind(".") > s.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        s = s.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif dots + commas:
        sep = "." if dots else ","
        whole, _, tail = s.rpartition(sep)
        is_thousands = (dots + commas) > 1 or (
            len(tail) == 3 and len(whole) <= 3 and not whole.startswith("0")
        )
        s = s.replace(sep, "") if is_thousands else s.replace(sep, ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_date(raw: str | None) -> date | None:
    """Kun birinchi keladi: "3-11-2026" = 3-noyabr."""
    if not raw:
        return None
    parts = re.findall(r"\d+", raw)
    if len(parts) != 3:
        return None
    try:
        if len(parts[0]) == 4:
            return datetime.strptime("-".join(parts), "%Y-%m-%d").date()
        fmt = "%d-%m-%Y" if len(parts[2]) == 4 else "%d-%m-%y"
        return datetime.strptime("-".join(parts), fmt).date()
    except ValueError:
        return None


def normalize(doc: ExtractedDoc) -> Document:
    return Document(
        doc_date=parse_date(doc.doc_date),
        supplier=doc.supplier.strip(),
        receiver=doc.receiver.strip(),
        total=parse_number(doc.total),
        items=[
            Item(
                i.name.strip(),
                i.unit.strip(),
                parse_number(i.qty),
                parse_number(i.price),
                parse_number(i.amount),
            )
            for i in doc.items
        ],
        unclear=doc.unclear,
    )


def format_plain(value: Decimal | None) -> str:
    return "" if value is None else format(value.normalize(), "f")


def to_extracted(doc: Document) -> ExtractedDoc:
    """Tuzatish so'rovi uchun hujjatni Gemini sxemasiga qaytaradi."""
    return ExtractedDoc(
        is_nakladnaya=True,
        doc_date=doc.doc_date.strftime("%d.%m.%Y") if doc.doc_date else "",
        supplier=doc.supplier,
        receiver=doc.receiver,
        items=[
            ExtractedItem(
                name=i.name,
                unit=i.unit,
                qty=format_plain(i.qty),
                price=format_plain(i.price),
                amount=format_plain(i.amount),
            )
            for i in doc.items
        ],
        total=format_plain(doc.total),
        unclear=[],
    )


def blocking_errors(doc: Document) -> list[str]:
    """Bular tuzatilmaguncha hujjatni tasdiqlab bo'lmaydi."""
    errors = []
    if doc.doc_date is None:
        errors.append("Sana o'qilmadi")
    if not doc.items:
        errors.append("Tovar qatorlari topilmadi")
    for n, i in enumerate(doc.items, 1):
        missing = [
            label
            for label, value in (
                ("nomi", i.name),
                ("soni", i.qty),
                ("narxi", i.price),
                ("summasi", i.amount),
            )
            if not value
        ]
        if missing:
            errors.append(f"{n}-qatorda yo'q: {', '.join(missing)}")
    return errors


def check_warnings(doc: Document, today: date | None = None) -> list[str]:
    """Tasdiqlashga to'sqinlik qilmaydi, lekin ko'zdan kechirish kerak."""
    today = today or today_local()
    warnings = []
    if doc.doc_date:
        if doc.doc_date > today:
            warnings.append(f"Sana kelajakda: {doc.doc_date:%d.%m.%Y}")
        elif today - doc.doc_date > timedelta(days=30):
            warnings.append(f"Sana 30 kundan eski: {doc.doc_date:%d.%m.%Y}")
    for n, i in enumerate(doc.items, 1):
        if i.qty and i.price and i.amount and i.qty * i.price != i.amount:
            warnings.append(
                f"{n}-qator: {format_plain(i.qty)} × {format_plain(i.price)} = "
                f"{format_plain(i.qty * i.price)}, qog'ozda {format_plain(i.amount)}"
            )
    lines_sum = lines_total(doc)
    if doc.total is None:
        warnings.append("Итого o'qilmadi; CSV'ga qatorlar yig'indisi yoziladi")
    elif doc.total != lines_sum:
        warnings.append(
            f"Итого {format_plain(doc.total)}, qatorlar yig'indisi {format_plain(lines_sum)}; "
            "CSV'ga qatorlar yig'indisi yoziladi"
        )
    warnings.extend(f"Aniq o'qilmadi: {u}" for u in doc.unclear)
    return warnings


def lines_total(doc: Document) -> Decimal:
    return sum((i.amount for i in doc.items if i.amount is not None), Decimal(0))
