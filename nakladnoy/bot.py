import asyncio
import logging
from html import escape
from io import BytesIO

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import matching
from .config import Settings
from .counterparty_import import ImportFormatError, is_real_inn, parse_workbook
from .csv_export import build_csv, format_number
from .db import Repo, StoredDocument
from .extraction import (
    blocking_errors,
    check_warnings,
    lines_total,
    normalize,
    to_extracted,
    today_local,
)
from .gemini import GeminiExtractor

log = logging.getLogger(__name__)

HELP = (
    "Nakladnoy rasmini yuboring. Bir nechta rasm ham bo'ladi.\n\n"
    "Tekshirib, ✅ bosing. Kun oxirida /csv bilan hammasini bitta faylda oling.\n\n"
    "/navbat — tasdiqlangan, CSV kutayotganlar\n"
    "/csv — navbatdagilarni CSV qilish\n"
    "/qayta — oxirgi CSV'ni qayta olish\n"
    "/kontragentlar nom — qidirish\n"
    "/kontragent Nomi | INN | Договор | Izoh — qo'shish yoki yangilash\n"
    "/admin — boshqaruv paneli, kontragentlarni Excel'dan import"
)
COUNTERPARTY_LIST_LIMIT = 30
EXCEL_IMPORT_HINT = (
    "1C'dan yuklab olingan Excel faylni (.xlsx yoki .xls) shu yerga yuboring.\n"
    "Kerakli ustunlar: «Контрагент» yoki «Наименование» va «ИНН». "
    "«Договор» va «Комментарий» bo'lsa, ular ham olinadi.\n"
    "Bazaga yozishdan oldin natijani ko'rsataman."
)


class Editing(StatesGroup):
    waiting_instruction = State()


class AccessMiddleware(BaseMiddleware):
    """Faqat ALLOWED_USER_IDS dagilar ishlata oladi; ro'yxat bo'sh bo'lsa hech kim."""

    def __init__(self, allowed: frozenset[int]):
        self.allowed = allowed

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None and user.id in self.allowed:
            return await handler(event, data)
        log.warning("Ruxsatsiz urinish: user_id=%s", user.id if user else None)
        if event.message is not None and user is not None:
            await event.message.answer(
                f"Ruxsat yo'q. Sizning ID: {user.id}\n"
                "Uni .env faylidagi ALLOWED_USER_IDS ga qo'shing."
            )
        return None


def inn_text(cp: matching.Counterparty) -> str:
    return escape(cp.inn) if cp.inn else "yo'q"


def contract_text(cp: matching.Counterparty) -> str:
    return escape(cp.contract) if cp.contract else "❓ yo'q"


async def answer_long(message: Message, lines: list[str]) -> None:
    """Telegram xabari 4096 belgidan oshmasligi uchun bo'lib yuboradi."""
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    await message.answer(chunk)


def render_card(stored: StoredDocument, duplicates: list[int]) -> str:
    doc, cp = stored.doc, stored.counterparty
    lines = [
        f"<b>Nakladnoy #{stored.id}</b>",
        f"Sana: {doc.doc_date:%d.%m.%Y}" if doc.doc_date else "Sana: ❓",
        f"Kimdan: {escape(doc.supplier)}",
        f"Kimga: {escape(doc.receiver)}",
        "",
    ]
    if cp:
        lines += [
            f"Kontragent: <b>{escape(cp.name)}</b>",
            f"ИНН: {inn_text(cp)}",
            f"Договор: {contract_text(cp)}",
        ]
    else:
        lines.append("Kontragent: ❓ pastdan tanlang")
    lines.append("")
    for n, i in enumerate(doc.items, 1):
        lines.append(
            f"{n}. {escape(i.name)} — {format_number(i.qty) or '?'} {escape(i.unit)} × "
            f"{format_number(i.price) or '?'} = {format_number(i.amount) or '?'}"
        )
    lines.append(f"\n<b>Jami: {format_number(lines_total(doc))}</b>")

    errors = confirm_blockers(stored)
    if errors:
        lines.append("\n⛔ " + "\n⛔ ".join(escape(e) for e in errors))
    warnings = check_warnings(doc)
    if cp and not cp.contract:
        warnings.append("Договор yo'q: CSV'da bo'sh bo'ladi")
    if duplicates:
        ids = ", ".join(f"#{d}" for d in duplicates)
        warnings.append(f"Shunday hujjat allaqachon bor: {ids}")
    if warnings:
        lines.append("\n⚠️ " + "\n⚠️ ".join(escape(w) for w in warnings))
    return "\n".join(lines)


