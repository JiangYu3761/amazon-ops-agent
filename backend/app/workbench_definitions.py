"""Versioned business form definitions shared by the API and browser."""
import json
import math
import os
from datetime import date
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

from .process_rules import safe_link


@lru_cache(maxsize=1)
def pages():
    catalog = Path(os.getenv('CATALOG_FILE', Path(__file__).resolve().parents[2] / 'frontend' / 'catalog.json'))
    root = Path(os.getenv('WORKBENCH_DEFINITION_DIR', catalog.parent))
    result = {}
    for name in ('growth', 'operations', 'workspace'):
        for page in json.loads((root / f'workbench-{name}.json').read_text(encoding='utf-8')):
            if page['id'] in result:
                raise RuntimeError(f"重复工作台编号 {page['id']}")
            result[page['id']] = page
    return result


def page_definition(page_id):
    page = pages().get(page_id)
    if not page:
        raise HTTPException(422, '请选择有效的工作台页面')
    return page


def section_definition(page_id, section_id):
    for section in page_definition(page_id)['sections']:
        if section['id'] == section_id:
            return section
    raise HTTPException(422, '所选业务类型不属于该页面')


def validate_values(fields, values, submitting=False):
    definitions = {field['key']: field for field in fields}
    if values.keys() - definitions.keys():
        raise HTTPException(422, '填写内容含有本业务单未定义的字段')
    for key, field in definitions.items():
        value = values.get(key)
        missing = value is None or isinstance(value, str) and not value.strip()
        if missing:
            if submitting and field.get('required'):
                raise HTTPException(422, '请填写：' + field['label'])
            continue
        kind = field['type']
        if kind == 'number':
            valid = type(value) in (int, float) and abs(value) < 10**308 and math.isfinite(value)
            if valid:
                valid = field.get('min', value) <= value <= field.get('max', value)
        elif kind in ('text', 'textarea'):
            valid = isinstance(value, str) and len(value) <= 10000
        elif kind == 'date':
            try:
                valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
            except ValueError:
                valid = False
        elif kind == 'select':
            valid = isinstance(value, str) and value in field['options']
        elif kind == 'url':
            safe_link(value)
            valid = True
        else:
            valid = False
        if not valid:
            raise HTTPException(422, '填写格式或取值不正确：' + field['label'])
