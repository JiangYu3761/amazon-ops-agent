"""Calendar queues reconstructed from recorded events, intersected with current product access."""
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from .ads_reviews import SCOPE, _scope, review_disposition
from .db import connection
from .workflow_auth import require_user


router = APIRouter(prefix='/api/products/{product_id}/ads-review-periods', tags=['Advertising review periods'])

# Use the event timestamps (when the evidence was entered), not the assessment's
# business date. A later correction can refer to an older business date.
SOURCES = """
SELECT s.id AS source_id, src.snapshot->>'title' AS title,
       src.snapshot->>'owner_id' AS owner_id, src.snapshot->>'reviewer_id' AS reviewer_id,
       ow.display_name AS owner_name, rv.display_name AS reviewer_name,
       p.plan, CASE WHEN life.action IS NOT NULL THEN r.id END AS review_id,
       life.action, life.payload, life.created_at AS assessed_at
FROM workbench_items source
JOIN LATERAL (
    SELECT e.id FROM workbench_events e
    WHERE e.item_id=source.id AND e.action='execute' AND btrim(e.evidence)<>''
      AND e.snapshot->>'owner_id'=e.actor_id::text AND e.created_at<%(until)s
    ORDER BY e.version DESC LIMIT 1
) executed ON true
JOIN LATERAL (
    SELECT e.snapshot FROM workbench_events e WHERE e.item_id=source.id AND e.created_at<%(until)s
    ORDER BY e.version DESC LIMIT 1
) src ON true
CROSS JOIN LATERAL (
    SELECT source.id, source.product_id, source.page_id, source.section_id, source.owner_id,
           source.reviewer_id, src.snapshot->'values' AS values
) s
LEFT JOIN app_users ow ON ow.id=(src.snapshot->>'owner_id')::uuid
LEFT JOIN app_users rv ON rv.id=(src.snapshot->>'reviewer_id')::uuid
LEFT JOIN ads_review_plans p ON p.source_id=s.id AND p.recorded_at<%(until)s
LEFT JOIN ads_decision_reviews r ON r.source_id=p.source_id
LEFT JOIN LATERAL (
    SELECT e.action,e.payload,e.created_at FROM ads_review_events e
    WHERE e.review_id=r.id AND e.created_at<%(until)s AND e.action IN ('start','assess','reopen')
    ORDER BY e.version DESC LIMIT 1
) life ON true
WHERE """ + SCOPE

LINKS = """
SELECT DISTINCT ON (r.id, e.payload->>'id')
       s.id AS source_id, r.id AS review_id, e.payload->>'id' AS item_id,
       e.payload->>'kind' AS kind, e.created_at AS linked_at, child.created_at AS child_changed_at,
       child.snapshot->>'title' AS title, child.snapshot->>'owner_id' AS owner_id,
       ow.display_name AS owner_name, child.snapshot->>'status' AS status
FROM ads_decision_reviews r
JOIN workbench_items s ON s.id=r.source_id
JOIN ads_review_events e ON e.review_id=r.id AND e.action='link' AND e.created_at<%(until)s
JOIN workbench_items c ON c.id=(e.payload->>'id')::uuid AND c.product_id=s.product_id
JOIN LATERAL (
    SELECT x.snapshot,x.created_at FROM workbench_events x WHERE x.item_id=c.id AND x.created_at<%(until)s
    ORDER BY x.version DESC LIMIT 1
) child ON true
LEFT JOIN app_users ow ON ow.id=(child.snapshot->>'owner_id')::uuid
WHERE r.id=ANY(%(review_ids)s::uuid[]) ORDER BY r.id,e.payload->>'id',e.version"""

KIND = {
    'not_started':'有执行凭证待启动', 'open':'未结复盘', 'due':'已到复核日',
    'conclusion':'本期已签结论', 'insufficient':'本期证据不足', 'follow_up':'关联后续单',
    'strategy_candidate':'关联策略候选', 'next_review':'已到下次复核日', 'owner_pending':'原经办人待处置',
}
STATUS = {'not_started':'待启动', 'open':'待评估', 'closed':'已结', 'draft':'草稿',
          'pending_review':'待审核', 'approved':'已批准', 'executed':'已登记执行'}
