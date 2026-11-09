from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import asyncpg

from .extraction import Document, Item
from .matching import Counterparty

SCHEMA = Path(__file__).with_name("schema.sql")


@dataclass
class StoredDocument:
    id: int
    status: str
    doc: Document
    counterparty: Counterparty | None


def _counterparty(row) -> Counterparty:
    return Counterparty(row["id"], row["name"], row["inn"], row["contract"], row["comment"])


class Repo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Repo":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        async with pool.acquire() as con:
            await con.execute(SCHEMA.read_text(encoding="utf-8"))
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def counterparties(self) -> list[Counterparty]:
        rows = await self.pool.fetch("SELECT * FROM counterparties ORDER BY name")
        return [_counterparty(r) for r in rows]

    async def upsert_counterparty(
        self, name: str, inn: str, contract: str | None, comment: str | None
    ) -> None:
        await self.pool.execute(
            """INSERT INTO counterparties (name, inn, contract, comment) VALUES ($1, $2, $3, $4)
               ON CONFLICT (inn) DO UPDATE SET name = EXCLUDED.name, contract = EXCLUDED.contract,
                   comment = COALESCE(EXCLUDED.comment, counterparties.comment)""",
            name, inn, contract, comment,
        )

    @staticmethod
    async def _write_items(con, doc_id: int, items: list[Item]) -> None:
        await con.execute("DELETE FROM document_items WHERE document_id = $1", doc_id)
        await con.executemany(
            """INSERT INTO document_items (document_id, line_no, name, unit, qty, price, amount)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            [
                (doc_id, n, i.name, i.unit, i.qty, i.price, i.amount)
                for n, i in enumerate(items, 1)
            ],
        )

    async def create_draft(
        self, doc: Document, counterparty_id: int | None, user_id: int, photo_file_id: str | None
    ) -> int:
        async with self.pool.acquire() as con, con.transaction():
            doc_id = await con.fetchval(
                """INSERT INTO documents (doc_date, supplier, receiver, total, unclear,
                                          counterparty_id, user_id, photo_file_id)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id""",
                doc.doc_date, doc.supplier, doc.receiver, doc.total, doc.unclear,
                counterparty_id, user_id, photo_file_id,
            )
            await self._write_items(con, doc_id, doc.items)
        return doc_id

    async def update_draft(self, doc_id: int, doc: Document, counterparty_id: int | None) -> bool:
        async with self.pool.acquire() as con, con.transaction():
            status = await con.fetchval(
                """UPDATE documents SET doc_date = $2, supplier = $3, receiver = $4, total = $5,
                       unclear = $6, counterparty_id = $7
                   WHERE id = $1 AND status = 'draft' RETURNING status""",
                doc_id, doc.doc_date, doc.supplier, doc.receiver, doc.total, doc.unclear,
                counterparty_id,
            )
            if status is None:
                return False
            await self._write_items(con, doc_id, doc.items)
        return True

    async def set_counterparty(self, doc_id: int, counterparty_id: int) -> bool:
        result = await self.pool.execute(
            "UPDATE documents SET counterparty_id = $2 WHERE id = $1 AND status = 'draft'",
            doc_id, counterparty_id,
        )
        return result == "UPDATE 1"

    async def set_status(self, doc_id: int, old: str, new: str) -> bool:
        result = await self.pool.execute(
            "UPDATE documents SET status = $3 WHERE id = $1 AND status = $2", doc_id, old, new
        )
        return result == "UPDATE 1"

    async def _load(self, con, where: str, *args) -> list[StoredDocument]:
        rows = await con.fetch(
            f"""SELECT d.*, c.id AS c_id, c.name AS c_name, c.inn AS c_inn,
                       c.contract AS c_contract, c.comment AS c_comment
                FROM documents d LEFT JOIN counterparties c ON c.id = d.counterparty_id
                WHERE {where} ORDER BY d.doc_date, d.id""",
            *args,
        )
        items = await con.fetch(
            "SELECT * FROM document_items WHERE document_id = ANY($1) ORDER BY document_id, line_no",
            [r["id"] for r in rows],
        )
        by_doc: dict[int, list[Item]] = {}
        for i in items:
            by_doc.setdefault(i["document_id"], []).append(
                Item(i["name"], i["unit"], _clean(i["qty"]), _clean(i["price"]), _clean(i["amount"]))
            )
        return [
            StoredDocument(
                id=r["id"],
                status=r["status"],
                doc=Document(
                    doc_date=r["doc_date"],
                    supplier=r["supplier"],
                    receiver=r["receiver"],
                    total=_clean(r["total"]),
                    items=by_doc.get(r["id"], []),
                    unclear=list(r["unclear"]),
                ),
                counterparty=(
                    Counterparty(r["c_id"], r["c_name"], r["c_inn"], r["c_contract"], r["c_comment"])
                    if r["c_id"] is not None
                    else None
                ),
            )
            for r in rows
        ]

    async def get(self, doc_id: int) -> StoredDocument | None:
        async with self.pool.acquire() as con:
            docs = await self._load(con, "d.id = $1", doc_id)
        return docs[0] if docs else None

    async def confirmed(self) -> list[StoredDocument]:
        async with self.pool.acquire() as con:
            return await self._load(con, "d.status = 'confirmed'")

    async def find_duplicates(self, stored: StoredDocument) -> list[int]:
        """Shu kontragent, sana va summali boshqa hujjatlar (bekor qilinganlardan tashqari)."""
        if stored.counterparty is None or stored.doc.doc_date is None:
            return []
        rows = await self.pool.fetch(
            """SELECT d.id FROM documents d
               WHERE d.id <> $1 AND d.status IN ('confirmed', 'exported')
                 AND d.counterparty_id = $2 AND d.doc_date = $3
                 AND (SELECT sum(amount) FROM document_items WHERE document_id = d.id) = $4""",
            stored.id, stored.counterparty.id, stored.doc.doc_date,
            sum((i.amount or Decimal(0) for i in stored.doc.items), Decimal(0)),
        )
        return [r["id"] for r in rows]

    async def create_export(self, user_id: int) -> tuple[int, list[StoredDocument]] | None:
        """Tasdiqlangan hujjatlarni bitta eksportga atomik o'tkazadi."""
        async with self.pool.acquire() as con, con.transaction():
            ids = await con.fetch(
                "SELECT id FROM documents WHERE status = 'confirmed' FOR UPDATE"
            )
            if not ids:
                return None
            export_id = await con.fetchval(
                "INSERT INTO exports (user_id) VALUES ($1) RETURNING id", user_id
            )
            await con.execute(
                """UPDATE documents SET status = 'exported', export_id = $1
                   WHERE id = ANY($2)""",
                export_id, [r["id"] for r in ids],
            )
            return export_id, await self._load(con, "d.export_id = $1", export_id)

    async def last_export(self) -> tuple[int, list[StoredDocument]] | None:
        async with self.pool.acquire() as con:
            export_id = await con.fetchval("SELECT max(id) FROM exports")
            if export_id is None:
                return None
            return export_id, await self._load(con, "d.export_id = $1", export_id)


def _clean(value: Decimal | None) -> Decimal | None:
    """NUMERIC(18,2) dagi ortiqcha nollarni olib tashlaydi: 250000.00 -> 250000."""
    if value is None:
        return None
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value.normalize()
