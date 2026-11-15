"""Merchant normalisation and rule-based categorisation.

Unknown merchants are categorised by the LLM during the daily digest and the
result is cached in the `merchant_categories` table, so each merchant is only
ever sent once.  You can override any mapping with `finmon categorize`.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

CATEGORIES = [
    "groceries", "eating_out", "transport", "fuel", "shopping", "subscriptions",
    "utilities", "housing", "insurance", "health", "leisure", "travel", "education",
    "personal_care", "pets", "loans", "gifts_donations", "fees", "cash", "taxes", "bizum",
    "internal_transfer", "savings", "income", "other",
]

# Spanish display names for messages (category ids stay in English in code and commands).
CATEGORY_ES = {
    "groceries": "supermercado", "eating_out": "restaurantes y delivery", "transport": "transporte",
    "fuel": "gasolina", "shopping": "compras", "subscriptions": "suscripciones", "utilities": "suministros",
    "housing": "vivienda", "insurance": "seguros", "health": "salud", "leisure": "ocio", "travel": "viajes",
    "education": "formación", "personal_care": "cuidado personal", "pets": "mascotas", "loans": "préstamos",
    "gifts_donations": "regalos y donaciones",
    "fees": "comisiones", "cash": "efectivo", "taxes": "impuestos", "bizum": "bizum",
    "internal_transfer": "traspasos propios", "savings": "ahorro", "income": "ingresos", "other": "otros",
    "uncategorized": "sin categoría",
}

# keyword (matched against normalised merchant + description) -> category
RULES: list[tuple[str, str]] = [
    # groceries
    *[(k, "groceries") for k in ["mercadona", "carrefour", "lidl", "aldi", " dia ", "alcampo", "eroski",
                                  " consum ", "bonpreu", "condis", "hipercor", "supercor", "caprabo",
                                  "ahorramas", "el corte ingles super", "froiz", "gadis", " spar "]],
    # eating out / delivery
    *[(k, "eating_out") for k in ["glovo", "just eat", "uber eats", "deliveroo", "mcdonald", "burger king",
                                   "telepizza", "starbucks", "restaurante", "cafeteria", " bar ", "tgtg",
                                   "too good to go"]],
    # transport
    *[(k, "transport") for k in ["renfe", " metro", " emt ", "tmb", "cabify", " uber", "bolt", "free now",
                                  "freenow", "alsa", "bicing", "bicimad", "parking", "autopista", "peaje",
                                  "via-t", "iryo", "ouigo"]],
    *[(k, "fuel") for k in ["repsol", "cepsa", "galp", " bp ", "shell", "petronor", "ballenoil", "plenoil"]],
    # subscriptions
    *[(k, "subscriptions") for k in ["netflix", "spotify", "hbo", "max.com", "disney", "prime video",
                                      "amazon prime", "apple.com/bill", "icloud", "google one",
                                      "youtube premium", "anthropic", "claude.ai", "openai", "dazn",
                                      "movistar plus", "filmin", "gym", "basic-fit", " dir "]],
    # utilities / telco
    *[(k, "utilities") for k in ["iberdrola", "endesa", "naturgy", "holaluz", "repsol luz", "octopus",
                                  "canal de isabel", "aigues", "agua", "movistar", "vodafone", "orange",
                                  " digi ", "pepephone", " o2 ", "yoigo", "masmovil", "simyo", "lowi",
                                  "finetwork"]],
    *[(k, "insurance") for k in ["mapfre", "mutua", " axa ", "allianz", "sanitas", "adeslas", "dkv",
                                  "linea directa", "seguro"]],
    *[(k, "health") for k in ["farmacia", "clinica", "dentist", "hospital", "optica"]],
    *[(k, "shopping") for k in ["amazon", "aliexpress", "zara", "primark", "decathlon", "ikea",
                                 "el corte ingles", "mediamarkt", "fnac", "leroy merlin", "shein",
                                 "pull&bear", "bershka", "mango", "h&m", "uniqlo", " action "]],
    *[(k, "travel") for k in ["ryanair", "vueling", "iberia", "booking.com", "airbnb", "easyjet",
                                "hotel", "expedia"]],
    *[(k, "leisure") for k in [" cine", "teatro", "ticketmaster", "entradas", "steam", "playstation",
                                 "nintendo"]],
    # Generic Spanish wording and more chains
    *[(k, "groceries") for k in [" charter ", "bazar y alimentacion", "alimentacion", "mercado ", "supermercado",
                                  "fruteria", "panaderia", "carniceria"]],
    *[(k, "eating_out") for k in ["justeat", " bk", "pizzeria", "kebab", "sushi", "cerveceria", "taberna",
                                   "heladeria", "kfc", "domino's", "foster's hollywood", "vips", "100 montaditos"]],
    *[(k, "transport") for k in ["aparcamiento", "saba ", "empark"]],
    *[(k, "fuel") for k in ["estacion de servicio", "gasolinera"]],
    *[(k, "pets") for k in [" vet ", "veterinari", "kiwoko", "tiendanimal", "mascota"]],
    *[(k, "loans") for k in ["cuota de prestamo", "cuota prestamo"]],
    *[(k, "leisure") for k in ["loteria", "loterias", "yelmo", "cinesa", "kinepolis", "ocine"]],
    *[(k, "shopping") for k in ["media markt", "worten", "pccomponentes", "primor", "druni"]],
    *[(k, "personal_care") for k in ["peluqueria", "barberia", "estetica"]],
    *[(k, "cash") for k in ["cajero", "retirada", " atm "]],
    *[(k, "taxes") for k in ["aeat", "agencia tributaria", "hacienda", "ayuntamiento", " ibi "]],
    *[(k, "fees") for k in ["comision", "comisión", "intereses"]],
    ("bizum", "bizum"),
    *[(k, "housing") for k in ["alquiler", "hipoteca", "comunidad de propietarios", " rent "]],
    *[(k, "income") for k in ["nomina", "nómina", "salary", "payroll"]],
    *[(k, "savings") for k in ["cuenta naranja", "traspaso a cuenta naranja", "fondo naranja",
                                 "broker", "indexa", "myinvestor", "trade republic"]],
    *[(k, "internal_transfer") for k in ["traspaso"]],
]

# How Spanish banks commonly prefix movement texts.
_PREFIXES = re.compile(r"^(pago bizum en|pago en|compra en|compra tarj\.?|bizum recibido de|bizum enviado a|recibo)\s+")
# Trailing location noise in card payments, e.g. "NOODLE BAR SEVILLA ES".
_TRAILING_NOISE = {"es", "us", "ie", "gb", "fr", "de", "nl", "lu", "pt", "it", "madrid", "barcelona", "valencia",
                   "sevilla", "malaga", "bilbao", "zaragoza", "alicante", "murcia", "palma", "granada", "valladolid",
                   "vigo", "coruna", "gijon", "oviedo", "pamplona", "santander", "cordoba", "cadiz", "toledo"}
_CARD_NOISE = re.compile(r"\b(compra|pago|tarj(eta)?|card|en|recibo|transferencia)\b|\*+|\d{4,}|[\d/.:-]{6,}")


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalise_merchant(counterparty: str | None, description: str | None) -> str:
    """Produce a stable, lowercase key such as 'mercadona' or 'glovo app'."""
    if counterparty and re.fullmatch(r"[\d\s]+", counterparty):
        counterparty = None  # some banks put an account number there
    text = (counterparty or description or "").replace("¥", "Ñ")  # some banks mis-encode Ñ as ¥
    text = _strip_accents(text.lower()).strip()
    text = _PREFIXES.sub("", text)
    text = _CARD_NOISE.sub(" ", text)
    text = re.sub(r"[^a-z0-9&.' ]+", " ", text)
    words = text.split()
    while len(words) > 1 and words[-1] in _TRAILING_NOISE:
        words.pop()
    return " ".join(words[:4]) or "unknown"


def is_merchant_bizum(description: str | None) -> bool:
    """'Pago Bizum en CINESA' is a purchase in a shop paid with Bizum, not a payment to a person."""
    return _strip_accents((description or "").lower()).startswith("pago bizum en")


def rule_category(merchant: str, description: str | None, amount: float) -> str | None:
    haystack = f" {merchant} {_strip_accents((description or '').lower())} "
    for keyword, category in RULES:
        if keyword in haystack:
            if category == "income" and amount < 0:
                continue
            if category == "bizum" and is_merchant_bizum(description):
                continue
            return category
    return None


def categorise(conn: sqlite3.Connection, merchant: str, description: str | None, amount: float,
               bank_category: str | None = None) -> str | None:
    row = conn.execute("SELECT category FROM merchant_categories WHERE merchant = ?", (merchant,)).fetchone()
    if row:
        return row["category"]
    cat = rule_category(merchant, description, amount)
    if cat:
        return cat
    if bank_category:
        return bank_category
    if amount > 0:
        return "income"
    return None  # left for the LLM to decide


def recategorise_all(conn: sqlite3.Connection) -> int:
    """Re-apply categories to every transaction (after rules or mappings change)."""
    """Re-normalise merchants and re-apply categories (after rules or mappings change)."""
    for r in conn.execute("SELECT id, counterparty, description FROM transactions").fetchall():
        conn.execute("UPDATE transactions SET merchant = ? WHERE id = ?",
                     (normalise_merchant(r["counterparty"], r["description"]), r["id"]))
    rows = conn.execute(
        "SELECT id, merchant, description, amount FROM transactions "
        "WHERE COALESCE(category_source, 'auto') = 'auto'").fetchall()
    n = 0
    for r in rows:
        cat = categorise(conn, r["merchant"], r["description"], r["amount"])
        conn.execute("UPDATE transactions SET category = ? WHERE id = ?", (cat, r["id"]))
        n += 1
    conn.commit()
    return n


def set_merchant_category(conn: sqlite3.Connection, merchant: str, category: str, source: str) -> None:
    conn.execute(
        "INSERT INTO merchant_categories(merchant, category, source) VALUES(?, ?, ?) "
        "ON CONFLICT(merchant) DO UPDATE SET category = excluded.category, source = excluded.source "
        "WHERE merchant_categories.source != 'user' OR excluded.source = 'user'",
        (merchant, category, source),
    )
    conn.execute(
        "UPDATE transactions SET category = ? WHERE merchant = ? AND COALESCE(category_source, 'auto') = 'auto'",
        (category, merchant))


def is_person_transfer(category: str | None, description: str | None) -> bool:
    """Bizum and similar person-to-person payments, which need per-transaction interpretation."""
    if is_merchant_bizum(description):
        return False
    return category == "bizum" or "bizum" in (description or "").lower()
