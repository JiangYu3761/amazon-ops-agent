"""Reuse signed advertising evidence only inside the original account, entity, action and ACL."""
from datetime import date, datetime
import json
import re
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb

from .db import connection
from .workflow_auth import require_user


router = APIRouter(prefix='/api/products/{product_id}/ads-review-feedback', tags=['Advertising feedback'])
ENTITY = re.compile(r'profile:([1-9][0-9]{0,29})/(campaign|adgroup|keyword|target):([1-9][0-9]{0,29})', re.ASCII)
ACTION_ENTITIES = {
    '调整预算': {'campaign'},
    '调整竞价': {'adgroup', 'keyword', 'target'},
    '暂停投放': {'campaign', 'adgroup', 'keyword', 'target'},
}
RULE_VERSION = 'signed-pilot-v1'
MATCH = """s.product_id=%(product)s AND s.site=%(site)s AND s.id<>%(source)s
    AND s.page_id='ads' AND s.section_id='change'
    AND (%(admin)s OR s.owner_id=%(actor)s OR s.reviewer_id=%(actor)s)
    AND p.plan->'feedback'->'identity'=%(identity)s"""


def _identity(conn, source, lock=False):
    if source['section_id'] != 'change':
        return None, '此类广告单没有可核对的变更动作。'
    values = source['values']
    entity = ENTITY.fullmatch(str(values.get('entity_id', '')).strip())
    action = values.get('change_kind')
    if not entity:
        return None, '在投放调整申请中照录完整广告标识，例如 profile:123/target:456，并选择调整类型。'
    profile, entity_type, entity_id = entity.groups()
    if entity_type not in ACTION_ENTITIES.get(action, set()):
        return None, '该动作的具体对象或影响范围尚不能唯一定位；可继续登记原计划和观察结果。'
    product = conn.execute('SELECT marketplace,data_account_key FROM products WHERE id=%s'
                           + (' FOR SHARE' if lock else ''), (source['product_id'],)).fetchone()
    if not product or not product['data_account_key'] or product['marketplace'] != source['site']:
        return None, '请核对产品的数据店铺账号和广告单站点，再引用同一对象的复盘。'
    account = product['data_account_key']
    # Credentials are never read. An explicitly conflicting connected profile defeats a manual reference.
    profiles = conn.execute("""SELECT account_key,ads_profile_id FROM operating_connections
        WHERE provider='ads_sp' AND marketplace=%s AND NULLIF(ads_profile_id,'') IS NOT NULL
        AND (account_key=%s OR ads_profile_id=%s)""", (source['site'], account, profile)).fetchall()
    if any(r['account_key'] != account or r['ads_profile_id'] != profile for r in profiles):
        return None, '广告账户标识与此站点已登记的店铺连接不一致，请核对账号与 profile。'
    return dict(account_key=account, site=source['site'], profile_id=profile,
                entity_type=entity_type, entity_id=entity_id, action=action), ''


def _feedback(state, message, identity=None, reference=None, candidate_version=None):
    return dict(rule_version=RULE_VERSION, state=state, message=message, identity=identity,
                reference=reference, candidate_version=candidate_version, applied=False)


