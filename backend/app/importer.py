import csv
import hashlib
import json
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


TERM_FIELDS = ("profile_id", "ad_type", "campaign_id", "ad_group_id", "keyword_id", "match_type", "search_term")


def _term_metadata(source, item, line):
    if source.get("report_grain") != "search_term":
        raise ValueError("搜索词报表必须显式填写 report_grain=search_term")
    for key in TERM_FIELDS:
        value = source.get(key, "")
        if not value or not value.isprintable() or len(value) > (512 if key == "search_term" else 120):
            raise ValueError(f"搜索词维度 {key} 缺失或格式无效")
        item[key] = value
    item["ad_type"] = item["ad_type"].upper()
    item["match_type"] = item["match_type"].upper()
    if item["ad_type"] not in ("SP", "SB") or item["match_type"] not in ("EXACT", "PHRASE", "BROAD"):
        raise ValueError("当前支持 SP、SB 关键词搜索词，匹配方式为 EXACT、PHRASE 或 BROAD")
    item["attribution_days"] = _integer(source.get("attribution_days", ""), "attribution_days")
    if not 1 <= item["attribution_days"] <= 90 or not source.get("currency"):
        raise ValueError("请明确填写 1 至 90 天的归因窗口及报告币种")
    item.update(report_grain="search_term", source_line=line)
    dimensions = [item["date"].isoformat(), item["currency"], item["attribution_days"], *[item[k] for k in TERM_FIELDS]]
    item["business_key"] = hashlib.sha256(json.dumps(dimensions, ensure_ascii=False).encode()).hexdigest()


def _advertised_product_metadata(source, item, line):
    if source.get("report_grain") != "advertised_product" or source.get("ad_type", "").upper() != "SP":
        raise ValueError("商品广告日报须明确使用 report_grain=advertised_product、ad_type=SP")
    if not re.fullmatch(r"[0-9]{1,30}", source.get("profile_id", "")):
        raise ValueError("profile_id 须为来源广告账户的数字标识")
    ids = ("campaign_id", "ad_group_id", "ad_id", "sku")
    for field in ids:
        value = source.get(field, "")
        if not value or not value.isprintable() or len(value) > 120:
            raise ValueError(f"广告商品维度 {field} 缺失或格式无效")
        item[field] = value
    days = _integer(source.get("attribution_days", ""), "attribution_days")
    if not 1 <= days <= 90 or not source.get("currency"):
        raise ValueError("请明确填写原报告币种及 1 至 90 天的归因窗口")
    item.update(profile_id=source["profile_id"], report_grain="advertised_product", ad_type="SP",
                attribution_days=days, source_line=line, asin=source.get("asin") or None)
    for field in ("account_key", "marketplace"):
        if source.get(field):
            item[field] = source[field]
    dimensions = [item["date"].isoformat(), item["profile_id"], item["currency"], days, *[item[field] for field in ids]]
    item["business_key"] = hashlib.sha256(json.dumps(dimensions, ensure_ascii=False).encode()).hexdigest()


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
    term_file = import_type == "advertising" and "search_term" in headers
    product_file = (import_type == "advertising" and not term_file
                    and bool({"profile_id", "campaign_id", "ad_group_id", "ad_id"} & set(headers)))
    seen_terms = set()
    seen_products = set()
    seen_legacy = set()
    legacy_duplicate = None
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
                if term_file or source.get("report_grain") == "search_term":
                    term_file = True
                    _term_metadata(source, item, line)
                    if item["business_key"] in seen_terms:
                        raise ValueError("同一文件重复包含该搜索词投放源行，请先核对后再导入")
                    seen_terms.add(item["business_key"])
                elif product_file or source.get("report_grain") == "advertised_product":
                    product_file = True
                    _advertised_product_metadata(source, item, line)
                    if item["business_key"] in seen_products:
                        raise ValueError("同一文件重复包含该广告商品源行，请核对原始报表")
                    seen_products.add(item["business_key"])
                else:
                    key = (item["date"], item["campaign_name"], item["entity_name"])
                    if key in seen_legacy:
                        legacy_duplicate = line
                        continue
                    seen_legacy.add(key)
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
    if legacy_duplicate is not None:
        raise ValueError(f"第 {legacy_duplicate} 行与本文件同日、同活动名和实体名的记录冲突。请核对重复行，多个广告须按真实广告 ID 导入")
    if term_file and errors:
        raise ValueError(f"搜索词报表未导入：第 {errors[0]['line']} 行，{errors[0]['error']}")
    if product_file and (errors or any(row.get("report_grain") != "advertised_product" for row in rows)):
        if errors:
            raise ValueError(f"广告商品报表未导入：第 {errors[0]['line']} 行，{errors[0]['error']}")
        raise ValueError("同一广告商品 CSV 只能包含 advertised_product 粒度")
    return rows, errors
