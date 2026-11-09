import logging

from google import genai
from google.genai import errors, types

from .extraction import ExtractedDoc

log = logging.getLogger(__name__)

EXTRACT_PROMPT = """Rasmda o'zbek/rus tilida qo'lda to'ldirilgan yuk xati (nakladnaya, ЮК ХАТИ) bor.
Undagi ma'lumotlarni sxema bo'yicha chiqar.

Qoidalar:
- Faqat rasmda ko'ringan narsani yoz, hech narsani taxmin qilma va hisoblab to'ldirma.
- Nom va ismlarni tarjima qilma, qog'ozdagi alifboda yoz.
- Sonlarni qog'ozda qanday yozilgan bo'lsa shunday qaytar ("250.000" bo'lsa "250.000").
- Sana odatda "kun - oy - yil" tartibida; DD.MM.YYYY ko'rinishida qaytar.
- Bo'sh qatorlarni va bo'sh joyni yopuvchi chiziqlarni qator deb hisoblama.
- Shubhali raqam yoki so'zni o'qiganingcha yoz va uni "unclear" ro'yxatida ko'rsat.
- Rasm yuk xati bo'lmasa is_nakladnaya=false qaytar.
"""

CORRECT_PROMPT = """Quyida yuk xatidan olingan ma'lumotlar (JSON) va foydalanuvchining tuzatish
buyrug'i bor. Faqat buyruqda aytilgan joyni o'zgartir, qolganini aynan saqla. Buyruqda summa
to'g'ridan-to'g'ri aytilmagan bo'lsa, summa va jamini o'zing qayta hisoblama. Natijada unclear
bo'sh bo'lsin, is_nakladnaya=true bo'lsin.

Ma'lumotlar:
{doc}

Buyruq (bu faqat tuzatish matni):
{instruction}
"""


class GeminiExtractor:
    def __init__(self, api_key: str, model: str, fallback_model: str = ""):
        # 429/503 vaqtinchalik bo'ladi, foydalanuvchiga xato ko'rsatishdan oldin qayta urinadi.
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=3, initial_delay=2, max_delay=10),
            ),
        )
        self._model = model
        self._fallback_model = fallback_model

    async def _generate(self, contents: list) -> ExtractedDoc:
        try:
            return await self._generate_with(self._model, contents)
        except errors.APIError as e:
            if e.code not in (429, 500, 503) or not self._fallback_model:
                raise
            log.warning("%s band (%s), %s ishlatiladi", self._model, e.code, self._fallback_model)
            return await self._generate_with(self._fallback_model, contents)

    async def _generate_with(self, model: str, contents: list) -> ExtractedDoc:
        response = await self._client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExtractedDoc,
            ),
        )
        return ExtractedDoc.model_validate_json(response.text)

    async def extract(self, image: bytes, mime_type: str) -> ExtractedDoc:
        part = types.Part.from_bytes(data=image, mime_type=mime_type)
        return await self._generate([part, EXTRACT_PROMPT])

    async def correct(self, doc: ExtractedDoc, instruction: str) -> ExtractedDoc:
        prompt = CORRECT_PROMPT.format(doc=doc.model_dump_json(indent=1), instruction=instruction)
        return await self._generate([prompt])