def _recommend(conn, source, actor, *, lock):
    identity, problem = _identity(conn, source, lock=lock)
    if not identity:
        return _feedback('identity_missing', problem)
    params = dict(product=source['product_id'], source=source['id'], site=source['site'],
                  identity=Jsonb(identity), actor=actor['id'], admin=actor['is_admin'])
    if lock:
        # Every new plan for this tuple serializes even when there are no matching rows yet.
        scope_key = json.dumps([str(source['product_id']), identity], ensure_ascii=False, sort_keys=True)
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (scope_key,))
        # Reopen/sign-off take UPDATE locks on these rows. Re-read after acquiring the shared locks.
        conn.execute("""SELECT r.id FROM ads_decision_reviews r JOIN ads_review_plans p ON p.source_id=r.source_id
            JOIN workbench_items s ON s.id=p.source_id WHERE """ + MATCH + ' ORDER BY r.id FOR SHARE OF r', params).fetchall()
    row = conn.execute("""SELECT s.id source_id,s.number,p.plan,p.basis,r.id review_id,r.status,r.version,
        r.assessment,sign.version signed_version,sign.actor_id signed_by,sign.payload signed_payload,s.reviewer_id
        FROM ads_review_plans p JOIN workbench_items s ON s.id=p.source_id
        LEFT JOIN ads_decision_reviews r ON r.source_id=p.source_id
        LEFT JOIN LATERAL (SELECT e.version,e.actor_id,e.payload FROM ads_review_events e
            WHERE e.review_id=r.id AND e.action='assess' ORDER BY e.version DESC LIMIT 1) sign ON TRUE
        WHERE """ + MATCH + """
        ORDER BY CASE WHEN r.status='open' AND sign.version IS NOT NULL THEN 0
                      WHEN r.status IS NULL OR r.status='open' THEN 1 ELSE 2 END,
                 p.recorded_at DESC,s.id DESC LIMIT 1""", params).fetchone()
    if not row:
        return _feedback('clear', '当前可见记录中没有同店铺、站点、广告对象及动作的前案。', identity)
    ref = dict(source_id=str(row['source_id']), number=row['number'],
               review_id=str(row['review_id']) if row['review_id'] else None,
               version=row['version'], signed_version=row['signed_version'])
    def result(state, message, candidate=None):
        return _feedback(state, message, identity, ref, candidate)

    today = datetime.now(ZoneInfo(source['business_timezone'])).date()
    if row['status'] != 'closed':
        if row['signed_version'] is not None:
            return result('hold', '前案已因补证或纠正重开。暂停重复建议，等待重新签署。')
        due = date.fromisoformat(row['plan']['review_date'])
        if today > due:
            return result('hold', '前案已过复核日，先补齐回执及成熟报告，再决定同对象的下一次调整。')
        return result('observe', '同一动作正在等候执行回查或独立复盘，当前仅记录观察。')
    a = row['assessment']
    if (not row['signed_version'] or row['signed_by'] != row['reviewer_id']
            or a != row['signed_payload'] or a.get('verification') != 'reviewer_attested'):
        return result('observe', '找不到可核对的独立签署版本，等待人工核验。')
    if not a.get('guardrail_ok') or a.get('effect_direction') == 'negative':
        return result('hold', '前案经营护栏触线或存在负向实验结果，先核查止损与纠正，暂停重复建议。')
    if a.get('effect_direction') == 'no_detected_effect':
        return result('observe', '前案未检出效果，维持原范围，补充独立证据后再考虑复验。')
    if a.get('outcome') == 'ineffective':
        return result('hold', '前案评估无效，先查明原因再决定同一动作。')
    if a.get('next_review_date'):
        if today > date.fromisoformat(a['next_review_date']):
            return result('hold', '前案的跟踪复核已过期，暂停重复建议，待补数或重新签署。')
        return result('observe', '前案仍有下次复核安排，待跟踪完成后再采纳。')
    if not (row['basis'] == 'prospective' and row['plan'].get('control_method') == 'randomized'
            and a.get('mature') and a.get('execution_ok') and a.get('incremental')
            and a.get('confidence') == 'high' and a.get('material_interference') in ('none','controlled')
            and a.get('outcome') == 'effective' and a.get('effect_direction') == 'positive' and a.get('control_result')):
        return result('observe', '前案仅支持观察或仍待补数，保留签署来源，继续收集独立证据。')
    token = f"{RULE_VERSION}:{row['review_id']}:{row['signed_version']}"
    used = conn.execute("""SELECT 1 FROM ads_review_plans WHERE plan->'feedback'->>'applied'='true'
                           AND plan->'feedback'->>'candidate_version'=%s LIMIT 1""", (token,)).fetchone()
    if used:
        return result('observe', '该签署版本已经用于一项后续试验，等待该试验独立复盘。')
    advice = result('candidate', '可采纳前案的签署版本，在同一指标和归因窗下安排新的随机对照复验。', token)
    values = row['plan'].get('source_values') or {}
    advice['calibration'] = dict(metric_basis=row['plan']['metric_basis'], attribution_days=row['plan']['attribution_days'],
                                 control_method='randomized', previous_expected=row['plan']['expected'],
                                 original_setting=values.get('previous_setting'), tested_setting=values.get('proposed_setting'))
    return advice


