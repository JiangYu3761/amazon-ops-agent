"""Decision reviews for witnessed workbench executions; no external advertising writes."""
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .db import connection
from .workflow_auth import require_user


router = APIRouter(prefix='/api/products/{product_id}/ads-reviews', tags=['Advertising decision reviews'])
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
OptionalNote = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
# Keep data only within the original product and the source case's authorized actors.
SCOPE = """s.product_id=%(product)s AND s.page_id='ads'
    AND (s.section_id IN ('change','framework') OR (s.section_id='query_harvest'
         AND s.values->>'query_action' IN ('建立精准承接','精准否定单一查询','验证承接后否定来源')))
    AND (%(admin)s OR s.owner_id=%(actor)s OR s.reviewer_id=%(actor)s)"""
SOURCE_JOINS = """FROM workbench_items s
    JOIN app_users ow ON ow.id=s.owner_id LEFT JOIN app_users rv ON rv.id=s.reviewer_id
    LEFT JOIN ads_review_plans p ON p.source_id=s.id"""
REVIEW_SELECT = """SELECT r.*, s.product_id,s.site,s.business_timezone,s.number source_number,
    s.title source_title,s.object_ref,s.owner_id,s.reviewer_id,ow.display_name owner_name,
    rv.display_name reviewer_name,p.basis,p.plan,p.recorded_at,
    EXISTS(SELECT 1 FROM ads_review_links l WHERE l.review_id=r.id AND l.kind='follow_up') has_follow_up """ + SOURCE_JOINS + """
    JOIN ads_decision_reviews r ON r.source_id=s.id WHERE """ + SCOPE