OUTCOME = {'effective':'有效', 'ineffective':'无效', 'inconclusive':'证据不足'}
PRIORITY = ['owner_pending','due','next_review','not_started','insufficient','follow_up','open','conclusion','strategy_candidate']


def _item(kind, row, status, owner, reason, *, due=None, outcome=None, child=None):
    record_id = child or row['review_id'] or row['source_id']
    return dict(key=f'{kind}:{record_id}',kind=kind,kind_label=KIND[kind],
                source_id=row['source_id'],review_id=row['review_id'],item_id=child,
                title=row['title'],status=status,status_label=STATUS.get(status,status),
                owner_id=row.get(owner+'_id'),owner_name=row.get(owner+'_name'),due_date=due,
                conclusion=outcome,conclusion_label=OUTCOME.get(outcome),reason=reason)


def _metric(key, label, n, total, denominator):
    return {'key':key,'label':label,'n':n,'N':total,'denominator':denominator}


def _insufficient_reason(assessment):
    # Only report conditions present in the signed record, including legacy
    # assessments which do not have every newer evidence field.
    recorded = []
    if assessment.get('execution_ok') is False:
        recorded.append('执行核对未通过')
    if assessment.get('mature') is False:
        recorded.append('归因或观察窗口未成熟')
    if assessment.get('guardrail_ok') is False:
        recorded.append('经营护栏未通过')
    if assessment.get('confidence') == 'low':
        recorded.append('置信度低')
    if assessment.get('material_interference') in ('unknown','uncontrolled'):
        recorded.append('同期干扰未确定或未控制')
    return '独立复核人已签署证据不足。' + ('已记录：'+'、'.join(recorded)+'。' if recorded else '')


def _compile(sources, links, cutoff, since, period_timestamp, today):
    items = []
    started = open_dated = due_open = 0
    cohort_due = cohort_closed = signed = conclusive = 0
    known_reviews = set()
    # A calibration candidate is not a business follow-up. A link recorded later
    # cannot discharge the obligation in an earlier as-of view.
    handled = {link['review_id'] for link in links if link['kind']=='follow_up'}
    for r in sources:
        if not today:
            # Names can change independently of the event ledger. The recorded ID is historical.
            r = dict(r,owner_name=None,reviewer_name=None)
        plan = r['plan'] or {}
        due = date.fromisoformat(plan['review_date']) if plan.get('review_date') else None
        review_id = r['review_id']
        closed = review_id is not None and r['action']=='assess'
        # An executed source with a recorded deadline is already owed a review,
        # even if no one has started its review workflow.
        if due and since <= due <= cutoff:
            cohort_due += 1
            cohort_closed += int(closed)
        if due and not closed:
            open_dated += 1
            if due <= cutoff:
                due_open += 1
                if review_id is None:
                    items.append(_item('due',r,'not_started','owner','计划复核日已到，须先由经办人启动复盘。',due=due))
                else:
                    items.append(_item('due',r,'open','reviewer','计划复核日已到，仍无有效签署结论。',due=due))
        if review_id is None:
            items.append(_item('not_started',r,'not_started','owner','原变更单已登记执行凭证，截止日尚无复盘启动记录。',due=due))
            continue
        started += 1
        known_reviews.add(review_id)
        if not closed:
            items.append(_item('open',r,'open','reviewer','截止日最新复盘状态为待评估。',due=due))
            continue
        assessment = r['payload']
        outcome = assessment['outcome']
        if review_disposition(assessment,review_id in handled)=='owner_pending':
            items.append(_item('owner_pending',r,'closed','owner',
                        '已签结论需要处置，尚未登记下次复核、关联后续单或终止理由。',outcome=outcome))
        if r['assessed_at'] >= period_timestamp:
            signed += 1
            conclusive += int(outcome in ('effective','ineffective'))
            items.append(_item('conclusion',r,'closed','reviewer','按签署事件登记时间归入本期。',due=due,outcome=outcome))
            if outcome=='inconclusive':
                items.append(_item('insufficient',r,'closed','reviewer',_insufficient_reason(assessment),due=due,outcome=outcome))
        next_date = assessment.get('next_review_date')
        if next_date and date.fromisoformat(next_date)<=cutoff:
            items.append(_item('next_review',r,'closed','reviewer','已签结论登记了下次复核时间，截止日已到。',due=date.fromisoformat(next_date),outcome=outcome))
    linked_total = linked_open = candidates = 0
    for link in links:
        if link['review_id'] not in known_reviews:
            continue
        status,kind = link['status'],link['kind']
        # Carry unfinished hand-offs across periods. Count closed hand-offs only
        # when their association or latest child transition belongs to this period.
        if status=='closed' and max(link['linked_at'],link['child_changed_at']) < period_timestamp:
            continue
        linked_total += 1
        candidates += int(kind=='strategy_candidate')
        linked_open += int(status!='closed')
        if not today:
            link=dict(link,owner_name=None)
        reason=('已关联的候选工作单；是否发布须另有凭证。' if kind=='strategy_candidate'
                else '后续单状态与负责人取自截止日前的业务单事件。')
        items.append(_item(kind,link,status,'owner',reason,child=link['item_id']))
    metrics = [
        _metric('started','已启动复盘',started,len(sources),'截至截止日有经办人执行凭证的可见来源单'),
        _metric('due_open','已到期且未结',due_open,open_dated,'截至截止日已登记执行且有计划复核日的未签来源单，含待启动'),
        _metric('due_closed','本期到期单已结',cohort_closed,cohort_due,'已登记执行、计划复核日在本期起日至截止日内的来源单，含待启动'),
        _metric('conclusive','本期已签明确结论',conclusive,signed,'本期签署且截至截止日仍有效的复盘结论'),
        _metric('insufficient','本期已签证据不足',signed-conclusive,signed,'本期签署且截至截止日仍有效的复盘结论'),
        _metric('linked_open','未结关联单',linked_open,linked_total,'未结关联单与本期有变动的已结关联单'),
        _metric('candidates','关联策略候选',candidates,linked_total,'未结关联单与本期有变动的已结关联单'),
    ]
    items.sort(key=lambda x:(PRIORITY.index(x['kind']),x['due_date'] or date.max,x['key']))
    return items,metrics