def freeze_feedback(conn, source, actor, basis, body):
    """Called inside the original plan transaction with its source and request receipt locked."""
    advice = _recommend(conn, source, actor, lock=True)
    if body.calibration_version is not None:
        if (basis != 'prospective' or advice['state'] != 'candidate'
                or advice['candidate_version'] != body.calibration_version):
            raise HTTPException(409, '该校准候选已失效、已被采纳或不适用于此单。请刷新前案依据。')
        calibration = advice['calibration']
        if any(getattr(body, field) != calibration[field] for field in ('metric_basis','attribution_days','control_method')):
            raise HTTPException(422, '采纳前案校准须使用同一指标及归因窗，并登记本次独立随机对照。')
        advice['applied'] = True
    return advice


def _frozen_preview(conn, source, actor):
    saved = source['plan'].get('feedback') or _feedback('identity_missing', '此计划登记时尚未冻结可核对的广告对象。')
    current = {**saved, 'frozen': True}
    if not saved.get('applied'):
        return current
    ref = saved['reference']
    identity, _ = _identity(conn, source)
    row = None
    if identity == saved['identity']:
        row = conn.execute("""SELECT r.status,r.assessment,s.reviewer_id,
            e.version signed_version,e.actor_id,e.payload FROM ads_decision_reviews r
            JOIN ads_review_plans p ON p.source_id=r.source_id JOIN workbench_items s ON s.id=p.source_id
            LEFT JOIN LATERAL (SELECT version,actor_id,payload FROM ads_review_events
                WHERE review_id=r.id AND action='assess' ORDER BY version DESC LIMIT 1) e ON TRUE
            WHERE r.id=%s AND s.id=%s AND s.product_id=%s AND s.site=%s AND s.page_id='ads' AND s.section_id='change'
            AND p.plan->'feedback'->'identity'=%s AND (%s OR s.owner_id=%s OR s.reviewer_id=%s)""",
            (ref['review_id'],ref['source_id'],source['product_id'],source['site'],Jsonb(identity),
             actor['is_admin'],actor['id'],actor['id'])).fetchone()
    if not row:
        return {**current, 'reference': None, 'candidate_version': None, 'calibration': None,
                'evidence_status': 'unavailable', 'evidence_message': '当前范围无法核验前案，请联系复核人。'}
    if (row['status'] == 'closed' and row['signed_version'] == ref['signed_version']
            and row['actor_id'] == row['reviewer_id'] and row['payload'] == row['assessment']):
        return {**current, 'evidence_status': 'current', 'evidence_message': '原签署版本仍有效。'}
    message = ('原证据已修订，执行前请重新核对。' if source['status'] == 'approved'
               else '原证据已有修订，本单保留执行当时采用的版本。')
    return {**current, 'evidence_status': 'revised', 'evidence_message': message}


@router.get('/sources/{source_id}')
def preview(product_id: UUID, source_id: UUID, actor=Depends(require_user)):
    with connection() as conn:
        source = conn.execute("""SELECT s.*,p.plan FROM workbench_items s
            LEFT JOIN ads_review_plans p ON p.source_id=s.id
            WHERE s.product_id=%s AND s.id=%s AND s.page_id='ads'
            AND (%s OR s.owner_id=%s OR s.reviewer_id=%s)""",
            (product_id, source_id, actor['is_admin'], actor['id'], actor['id'])).fetchone()
        if not source:
            raise HTTPException(404, '找不到此产品范围内可访问的广告单')
        if source['plan'] is not None:
            return _frozen_preview(conn, source, actor)
        return {**_recommend(conn, source, actor, lock=False), 'frozen': False}
