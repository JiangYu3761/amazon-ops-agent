from datetime import date
from typing import Literal
from .deliverable_rules import delivery_catalog,build_delivery_rows

PageId=Literal['dashboard','deliverables','workflow','shop','listing','ads','inventory','calendar','finance','service','compliance','ranking','agent','imports','team','settings']
Scope=Literal['dashboard','deliverables','workflow','shop','listing','ads','inventory','calendar','finance','service','compliance','ranking','agent','imports','team','settings','global']
SCOPE_PREFIXES={'@交付清单':'deliverables','@资料':'deliverables','@全局':'global','@驾驶舱':'dashboard','@流程':'workflow','@店铺':'shop','@页面':'listing','@广告':'ads','@库存':'inventory','@活动':'calendar','@利润':'finance','@售后':'service','@合规':'compliance','@排名':'ranking','@建议':'agent','@导入':'imports','@团队':'team'}


def resolve_scope(message: str,current_page: PageId)->tuple[Scope,str]:
    stripped=message.strip()
    for prefix,scope in SCOPE_PREFIXES.items():
        if stripped.startswith(prefix):
            return scope,stripped[len(prefix):].strip()
    return current_page,stripped


def build_context(conn,product_id,scope:Scope,filters:dict|None=None)->dict:
    filters=filters or {}
    contexts={'scope':scope,'filters':filters,'as_of':date.today().isoformat()}
    contexts['product']=conn.execute('SELECT model,marketplace,currency,data_source FROM products WHERE id=%s',(product_id,)).fetchone()
    if scope in {'dashboard','global'}:
        contexts['dashboard']=conn.execute('''SELECT p.model,p.name,p.asin,p.marketplace,p.target_launch_date,p.current_stage,p.data_source,
            pp.completion_percent,pp.overdue_count,pp.blocked_count FROM products p JOIN product_progress pp ON pp.product_id=p.id WHERE p.id=%s''',(product_id,)).fetchone()
    task_scopes={'deliverables','workflow','shop','listing','ads','inventory','calendar','finance','service','compliance','team','agent','global'}
    if scope in task_scopes:
        clauses=['pw.product_id=%s'];params=[product_id]
        if scope in {'shop','listing','ads','inventory','calendar','finance','service','compliance'}:
            clauses.append('t.module_code=%s');params.append(scope)
        if scope!='global':
            if filters.get('module') not in {None,'all'} and scope=='workflow':
                clauses.append('t.module_code=%s');params.append(filters['module'])
            status=filters.get('status')
            if status=='overdue':clauses.append("t.due_date<CURRENT_DATE AND t.status NOT IN ('completed','not_applicable')")
            elif status=='unassigned':clauses.append('t.owner_id IS NULL')
            elif status in {'not_started','in_progress','pending_approval','completed','blocked','not_applicable'}:
                clauses.append('t.status=%s');params.append(status)
            if filters.get('owner_id'):
                clauses.append('t.owner_id::text=%s');params.append(filters['owner_id'])
            if filters.get('phase') not in {None,'all'}:
                clauses.append('t.phase=%s');params.append(filters['phase'])
            if filters.get('search') and scope!='deliverables':
                clauses.append("(t.title ILIKE %s OR COALESCE(t.department,'') ILIKE %s OR COALESCE(u.display_name,'') ILIKE %s)")
                params += ['%'+filters['search']+'%']*3
        contexts['tasks']=conn.execute('''SELECT t.id,t.sop_key,t.phase,t.module_code,t.stage_name,t.title,t.status,t.department,u.display_name owner,t.due_date,
            t.evidence_required,t.approval_required,t.acceptance_criteria FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id
            LEFT JOIN app_users u ON u.id=t.owner_id WHERE '''+' AND '.join(clauses)+' ORDER BY t.sequence LIMIT 300',params).fetchall()
    if scope in {'deliverables','global'}:
        documents=conn.execute('SELECT * FROM product_documents WHERE product_id=%s',(product_id,)).fetchall()
        evidence=conn.execute("SELECT e.task_id,e.uri,e.note FROM task_evidence e JOIN tasks t ON t.id=e.task_id JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE pw.product_id=%s AND e.uri ~* '^https?://'",(product_id,)).fetchall()
        cat=delivery_catalog()
        contexts['delivery_categories']=cat['delivery_categories']
        contexts['deliverables']=build_delivery_rows(contexts.get('tasks',[]),documents,evidence,cat,filters if scope!='global' else {})
        contexts['file_contents_read']=False
        if scope=='deliverables':
            contexts.pop('tasks',None)
    if scope in {'ranking','global'}:
        clauses=['ro.product_id=%s'];params=[product_id]
        if scope!='global':
            if filters.get('rank_type') in {'organic','sponsored','bsr'}:
                clauses.append('ro.observation_type=%s');params.append(filters['rank_type'])
            if filters.get('rank_keyword'):
                clauses.append('tq.keyword=%s');params.append(filters['rank_keyword'])
        contexts['ranking']=conn.execute('''SELECT tq.keyword,tq.marketplace,tq.postal_code,tq.device,ro.observation_type,
            ro.observed_at,ro.rank_value FROM rank_observations ro LEFT JOIN tracking_queries tq ON tq.id=ro.tracking_query_id
            WHERE '''+' AND '.join(clauses)+' ORDER BY ro.observed_at DESC LIMIT 300',params).fetchall()
    if scope in {'imports','global'}:
        contexts['imports']=conn.execute('SELECT import_type,filename,status,row_count,error_count,created_at FROM data_imports WHERE product_id=%s ORDER BY created_at DESC LIMIT 50',(product_id,)).fetchall()
    if scope in {'calendar','global'}:
        clauses=['product_id=%s'];params=[product_id]
        if scope!='global' and filters.get('month'):
            clauses.append("to_char(start_date,'YYYY-MM')=%s");params.append(filters['month'])
        contexts['events']=conn.execute('SELECT title,start_date,end_date,event_type,status,notes FROM operating_events WHERE '+' AND '.join(clauses)+' ORDER BY start_date',params).fetchall()
    if scope in {'ads','global'}:
        contexts['advertising']=conn.execute('SELECT report_date,campaign_name,entity_name,impressions,clicks,spend,attributed_sales,attributed_orders,currency FROM ad_performance_daily WHERE product_id=%s ORDER BY report_date DESC LIMIT 200',(product_id,)).fetchall()
    if scope in {'inventory','global'}:
        contexts['inventory']=conn.execute('SELECT * FROM inventory_snapshots WHERE product_id=%s ORDER BY snapshot_date DESC LIMIT 100',(product_id,)).fetchall()
    if scope in {'finance','global'}:
        contexts['costs']=conn.execute('SELECT * FROM cost_records WHERE product_id=%s ORDER BY effective_date DESC LIMIT 100',(product_id,)).fetchall()
        contexts['sales']=conn.execute('SELECT * FROM sales_daily WHERE product_id=%s ORDER BY sale_date DESC LIMIT 100',(product_id,)).fetchall()
    if scope=='settings':
        contexts['system']={'version':'0.4.0','external_amazon_apis':'not_connected','collector':'not_connected','rbac':'not_implemented','updater':'requires_docker_deployment_and_real_validation'}
    return contexts