class Request(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID


class Plan(Request):
    expected: Note
    baseline: Note
    evidence: Note
    review_date: date
    observation_end: date
    attribution_days: int = Field(ge=0, le=90, strict=True)
    metric_basis: Literal['attributed','traffic','operational'] = 'attributed'
    control_method: Literal['none', 'matched', 'randomized'] = 'none'
    control_evidence: OptionalNote = ''
    calibration_version: str | None = Field(default=None,max_length=160)

    @model_validator(mode='after')
    def check_dates_and_control(self):
        if self.review_date < self.observation_end or self.observation_end.year >= 9999:
            raise ValueError('复核日期应在观察期结束当日或之后，请填写有效业务日期')
        if self.metric_basis=='attributed' and self.attribution_days==0:
            raise ValueError('广告归因转化须使用对应报表的正数归因窗口')
        if self.control_method != 'none' and not self.control_evidence:
            raise ValueError('有对照的计划须登记分组方法与凭证')
        return self


class Change(Request):
    expected_version: int = Field(ge=1, strict=True)


class Assessment(Change):
    as_of: date
    execution_ok: bool = Field(strict=True)
    guardrail_ok: bool = Field(strict=True)
    actual: Note
    interference: Note
    guardrails: Note
    evidence: Note
    outcome: Literal['effective','ineffective','inconclusive']
    incremental: bool = Field(strict=True)
    effect_direction: Literal['positive','negative','no_detected_effect'] | None = None
    control_result: OptionalNote = ''
    next_review_date: date | None = None
    refreshed_on: date | None = None
    material_interference: Literal['none','controlled','uncontrolled','unknown'] = 'unknown'
    confidence: Literal['low','moderate','high'] = 'low'
    stop_reason: OptionalNote = ''


class Reopen(Change):
    reason: Note


class Link(Change):
    kind: Literal['follow_up','strategy_candidate']
    item_id: UUID


def _scope(product, actor, **args):
    return dict(product=product, admin=actor['is_admin'], actor=actor['id'], **args)


def _today(zone):
    return datetime.now(ZoneInfo(zone)).date()


def _source(conn, product, source, actor):
    row = conn.execute('SELECT s.* FROM workbench_items s WHERE ' + SCOPE +
                       ' AND s.id=%(source)s FOR UPDATE OF s', _scope(product, actor, source=source)).fetchone()
    if not row:
        raise HTTPException(404, '找不到此产品范围内可办理的广告单')
    if actor['id'] != row['owner_id']:
        raise HTTPException(403, '由原广告单的经办人登记方案及发起复盘')
    return row


def _review(conn, product, review, actor, lock=False):
    result = conn.execute(REVIEW_SELECT + ' AND r.id=%(review)s' + (' FOR UPDATE OF r' if lock else ''),
                          _scope(product, actor, review=review)).fetchone()
    if not result:
        raise HTTPException(404, '找不到此产品范围内可访问的复盘')
    return result


def _replay(conn, actor, source_id, operation, body):
    message = [operation, str(source_id), body.model_dump(mode='json')]
    fingerprint = sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    new = conn.execute('''INSERT INTO ads_review_requests(actor_id,request_id,source_id,fingerprint)
        VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING request_id''',
        (actor['id'],body.request_id,source_id,fingerprint)).fetchone()
    if new:
        return False
    previous = conn.execute('SELECT fingerprint FROM ads_review_requests WHERE actor_id=%s AND request_id=%s',
                            (actor['id'], body.request_id)).fetchone()
    if previous['fingerprint'] != fingerprint:
        raise HTTPException(409, '同一请求编号已经用于不同内容')
    return True


def _fields(body):
    return body.model_dump(mode='json', exclude={'request_id','expected_version','calibration_version'})


def _event(conn, row, actor, action, payload):
    conn.execute('''INSERT INTO ads_review_events(review_id,version,actor_id,action,payload)
                    VALUES(%s,%s,%s,%s,%s)''',
                 (row['id'], row['version'], actor['id'], action, Jsonb(jsonable_encoder(payload))))


def _plan(conn, source, actor, body, basis):
    if basis == 'prospective' and body.observation_end < _today(source['business_timezone']):
        raise HTTPException(422, '事前方案的观察终日不能早于当前业务日')
    plan = dict(_fields(body), actor_id=str(actor['id']), owner_id=str(source['owner_id']),
                reviewer_id=str(source['reviewer_id']), source_version=source['version'],
                source_values=source['values'], business_timezone=source['business_timezone'])
    # Feedback revalidates the candidate against the current source while this transaction holds its lock.
    from .ad_review_feedback import freeze_feedback
    plan['feedback'] = freeze_feedback(conn,source,actor,basis,body)
    row = conn.execute('''INSERT INTO ads_review_plans(source_id,basis,plan) VALUES(%s,%s,%s)
                          RETURNING recorded_at''',(source['id'], basis, Jsonb(plan))).fetchone()
    return {**plan, 'recorded_at':row['recorded_at']}


def review_disposition(assessment, has_follow_up=False):
    """Compute current or as-of disposition from signed evidence and links visible at that time."""
    if not assessment:
        return None
    if assessment.get('stop_reason'):
        return 'stopped'
    if has_follow_up:
        return 'linked'
    if assessment.get('next_review_date'):
        return 'scheduled'
    if assessment.get('outcome') != 'effective' or not assessment.get('execution_ok') or not assessment.get('guardrail_ok'):
        return 'owner_pending'
    return 'not_required'


def _summary(row, actor):
    record = dict(row)
    disposition = review_disposition(row['assessment'], record.pop('has_follow_up'))
    plan = {**record['plan'], 'recorded_at':record.pop('recorded_at')}
    mature = date.fromisoformat(plan['observation_end']) + timedelta(days=plan['attribution_days'])
    participant = actor['id'] in (row['owner_id'],row['reviewer_id'])
    return dict(record, plan=plan, matures_on=mature, disposition=disposition, can_assess=row['status']=='open' and actor['id']==row['reviewer_id'],
                can_reopen=row['status']=='closed' and participant, can_link=participant)


def _detail(conn, row, actor):
    history = conn.execute('''SELECT e.version,e.action,e.actor_id,u.display_name actor_name,e.created_at,e.payload
        FROM ads_review_events e JOIN app_users u ON u.id=e.actor_id WHERE e.review_id=%s AND version<=%s
        ORDER BY version''',(row['id'],row['version'])).fetchall()
    links = conn.execute('''SELECT c.id,l.kind,c.number,c.title,c.owner_id,u.display_name owner_name,
            c.status,c.section_id,c.page_id FROM ads_review_links l JOIN workbench_items c ON c.id=l.item_id
            JOIN app_users u ON u.id=c.owner_id WHERE l.review_id=%s ORDER BY c.created_at,c.id''',
                         (row['id'],)).fetchall()
    return dict(_summary(row, actor), history=history, links=links)


@router.get('/sources')
def sources(product_id: UUID, limit: int = Query(50,ge=1,le=100), offset: int = Query(0,ge=0),
            actor=Depends(require_user)):
    with connection() as conn:
        rows = conn.execute("""SELECT s.id,s.number,s.title,s.site,s.object_ref,s.owner_id,s.reviewer_id,s.status,
            ow.display_name owner_name,rv.display_name reviewer_name,p.plan,p.recorded_at,r.id review_id,
            (s.owner_id=%(actor)s AND s.status='approved' AND p.source_id IS NULL) can_plan,
            (s.owner_id=%(actor)s AND r.id IS NULL AND s.status IN ('executed','closed') AND EXISTS
             (SELECT 1 FROM workbench_events e WHERE e.item_id=s.id AND e.action='execute' AND btrim(e.evidence)<>'')) can_start
            """ + SOURCE_JOINS + ' LEFT JOIN ads_decision_reviews r ON r.source_id=s.id WHERE ' + SCOPE + """
            AND s.status IN ('approved','executed','closed') ORDER BY s.updated_at DESC,s.id LIMIT %(limit)s OFFSET %(offset)s""",
            _scope(product_id, actor, limit=limit+1, offset=offset)).fetchall()
    for row in rows:
        recorded_at = row.pop('recorded_at')
        if row['plan']:
            row['plan']['recorded_at'] = recorded_at
    return dict(items=rows[:limit],next_offset=offset+limit if len(rows)>limit else None)


@router.get('')
def list_reviews(product_id: UUID,status: Literal['open','closed'] | None = None,
                 limit: int = Query(50,ge=1,le=100), offset: int = Query(0,ge=0), actor=Depends(require_user)):
    with connection() as conn:
        rows = conn.execute(REVIEW_SELECT + (' AND r.status=%(status)s' if status else '') +
             ' ORDER BY r.updated_at DESC,r.id LIMIT %(limit)s OFFSET %(offset)s',
             _scope(product_id, actor, status=status,limit=limit+1,offset=offset)).fetchall()
    return dict(items=[_summary(row, actor) for row in rows[:limit]],next_offset=offset+limit if len(rows)>limit else None)


@router.get('/{review_id}')
def detail(product_id: UUID, review_id: UUID, actor=Depends(require_user)):
    with connection() as conn:
        # Lock the review while reading its links and versioned history.
        row = _review(conn, product_id, review_id, actor, lock=True)
        return _detail(conn,row,actor)


@router.post('/sources/{source_id}/plan')
def prepare(product_id: UUID, source_id: UUID, body: Plan, actor=Depends(require_user)):
    with connection() as conn:
        source = _source(conn,product_id,source_id,actor)
        replay = _replay(conn,actor,source_id,'plan',body)
        existing = conn.execute('SELECT plan,recorded_at FROM ads_review_plans WHERE source_id=%s',(source_id,)).fetchone()
        if replay:
            return {'plan':dict(existing['plan'],recorded_at=existing['recorded_at'])}
        if source['status'] != 'approved' or existing:
            raise HTTPException(409, '事前登记需要已获批、尚未执行且没有已锁定方案的广告单')
        return {'plan':_plan(conn,source,actor,body,'prospective')}


@router.post('/sources/{source_id}/start')
def start(product_id: UUID, source_id: UUID, body: Plan | Request, actor=Depends(require_user)):
    with connection() as conn:
        source = _source(conn,product_id,source_id,actor)
        replay = _replay(conn,actor,source_id,'start',body)
        existing = conn.execute(REVIEW_SELECT+' AND s.id=%(source)s', _scope(product_id,actor,source=source_id)).fetchone()
        if replay:
            return _detail(conn,existing,actor)
        if existing:
            raise HTTPException(409,'此广告单已发起复盘，请打开原复盘')
        execution = conn.execute("""SELECT id event_id,actor_id,version,created_at,evidence,note,snapshot FROM workbench_events
            WHERE item_id=%s AND action='execute' AND btrim(evidence)<>'' AND actor_id=%s
            ORDER BY version DESC LIMIT 1""",(source_id,source['owner_id'])).fetchone()
        if source['status'] not in ('executed','closed') or not execution or not source['reviewer_id']:
            raise HTTPException(409, '请先在原广告单登记实际执行凭证，并保留独立审核人')
        has_plan = conn.execute('SELECT 1 FROM ads_review_plans WHERE source_id=%s',(source_id,)).fetchone()
        if has_plan and isinstance(body,Plan):
            raise HTTPException(422,'此单已有不可改写的事前方案，直接开始复盘即可')
        if not has_plan:
            if not isinstance(body,Plan):
                raise HTTPException(422,'缺少事前方案，请明确填写回溯观察依据')
            _plan(conn,source,actor,body,'retrospective')
        created = conn.execute('INSERT INTO ads_decision_reviews(source_id,execution) VALUES(%s,%s) RETURNING id,version',
                               (source_id,Jsonb(jsonable_encoder(execution)))).fetchone()
        _event(conn,created,actor,'start',{'execution_event_id':execution['event_id'],'source_version':source['version']})
        return _detail(conn,_review(conn,product_id,created['id'],actor),actor)


def _check_change(row, body):
    if row['version'] != body.expected_version:
        raise HTTPException(409,'复盘已经更新，请重新读取结果和版本')


def _check_assessment(row, body):
    today = _today(row['business_timezone'])
    executed = datetime.fromisoformat(row['execution']['created_at']).astimezone(ZoneInfo(row['business_timezone'])).date()
    end = date.fromisoformat(row['plan']['observation_end'])
    if body.as_of > today or body.as_of < executed:
        raise HTTPException(422,'实际数据截至日须在执行登记日至当前业务日之间')
    if body.stop_reason and body.next_review_date:
        raise HTTPException(422,'请在指定下次复核日期与终止观察之间选择一种处置')
    if body.next_review_date and body.next_review_date < body.as_of:
        raise HTTPException(422,'下次复核日期不能早于本次数据截至日')
    if body.refreshed_on and not body.as_of <= body.refreshed_on <= today:
        raise HTTPException(422,'报表刷新日期应在数据截至日与当前业务日之间')
    threshold = end + timedelta(days=row['plan']['attribution_days'])
    mature = bool(end >= executed and body.as_of >= threshold and body.refreshed_on and body.refreshed_on >= threshold)
    if body.outcome != 'inconclusive' and (not body.execution_ok or not mature):
        raise HTTPException(422,'执行失败、观察期未覆盖执行或归因尚未成熟时，请选择证据不足')
    if body.outcome != 'inconclusive' and (body.confidence=='low' or body.material_interference in ('unknown','uncontrolled')):
        raise HTTPException(422,'低置信度或未控制的干扰只能暂记证据不足')
    if body.material_interference=='controlled' and not body.control_result:
        raise HTTPException(422,'请记录干扰控制方法与结果')
    if body.outcome == 'effective' and not body.guardrail_ok:
        raise HTTPException(422,'护栏未通过，无法标记为有效')
    if body.incremental and (body.outcome=='inconclusive' or body.confidence!='high' or row['basis']!='prospective'
                             or row['plan']['control_method']!='randomized' or not body.control_result):
        raise HTTPException(422,'实验归因结论需要事前登记的随机对照、高置信的对照结果与成熟的定向判断')
    direction = body.effect_direction
    if body.incremental:
        if body.outcome=='effective':
            direction = direction or 'positive'
            if direction != 'positive':
                raise HTTPException(422,'标记有效的实验应提供正向对照结果')
        elif not direction:
            raise HTTPException(422,'请明确该实验观测到的效果方向或未检出差异')
    elif direction:
        raise HTTPException(422,'实验效果方向仅适用于具备可归因结论的实验')
    assessment = dict(_fields(body), effect_direction=direction, mature=mature, verification='reviewer_attested')
    assessment['disposition'] = review_disposition(assessment, row['has_follow_up'])
    return assessment


@router.post('/{review_id}/assess')
def assess(product_id: UUID, review_id: UUID, body: Assessment, actor=Depends(require_user)):
    with connection() as conn:
        row = _review(conn,product_id,review_id,actor,lock=True)
        if actor['id'] != row['reviewer_id']:
            raise HTTPException(403,'复盘结论须由原单的独立审核人签署')
        if _replay(conn,actor,row['source_id'],'assess',body):
            return _detail(conn,row,actor)
        _check_change(row,body)
        if row['status'] != 'open':
            raise HTTPException(409,'已签署复盘，修改前请登记重开原因')
        assessment = _check_assessment(row,body)
        saved = conn.execute("""UPDATE ads_decision_reviews SET assessment=%s,status='closed',
            version=version+1,updated_at=clock_timestamp() WHERE id=%s RETURNING id,version""",
            (Jsonb(assessment),review_id)).fetchone()
        _event(conn,saved,actor,'assess',assessment)
        return _detail(conn,_review(conn,product_id,review_id,actor),actor)


@router.post('/{review_id}/reopen')
def reopen(product_id: UUID, review_id: UUID, body: Reopen, actor=Depends(require_user)):
    with connection() as conn:
        row = _review(conn,product_id,review_id,actor,lock=True)
        if actor['id'] not in (row['owner_id'],row['reviewer_id']):
            raise HTTPException(403,'请由原单的经办人或审核人申请重开')
        if _replay(conn,actor,row['source_id'],'reopen',body):
            return _detail(conn,row,actor)
        _check_change(row,body)
        if row['status'] != 'closed':
            raise HTTPException(409,'仅已签署结论需要重开')
        saved = conn.execute("""UPDATE ads_decision_reviews SET assessment=NULL,status='open',
            version=version+1,updated_at=clock_timestamp() WHERE id=%s RETURNING id,version""",
            (review_id,)).fetchone()
        _event(conn,saved,actor,'reopen',{'reason':body.reason,'prior_assessment':row['assessment']})
        return _detail(conn,_review(conn,product_id,review_id,actor),actor)


@router.post('/{review_id}/links')
def add_link(product_id: UUID, review_id: UUID, body: Link, actor=Depends(require_user)):
    with connection() as conn:
        row = _review(conn,product_id,review_id,actor,lock=True)
        if actor['id'] not in (row['owner_id'],row['reviewer_id']):
            raise HTTPException(403,'请由原单的经办人或审核人登记后续工作')
        if _replay(conn,actor,row['source_id'],'link',body):
            return _detail(conn,row,actor)
        _check_change(row,body)
        child = conn.execute('''SELECT id,number,page_id,title,owner_id FROM workbench_items
            WHERE id=%s AND parent_id=%s AND product_id=%s AND site=%s FOR SHARE''',
            (body.item_id,row['source_id'],product_id,row['site'])).fetchone()
        if not child or (body.kind=='strategy_candidate' and child['page_id']!='ads'):
            raise HTTPException(422,'请选择原广告单在同产品、同站点下创建的后续单，策略候选需为广告工作单')
        prior = conn.execute('SELECT kind FROM ads_review_links WHERE review_id=%s AND item_id=%s',
                             (review_id,body.item_id)).fetchone()
        if prior:
            if prior['kind'] != body.kind:
                raise HTTPException(409,'该子单已经以其他类型关联')
            return _detail(conn,row,actor)
        conn.execute('INSERT INTO ads_review_links(review_id,item_id,kind) VALUES(%s,%s,%s)',
                     (review_id,body.item_id,body.kind))
        saved = conn.execute('''UPDATE ads_decision_reviews SET version=version+1,updated_at=clock_timestamp()
                                WHERE id=%s RETURNING id,version''',(review_id,)).fetchone()
        _event(conn,saved,actor,'link',dict(child,kind=body.kind))
        return _detail(conn,_review(conn,product_id,review_id,actor),actor)
