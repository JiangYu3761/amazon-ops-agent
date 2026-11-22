import csv
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation


SCHEMAS = {
    "sales": {
        "required": ["date", "sku", "units_ordered", "ordered_revenue"],
        "optional": ["asin", "currency"],
    },
    "advertising": {
        "required": ["date", "campaign_name", "impressions", "clicks", "spend", "sales", "orders"],
        "optional": ["entity_name", "currency"],
    },
    "inventory": {
        "required": ["date", "sku", "fulfillable", "reserved", "inbound", "unsellable"],
        "optional": ["asin"],
    },
    "costs": {
        "required": ["date", "sku", "product_cost", "first_mile_cost", "fba_fee", "referral_fee", "other_cost"],
        "optional": ["currency"],
    },
}


_UNSIGNED_INTEGER = r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)"
_INTEGER_PATTERN = re.compile(_UNSIGNED_INTEGER + r"\Z")
_DECIMAL_PATTERN = re.compile(r"\$?" + _UNSIGNED_INTEGER + r"(?:\.[0-9]+)?\Z")
_INT32_MAX = 2_147_483_647


def _integer(value: str, field: str) -> int:
    if not value:
        raise ValueError(f"{field} cannot be blank")
    if value.startswith('-'):
        raise ValueError(f"{field} cannot be negative")
    if not _INTEGER_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be an integer with valid thousands grouping")
    digits = value.replace(',', '').lstrip('0') or '0'
    # Check the lexical length before int() so enormous inputs cannot reach the DB.
    if len(digits) > 10 or (len(digits) == 10 and digits > str(_INT32_MAX)):
        raise ValueError(f"{field} exceeds the 32-bit integer range")
    return int(digits)


def _decimal(value: str, field: str, scale: int = 2) -> Decimal:
    if not value:
        raise ValueError(f"{field} cannot be blank")
    if value.lower().lstrip('$+-') in {'nan', 'snan', 'inf', 'infinity'}:
        raise ValueError(f"{field} must be finite")
    if value.startswith('-'):
        raise ValueError(f"{field} cannot be negative")
    if not _DECIMAL_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be a number with valid thousands grouping")
    canonical = value.removeprefix('$').replace(',', '')
    whole, _, fraction = canonical.partition('.')
    if len(fraction) > scale:
        raise ValueError(f"{field} cannot have more than {scale} decimal places")
    if len(whole.lstrip('0')) > 14 - scale:
        raise ValueError(f"{field} exceeds NUMERIC(14,{scale}) range")
    try:
        parsed = Decimal(canonical)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def parse_csv(import_type: str, raw: bytes, default_currency: str = 'USD') -> tuple[list[dict], list[dict]]:
    if import_type not in SCHEMAS:
        raise ValueError("unsupported import type")
    default_currency = default_currency.strip().upper() if isinstance(default_currency, str) else ''
    if not re.fullmatch(r'[A-Z]{3}', default_currency):
        raise ValueError('default_currency must be a three-letter currency code')
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    missing = [name for name in SCHEMAS[import_type]["required"] if name not in headers]
    if missing:
        raise ValueError("missing columns: " + ", ".join(missing))
    if len(set(headers)) != len(headers):
        raise ValueError("duplicate column names")
    rows, errors = [], []
    for line, source in enumerate(reader, start=2):
        if None in source:
            errors.append({"line":line,"error":"column count does not match header"})
            continue
        source = {(key or "").strip(): (value or "").strip() for key, value in source.items()}
        try:
            for field in SCHEMAS[import_type]['required']:
                if not source.get(field):
                    raise ValueError(f"{field} cannot be blank")
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', source['date']):
                raise ValueError('date must use YYYY-MM-DD')
            currency = (source.get('currency') or default_currency).upper()
            if not re.fullmatch(r'[A-Z]{3}', currency):
                raise ValueError('currency must be a three-letter currency code')
            item = {"date": date.fromisoformat(source["date"]), "currency": currency}
            if import_type == "sales":
                item.update(sku=source["sku"], asin=source.get("asin") or None,
                            units_ordered=_integer(source["units_ordered"], "units_ordered"),
                            ordered_revenue=_decimal(source["ordered_revenue"], "ordered_revenue"))
            elif import_type == "advertising":
                item.update(campaign_name=source["campaign_name"], entity_name=source.get("entity_name", ""),
                            impressions=_integer(source["impressions"], "impressions"),
                            clicks=_integer(source["clicks"], "clicks"), spend=_decimal(source["spend"], "spend"),
                            sales=_decimal(source["sales"], "sales"), orders=_integer(source["orders"], "orders"))
            elif import_type == "inventory":
                item.update(sku=source["sku"], asin=source.get("asin") or None,
                            fulfillable=_integer(source["fulfillable"], "fulfillable"),
                            reserved=_integer(source["reserved"], "reserved"), inbound=_integer(source["inbound"], "inbound"),
                            unsellable=_integer(source["unsellable"], "unsellable"))
            else:
                item.update(sku=source["sku"], product_cost=_decimal(source["product_cost"], "product_cost", scale=4),
                            first_mile_cost=_decimal(source["first_mile_cost"], "first_mile_cost", scale=4),
                            fba_fee=_decimal(source["fba_fee"], "fba_fee", scale=4),
                            referral_fee=_decimal(source["referral_fee"], "referral_fee", scale=4),
                            other_cost=_decimal(source["other_cost"], "other_cost", scale=4))
            if not item.get("sku", "x") or not item.get("campaign_name", "x"):
                raise ValueError("sku or campaign_name cannot be empty")
            rows.append(item)
        except (ValueError, KeyError) as exc:
            errors.append({"line": line, "error": str(exc)})
    return rows, errors
