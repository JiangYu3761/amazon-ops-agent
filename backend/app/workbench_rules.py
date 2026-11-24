"""Evaluate the business checks declared by the selected work form."""
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from operator import eq, ge, le, ne

COMPARE = {"lte": le, "gte": ge, "eq": eq, "neq": ne}
ARITY = {"lte": 2, "gte": 2, "eq": 2, "neq": 2, "required_if": 2,
         "block_if": 1, "deadline": 1, "ratio": 2, "difference": 2}


def blank(value):
    return value is None or isinstance(value, str) and not value.strip()


def typed(value, kind):
    if kind == "number":
        number = Decimal(str(value))
        if not number.is_finite() or abs(number.adjusted()) > 100:
            raise ValueError("数值超出计算范围")
        return number
    if kind == "date":
        return date.fromisoformat(str(value))
    return str(value).strip()


def display_number(value):
    with localcontext() as ctx:
        ctx.prec = max(32, value.adjusted() + 6)
        return format(value.quantize(Decimal("0.01")), "f").rstrip("0").rstrip(".") or "0"


def evaluate_checks(section, values, today=None):
    """Return evidence for a draft; only explicit invalid inputs block submission."""
    result = []
    fields = {field["key"]: field for field in section.get("fields", [])}
    for check in section.get("checks", []):
        op, keys = check["op"], check["keys"]
        raw = [values.get(key) for key in keys]
        message = check["message"]
        level, detail = "ok", message
        value = None
        if op == "required_if":
            if raw[0] == check["when"] and blank(raw[1]):
                level, detail = "block", message
            else:
                continue
        elif op == "block_if":
            if raw[0] == check["when"]:
                level = "block"
            else:
                continue
        elif any(blank(x) for x in raw):
            level = "missing"
            detail = "待补 " + "、".join(fields[k]["label"] for k, x in zip(keys, raw) if blank(x))
        else:
            try:
                nums = [typed(x, fields[k]["type"]) for k, x in zip(keys, raw)]
                if op in COMPARE:
                    level = "ok" if COMPARE[op](*nums) else "block"
                    if level == "ok":
                        detail = "所填两个字段符合条件"
                elif op == "deadline":
                    if today is None:
                        continue
                    value = (nums[0] - today).days
                    level = "overdue" if value < 0 else "due" if value == 0 else "info"
                    detail = (f"已过 {abs(value)} 天" if value < 0 else "今天到期" if value == 0 else f"还有 {value} 天") + "。" + message
                elif op in {"ratio", "difference"}:
                    if op == "ratio" and nums[1] == 0:
                        level, detail = "missing", "分母为零，比例无定义。" + message
                    else:
                        number = nums[0] / nums[1] * 100 if op == "ratio" else nums[0] - nums[1]
                        value = display_number(number)
                        detail = value + ("%" if op == "ratio" else "") + "。" + message
                else:
                    raise ValueError("未知规则")
            except (ValueError, TypeError, InvalidOperation, OverflowError):
                level, detail = "block", "字段格式或数值范围不符合要求，请检查 " + "、".join(fields[k]["label"] for k in keys)
        result.append({"label": check["label"], "level": level, "detail": detail, "value": value, "fields": keys})
    return result