def confirm_blockers(stored: StoredDocument) -> list[str]:
    errors = blocking_errors(stored.doc)
    if stored.counterparty is None:
        errors.append("Kontragent tanlanmagan")
    return errors


def card_keyboard(stored: StoredDocument, options: list[matching.Counterparty]) -> InlineKeyboardMarkup:
    rows = []
    if stored.counterparty is None:
        rows += [
            [InlineKeyboardButton(text=cp.name[:60], callback_data=f"cp:{stored.id}:{cp.id}")]
            for cp in options
        ]
    actions = []
    if not confirm_blockers(stored):
        actions.append(InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ok:{stored.id}"))
    actions.append(InlineKeyboardButton(text="✏️ Tuzatish", callback_data=f"ed:{stored.id}"))
    actions.append(InlineKeyboardButton(text="❌ Bekor", callback_data=f"no:{stored.id}"))
    rows.append(actions)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def card_payload(repo: Repo, doc_id: int) -> dict:
    stored = await repo.get(doc_id)
    ranked = matching.rank(stored.doc.supplier, await repo.counterparties())
    duplicates = await repo.find_duplicates(stored)
    return {
        "text": render_card(stored, duplicates),
        "reply_markup": card_keyboard(stored, matching.candidates(ranked)),
    }


def build_dispatcher(settings: Settings, repo: Repo, extractor: GeminiExtractor) -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(AccessMiddleware(settings.allowed_user_ids))

    @dp.message(CommandStart())
    @dp.message(Command("help"))
    async def on_help(message: Message):
        await message.answer(HELP)

    async def process_image(message: Message, bot: Bot, file_id: str, mime: str):
        progress = await message.answer("⏳ O'qiyapman...")
        try:
            buffer: BytesIO = await bot.download(file_id)
            extracted = await extractor.extract(buffer.getvalue(), mime)
        except Exception:
            log.exception("Rasmni o'qishda xato")
            await progress.edit_text("❌ Rasmni o'qib bo'lmadi. Bir ozdan keyin qayta yuboring.")
            return
        if not extracted.is_nakladnaya:
            await progress.edit_text("Bu rasm nakladnoyga o'xshamaydi.")
            return
        doc = normalize(extracted)
        match = matching.auto_match(matching.rank(doc.supplier, await repo.counterparties()))
        doc_id = await repo.create_draft(
            doc, match.id if match else None, message.from_user.id, file_id
        )
        await progress.edit_text(**await card_payload(repo, doc_id))

    @dp.message(F.photo)
    async def on_photo(message: Message, bot: Bot):
        await process_image(message, bot, message.photo[-1].file_id, "image/jpeg")

    @dp.message(F.document.mime_type.startswith("image/"))
    async def on_image_file(message: Message, bot: Bot):
        await process_image(message, bot, message.document.file_id, message.document.mime_type)

    @dp.callback_query(F.data.startswith("cp:"))
    async def on_pick(callback: CallbackQuery):
        _, doc_id, cp_id = callback.data.split(":")
        if not await repo.set_counterparty(int(doc_id), int(cp_id)):
            await callback.answer("Bu hujjat endi qoralama emas", show_alert=True)
            return
        await callback.message.edit_text(**await card_payload(repo, int(doc_id)))
        await callback.answer()

    @dp.callback_query(F.data.startswith("ok:"))
    async def on_confirm(callback: CallbackQuery):
        doc_id = int(callback.data.split(":")[1])
        stored = await repo.get(doc_id)
        errors = confirm_blockers(stored)
        if errors:
            await callback.answer(errors[0], show_alert=True)
            return
        if not await repo.set_status(doc_id, "draft", "confirmed"):
            await callback.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
            return
        count = len(await repo.confirmed())
        await callback.message.edit_text(
            f"{callback.message.html_text}\n\n✅ <b>Tasdiqlandi.</b> Navbatda: {count}. "
            "Tayyor bo'lganda /csv"
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("no:"))
    async def on_cancel(callback: CallbackQuery):
        doc_id = int(callback.data.split(":")[1])
        if not await repo.set_status(doc_id, "draft", "cancelled"):
            await callback.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
            return
        await callback.message.edit_text(f"❌ Nakladnoy #{doc_id} bekor qilindi.")
        await callback.answer()

    @dp.callback_query(F.data.startswith("ed:"))
    async def on_edit(callback: CallbackQuery, state: FSMContext):
        doc_id = int(callback.data.split(":")[1])
        await state.set_state(Editing.waiting_instruction)
        await state.update_data(doc_id=doc_id)
        await callback.message.answer(
            f"#{doc_id} uchun nimani tuzatay? Oddiy yozing, masalan:\n"
            "«2-qator soni 5, summasi 175000» yoki «sana 02.11.2026»"
        )
        await callback.answer()

    @dp.message(Command("navbat"))
    async def on_queue(message: Message):
        docs = await repo.confirmed()
        if not docs:
            await message.answer("Navbat bo'sh.")
            return
        lines = [
            f"#{d.id} {d.doc.doc_date:%d.%m.%Y} {escape(d.counterparty.name)} — "
            f"{format_number(lines_total(d.doc))}"
            for d in docs
        ]
        await answer_long(message, lines + [f"\nJami {len(docs)} ta. /csv"])

    async def send_export(message: Message, export_id: int, docs: list[StoredDocument]):
        data = build_csv(docs, settings)
        name = f"postuplenie_{today_local():%Y-%m-%d}_{export_id}.csv"
        grand_total = sum((lines_total(d.doc) for d in docs), 0)
        await message.answer_document(
            BufferedInputFile(data, filename=name),
            caption=f"{len(docs)} ta nakladnoy, jami {format_number(grand_total)}",
        )

    @dp.message(Command("csv"))
    async def on_csv(message: Message):
        result = await repo.create_export(message.from_user.id)
        if result is None:
            await message.answer("Navbat bo'sh, tasdiqlangan nakladnoy yo'q.")
            return
        try:
            await send_export(message, *result)
        except Exception:
            log.exception("CSV yuborishda xato")
            await message.answer("❌ Fayl yuborilmadi, hujjatlar saqlangan. /qayta bilan oling.")

    @dp.message(Command("qayta"))
    async def on_resend(message: Message):
        result = await repo.last_export()
        if result is None:
            await message.answer("Hali CSV yaratilmagan.")
            return
        await send_export(message, *result)

    async def send_counterparties(message: Message, query: str | None):
        counterparties = await repo.counterparties()
        if query:
            ranked = matching.rank(query, counterparties)[:COUNTERPARTY_LIST_LIMIT]
            shown = [c for c, score in ranked if score >= matching.CANDIDATE_SCORE]
            footer = f"\nTopildi: {len(shown)}"
        else:
            shown = counterparties[:COUNTERPARTY_LIST_LIMIT]
            footer = f"\nJami {len(counterparties)} ta. Qidirish: /kontragentlar nom"
        lines = [f"{escape(c.name)} — {inn_text(c)}\n   {contract_text(c)}" for c in shown]
        await answer_long(message, lines + [footer])

    @dp.message(Command("kontragentlar"))
    async def on_counterparties(message: Message, command: CommandObject):
        await send_counterparties(message, command.args)

    @dp.message(Command("kontragent"))
    async def on_add_counterparty(message: Message, command: CommandObject):
        parts = [p.strip() for p in (command.args or "").split("|")]
        if len(parts) < 3 or not all(parts[:3]) or not parts[1].isdigit():
            await message.answer(
                "Format: /kontragent Nomi | INN | Договор | Izoh (ixtiyoriy)\n"
                "INN shu bo'lsa yangilanadi, bo'lmasa qo'shiladi."
            )
            return
        comment = parts[3] if len(parts) > 3 and parts[3] else None
        await repo.upsert_counterparty(parts[0], parts[1], parts[2], comment)
        await message.answer(f"Saqlandi: {escape(parts[0])}")

    @dp.message(Command("admin"))
    async def on_admin(message: Message):
        counterparties = await repo.counterparties()
        without_contract = sum(1 for c in counterparties if not c.contract)
        await message.answer(
            "<b>Boshqaruv paneli</b>\n\n"
            f"Kontragentlar: {len(counterparties)}\n"
            f"Договор kiritilmagan: {without_contract}\n"
            f"CSV navbatida: {len(await repo.confirmed())}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📥 Excel'dan import", callback_data="adm:import")],
                    [InlineKeyboardButton(text="📋 Kontragentlar", callback_data="adm:list")],
                ]
            ),
        )

    @dp.callback_query(F.data == "adm:import")
    async def on_admin_import(callback: CallbackQuery):
        await callback.message.answer(EXCEL_IMPORT_HINT)
        await callback.answer()

    @dp.callback_query(F.data == "adm:list")
    async def on_admin_list(callback: CallbackQuery):
        await send_counterparties(callback.message, None)
        await callback.answer()

    @dp.message(F.document.file_name.regexp(r"(?i).+\.xlsx?$"))
    async def on_excel(message: Message, bot: Bot, state: FSMContext):
        progress = await message.answer("⏳ Excel'ni o'qiyapman...")
        try:
            buffer: BytesIO = await bot.download(message.document.file_id)
            result = await asyncio.to_thread(parse_workbook, buffer.getvalue())
        except ImportFormatError as e:
            await progress.edit_text(f"❌ {escape(str(e))}")
            return
        except Exception:
            log.exception("Excel importida xato")
            await progress.edit_text("❌ Faylni o'qib bo'lmadi, qayta yuboring.")
            return
        skipped = [f"{n}-qator: {escape(reason)}" for n, reason in result.skipped[:5]]
        if len(result.skipped) > 5:
            skipped.append(f"... yana {len(result.skipped) - 5} ta")
        report = "\n".join(skipped)
        if not result.rows:
            await progress.edit_text(f"Import qilinadigan kontragent topilmadi.\n\n{report}")
            return
        existing = {c.name for c in await repo.counterparties()}
        new = sum(1 for r in result.rows if r.name not in existing)
        lines = [
            "<b>Excel import</b>",
            f"Yangi: {new}",
            f"Yangilanadi: {len(result.rows) - new}",
            f"ИНН yo'q yoki soxta: {sum(1 for r in result.rows if not is_real_inn(r.inn))}",
            f"Договор yo'qlar: {sum(1 for r in result.rows if not r.contract)}",
        ]
        if result.duplicates:
            lines.append(f"Takroriy nom: {result.duplicates} ta, birlashtirildi")
        if result.skipped:
            lines += [f"\nO'tkazib yuborildi: {len(result.skipped)}", report]
        await state.update_data(import_rows=result.rows)
        await progress.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Import qilish", callback_data="imp:ok"),
                        InlineKeyboardButton(text="❌ Bekor", callback_data="imp:no"),
                    ]
                ]
            ),
        )

    @dp.callback_query(F.data == "imp:ok")
    async def on_import_confirm(callback: CallbackQuery, state: FSMContext):
        rows = (await state.get_data()).get("import_rows")
        if not rows:
            await callback.answer("Import eskirgan, faylni qayta yuboring", show_alert=True)
            return
        await repo.import_counterparties(rows)
        await state.update_data(import_rows=None)
        await callback.message.edit_text(f"✅ {len(rows)} ta kontragent import qilindi.")
        await callback.answer()

    @dp.callback_query(F.data == "imp:no")
    async def on_import_cancel(callback: CallbackQuery, state: FSMContext):
        await state.update_data(import_rows=None)
        await callback.message.edit_text("❌ Import bekor qilindi.")
        await callback.answer()

    @dp.message(Editing.waiting_instruction, F.text)
    async def on_edit_instruction(message: Message, state: FSMContext):
        doc_id = (await state.get_data())["doc_id"]
        await state.clear()
        stored = await repo.get(doc_id)
        if stored is None or stored.status != "draft":
            await message.answer("Bu hujjat endi qoralama emas.")
            return
        progress = await message.answer("⏳ Tuzatyapman...")
        try:
            corrected = await extractor.correct(to_extracted(stored.doc), message.text)
        except Exception:
            log.exception("Tuzatishda xato")
            await progress.edit_text("❌ Tuzatib bo'lmadi, qayta urinib ko'ring.")
            return
        doc = normalize(corrected)
        cp_id = stored.counterparty.id if stored.counterparty else None
        if doc.supplier != stored.doc.supplier:
            match = matching.auto_match(matching.rank(doc.supplier, await repo.counterparties()))
            cp_id = match.id if match else None
        await repo.update_draft(doc_id, doc, cp_id)
        await progress.edit_text(**await card_payload(repo, doc_id))

    @dp.message(F.text)
    async def on_text(message: Message):
        await message.answer(HELP)

    return dp


async def run(settings: Settings) -> None:
    repo = await Repo.connect(settings.database_url)
    bot = Bot(settings.telegram_token, default=DefaultBotProperties(parse_mode="HTML"))
    extractor = GeminiExtractor(
        settings.gemini_api_key, settings.gemini_model, settings.gemini_fallback_model
    )
    dp = build_dispatcher(settings, repo, extractor)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await repo.close()
