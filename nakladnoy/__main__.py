import asyncio
import logging
import sys

from .bot import run
from .config import load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", settings.telegram_token),
            ("GEMINI_API_KEY", settings.gemini_api_key),
        )
        if not value
    ]
    if missing:
        sys.exit(f".env faylida to'ldirilmagan: {', '.join(missing)}")
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
