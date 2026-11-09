# Nakladnoy bot

Qo'lda yozilgan nakladnoy rasmini Telegram orqali oladi, Gemini bilan o'qiydi, PostgreSQL'da saqlaydi va 1C "Поступление материалов" uchun bitta CSV beradi.

## Ishga tushirish

1. `.env` faylida to'ldiring:
   - `TELEGRAM_BOT_TOKEN`: @BotFather → `/newbot`
   - `GEMINI_API_KEY`: https://aistudio.google.com/apikey
   - `DOC_DEPARTMENT`, `DOC_RESPONSIBLE`: 1C'dagi to'liq nomlar
2. `./run.ps1` ni ishga tushiring. U PostgreSQL'ni ham yoqadi.
3. Botga yozing. U ID'ingizni aytadi: uni `.env` dagi `ALLOWED_USER_IDS` ga yozib, botni qayta ishga tushiring.

Kompyuter o'chib yonsa, `run.ps1` ni qayta ishga tushiring.

## Ishlatish

1. Nakladnoy rasmini yuboring. Xira chiqsa, "fayl" sifatida yuboring.
2. Kartani tekshiring: ⛔ tuzatilmaguncha tasdiqlab bo'lmaydi, ⚠️ esa ogohlantirish.
3. Xato bo'lsa ✏️ bosib oddiy yozing: «2-qator soni 5, summasi 175000».
4. ✅ bosing. Kun oxirida `/csv`.

| Buyruq | Vazifasi |
|---|---|
| `/navbat` | Tasdiqlangan, CSV kutayotganlar |
| `/csv` | Navbatdagilarni bitta CSV qilish |
| `/qayta` | Oxirgi CSV'ni qayta olish |
| `/kontragentlar` | Ro'yxat |
| `/kontragent Nomi \| INN \| Договор \| Izoh` | Qo'shish yoki yangilash (INN bo'yicha) |

## CSV formati

- `;` bilan ajratilgan, BOM bilan UTF-8, o'nli vergul. Ajratuvchi: `CSV_DELIMITER`. Ustunlar: `nakladnoy/csv_export.py`.
- Bir tovar qatori = bir CSV qatori; sarlavha maydonlari har qatorda takrorlanadi. `ID загрузки` bir hujjat qatorlarini guruhlaydi.
- `Number`, `Биржевая`, `Сумма в валюте`, `Дата последнего изменения` bo'sh qoladi. `Date` va `Дата вх.док-та` = nakladnoy sanasi.
- `Сумма документа` = qatorlar yig'indisi.

## Railway

1. Railway'da GitHub repozitoriyidan yangi loyiha yarating va unga PostgreSQL qo'shing.
2. Bot servisiga `.env.example` dagi o'zgaruvchilarni kiriting. `DATABASE_URL` uchun: `${{Postgres.DATABASE_URL}}`.
3. Kontragentlar `seed.sql` da, u git'ga kirmaydi. Bir marta Railway bazasiga yuklang (Postgres → Connect → public URL):
   `psql "<public URL>" -f seed.sql`. Avval bot bir marta ishga tushib, jadvallarni yaratgan bo'lishi kerak.

Bitta token bilan bir vaqtda faqat bitta bot ishlay oladi: Railway ishlayotganda `run.ps1` ni yoqmang.

## Texnik

- Gemini band bo'lsa (429/500/503), bot qayta urinadi, keyin `GEMINI_FALLBACK_MODEL` ga o'tadi (standart: `gemini-3.6-flash`).
- PostgreSQL 17 Windows xizmati emas: `%LOCALAPPDATA%\nakladnoy-bot` ichida (admin huquqisiz). Bosh parol: `pg_superuser.txt`. Zaxira nusxa uchun `pgsql\bin\pg_dump.exe`.
- Testlar: `pip install -r requirements-dev.txt`, keyin `.venv\Scripts\python.exe -m pytest` (`TEST_DATABASE_URL` bazasini tozalaydi).
