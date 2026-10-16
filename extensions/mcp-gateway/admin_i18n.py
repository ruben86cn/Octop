"""Translate trusted UI templates only; never translate user/token data."""

import json
import re
from pathlib import Path

TRANSLATIONS = json.loads(
    Path(__file__).with_name("admin-locales.json").read_text(encoding="utf-8")
)
PATTERN = re.compile(
    "|".join(re.escape(key) for key in sorted(TRANSLATIONS, key=len, reverse=True))
)


def locale_for(request):
    raw = request.query_params.get("lang") or request.headers.get("accept-language", "zh")
    language = raw.split(",")[0].split(";")[0].lower()
    if language.startswith("ja"):
        return "ja"
    if language.startswith("en"):
        return "en"
    return "zh"


def translate_template(text, locale):
    if locale == "zh":
        return text
    return PATTERN.sub(lambda m: TRANSLATIONS[m.group()][locale], text)


def render_admin(locale, nonce):
    text = Path(__file__).with_name("admin.html").read_text(encoding="utf-8")
    return translate_template(text, locale).replace("__NONCE__", nonce).replace("__LANG__", locale)