@router.get('')
def periods(product_id: UUID, period: Literal['day','week','month']='day', as_of: date | None=None,
            limit: int=Query(50,ge=1,le=100), offset: int=Query(0,ge=0), actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        product=conn.execute('SELECT business_timezone FROM products WHERE id=%s',(product_id,)).fetchone()
        if not product:
            raise HTTPException(404,'此产品不存在')
        zone=ZoneInfo(product['business_timezone'])
        now=datetime.now(timezone.utc)
        today=now.astimezone(zone).date()
        cutoff=as_of or today
        if cutoff>today:
            raise HTTPException(422,'截止日不能晚于该产品的当前业务日')
        start=cutoff if period=='day' else (cutoff-timedelta(days=cutoff.weekday()) if period=='week' else cutoff.replace(day=1))
        since=datetime.combine(start,time.min,zone)
        until=min(now,datetime.combine(cutoff+timedelta(days=1),time.min,zone))
        params=_scope(product_id,actor,until=until)
        sources=conn.execute(SOURCES,params).fetchall()
        # The IDs already passed the shared ACL and the historical action eligibility
        # filter in this read-only snapshot. Later edits to a query action must not
        # add or remove its linked children from an earlier cutoff.
        visible_ids=[row['review_id'] for row in sources if row['review_id'] is not None]
        links=conn.execute(LINKS,dict(until=until,review_ids=visible_ids)).fetchall() if visible_ids else []
    items,metrics=_compile(sources,links,cutoff,start,since,cutoff==today)
    return dict(period=period,as_of=cutoff,period_start=start,period_end=cutoff,timezone=str(zone),
                scope='authorized_product_ads_sources',as_of_mode='event_history',
                scope_note='按产品业务时区和事件登记时间重建，以当前来源单权限限域；历史责任人仅显示记录的人员 ID。',
                metrics=metrics,items=items[offset:offset+limit],total=len(items),offset=offset,
                next_offset=offset+limit if offset+limit<len(items) else None)
