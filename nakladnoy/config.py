import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_model: str
    database_url: str
    allowed_user_ids: frozenset[int]
    currency: str
    vat_rate: str
    department: str
    responsible: str
    csv_delimiter: str


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    ids = os.getenv("ALLOWED_USER_IDS", "")
    return Settings(
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip(),
        gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.6-flash").strip(),
        database_url=os.environ["DATABASE_URL"].strip(),
        allowed_user_ids=frozenset(int(x) for x in ids.split(",") if x.strip()),
        currency=os.getenv("DOC_CURRENCY", "UZS"),
        vat_rate=os.getenv("DOC_VAT_RATE", "Без НДС"),
        department=os.getenv("DOC_DEPARTMENT", ""),
        responsible=os.getenv("DOC_RESPONSIBLE", ""),
        csv_delimiter=os.getenv("CSV_DELIMITER", ";"),
    )
