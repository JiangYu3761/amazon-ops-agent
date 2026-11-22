"""Product-scoped, read-only Amazon ingestion and deterministic analytics.

Credentials remain environment bindings. Canonical API facts are separate from
legacy CSV rows; only complete API partitions take precedence over CSV data.
"""
import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from psycopg.types.json import Jsonb

from .db import connection
from .workflow_auth import require_admin, require_user

router=APIRouter(prefix='/api')
ACTIVE=('queued','running','waiting','retry_wait')
DEFAULT_POLICY={'lead_time_days':None,'safety_days':None,'review_days':7,'moq':1,
                'pack_size':1,'target_acos':None,'inventory_max_age_days':2}
ERRORS={
 'credentials_missing':'服务器尚未配置所需 API 凭据',
 'authorization_failed':'平台授权失败，请检查应用、授权范围和账号',
 'rate_limited':'平台限流，系统将按计划重试',
 'upstream_unavailable':'平台服务暂不可用，系统将重试',
 'network_error':'无法连接平台，系统将重试',
 'invalid_report':'平台报告未通过字段或范围校验',
 'invalid_response':'平台响应未通过校验',
 'scope_mismatch':'报告账号、产品、币种或日期与请求不匹配',
 'configuration_changed':'连接配置已改变，旧任务停止发布数据',
 'report_failed':'平台未成功生成报告',
 'report_timeout':'等待平台报告超过时限',
 'sync_failed':'同步未完成，请检查服务器日志和授权状态',
 'worker_interrupted':'同步进程中断，等待自动接续',
 'cancelled':'任务已取消',
 'permission_denied':'平台拒绝访问，请检查应用权限和授权店铺',
 'authorization_expired':'平台授权已失效，请更新授权',
 'profile_not_authorized':'该广告 profile 不在已授权账号范围内',
 'profile_currency_mismatch':'广告账号币种与连接配置不一致，已停止写入',
 'profile_marketplace_mismatch':'广告账号站点与连接配置不一致，已停止写入',
 'profile_timezone_mismatch':'广告账号时区与连接配置不一致，已停止写入',
 'inventory_history_unavailable':'库存 API 不提供所请求日期的历史快照',
 'inventory_pagination_expired':'库存分页已过期，将重新完整扫描',
 'report_scope_mismatch':'平台报告的站点、日期或报表粒度与请求不匹配',
 'download_unavailable':'报告下载链接已过期，将重新获取',
 'request_rejected':'平台拒绝报告请求，请核对当前账号支持的能力和字段',
 'invalid_configuration':'连接配置无效，请核对站点、账号和时间参数',
 'currency_mismatch':'报告币种与产品币种不一致，已停止写入',
 'product_identity_mismatch':'报告的 SKU 或 ASIN 与产品映射不一致',
 'report_too_large':'报告超过安全处理上限，请缩短同步周期',
}


def now_utc():return datetime.now(timezone.utc)


def _json(value):
    return json.loads(json.dumps(value,default=lambda x:str(x) if isinstance(x,(date,datetime,UUID,Decimal)) else x,ensure_ascii=False))


def _audit(conn,actor,entity,identifier,action,before=None,after=None):
    conn.execute('''INSERT INTO audit_logs(actor_id,entity_type,entity_id,action,before_data,after_data)
        VALUES(%s,%s,%s,%s,%s,%s)''',(actor['id'],entity,str(identifier),action,
        Jsonb(_json(before)) if before is not None else None,Jsonb(_json(after)) if after is not None else None))


class MappingBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    product_id:UUID
    sku:str=Field(min_length=1,max_length=100)
    asin:str|None=Field(default=None,max_length=30)

    @field_validator('sku','asin')
    @classmethod
    def clean(cls,value):
        if value is None:return value
        value=value.strip()
        if not value or any(ord(c)<32 for c in value):raise ValueError('产品映射标识不能为空或包含控制字符')
        return value


class ConnectionBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=120)
    provider:Literal['sp_sales','sp_inventory','ads_sp']
    account_key:str=Field(min_length=1,max_length=80,pattern=r'^[A-Za-z0-9_.:-]+$')
    region:Literal['NA','EU','FE']='NA'
    marketplace:str=Field(default='US',pattern=r'^[A-Z]{2,4}$')
    marketplace_id:str=Field(default='ATVPDKIKX0DER',pattern=r'^[A-Z0-9]{5,30}$')
    currency:str=Field(default='USD',pattern=r'^[A-Z]{3}$')
    time_zone:str='America/Los_Angeles'
    env_prefix:str=Field(default='AMAZON',pattern=r'^[A-Z][A-Z0-9_]{0,39}$')
    ads_profile_id:str|None=Field(default=None,pattern=r'^\d{1,30}$')
    enabled:bool=False
    sync_interval_minutes:int=Field(default=60,ge=15,le=1440)
    lookback_days:int=Field(default=7,ge=1,le=31)
    mappings:list[MappingBody]=Field(min_length=1,max_length=500)

    @field_validator('time_zone')
    @classmethod
    def valid_zone(cls,value):
        try:ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError):raise ValueError('需要有效的 IANA 业务时区') from None
        return value

    @model_validator(mode='after')
    def valid_provider(self):
        if not self.name.strip():raise ValueError('连接名称不能为空')
        if self.provider=='ads_sp' and not self.ads_profile_id:raise ValueError('Ads 来源需要广告账号 profile ID')
        skus=[m.sku for m in self.mappings]
        if len(set(skus))!=len(skus):raise ValueError('一个连接内 SKU 不能重复映射')
        return self


class ConnectionUpdate(ConnectionBody):
    expected_version:int=Field(ge=1)


class SyncRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    start_date:date|None=None
    end_date:date|None=None


class PolicyBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    lead_time_days:int|None=Field(default=None,ge=0,le=730)
    safety_days:int|None=Field(default=None,ge=0,le=365)
    review_days:int=Field(default=7,ge=0,le=365)
    moq:int=Field(default=1,ge=1,le=1000000)
    pack_size:int=Field(default=1,ge=1,le=1000000)
    target_acos:Decimal|None=Field(default=None,ge=0,le=1000)
    inventory_max_age_days:int=Field(default=2,ge=0,le=90)


class PolicyUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version:int=Field(ge=0)
    policy:PolicyBody


def _product(conn,identifier,lock=False):
    value=conn.execute('SELECT * FROM products WHERE id=%s'+(' FOR UPDATE' if lock else ''),(identifier,)).fetchone()
    if not value:raise HTTPException(404,'Product not found')
    return value


def _mappings(conn,identifier):
    return conn.execute('SELECT product_id,sku,asin FROM operating_product_mappings WHERE connection_id=%s ORDER BY sku',(identifier,)).fetchall()


def _credentials(row):
    from .amazon_connectors import credentials_status
    return credentials_status(row)


def _public_job(row):
    keys=('id','connection_id','connection_version','status','start_date','end_date','attempt','next_attempt_at',
          'row_count','created_at','started_at','finished_at')
    result={k:row.get(k) for k in keys}
    code=row.get('error_code')
    result['error_code']=code if code in ERRORS else ('sync_failed' if code else None)
    result['detail']=ERRORS.get(code,'')
    # Remote IDs, signed URLs, raw reports and exception bodies stay server-side.
    result['warning_count']=len(row.get('warnings') or [])
    return result


def _public_connection(conn,row):
    result={k:row[k] for k in ('id','name','provider','account_key','region','marketplace','marketplace_id','currency',
        'time_zone','env_prefix','ads_profile_id','enabled','sync_interval_minutes','lookback_days','version',
        'next_sync_at','last_success_at','created_at','updated_at')}
    credentials=_credentials(row)
    result.update(credentials=credentials,mappings=_mappings(conn,row['id']))
    latest=conn.execute('SELECT * FROM operating_sync_jobs WHERE connection_id=%s ORDER BY created_at DESC,id DESC LIMIT 1',(row['id'],)).fetchone()
    code=row.get('last_error_code')
    result['last_error']=ERRORS.get(code,'同步未完成' if code else None)
    result['status']=('disabled' if not row['enabled'] else 'not_configured' if not credentials['configured'] else
       latest['status'] if latest and latest['status'] in ACTIVE else 'failed' if code else
       'connected' if row['last_success_at'] else 'not_verified')
    result['latest_job']=_public_job(latest) if latest else None
    result['capability']='只读同步；尚未使用真实店铺授权验收' if not row['last_success_at'] else '只读报告同步'
    return result


def _validate_mappings(conn,body,identifier=None):
    # Product row locks also serialize mapping writes across different connections.
    products={}
    for pid in sorted({m.product_id for m in body.mappings},key=str):products[pid]=_product(conn,pid,True)
    for mapping in body.mappings:
        product=products[mapping.product_id]
        if product['currency']!=body.currency or product['marketplace'].upper()!=body.marketplace:
            raise HTTPException(422,'连接的站点和币种必须与映射产品一致')
        if product.get('business_timezone')!=body.time_zone:
            raise HTTPException(422,'连接业务时区必须与产品档案一致，不能混合报表日界线')
        if product.get('data_account_key') not in (None,body.account_key):
            raise HTTPException(409,'该产品已绑定其他店铺账号，请使用独立产品档案，避免历史数据混合')
        duplicate=conn.execute('''SELECT c.id FROM operating_product_mappings m
            JOIN operating_connections c ON c.id=m.connection_id
            WHERE m.product_id=%s AND c.provider=%s AND c.id<>%s LIMIT 1''',
            (mapping.product_id,body.provider,identifier or UUID(int=0))).fetchone()
        if duplicate:raise HTTPException(409,'同一产品的同类数据源只能绑定一个连接，避免重复统计')
        # An ASIN may be shared by several SKUs of one product, never two products.
        if mapping.asin and any(other.asin==mapping.asin and other.product_id!=mapping.product_id for other in body.mappings):
            raise HTTPException(422,'同一 ASIN 不能映射到不同产品')
    for pid in products:
        conn.execute('UPDATE products SET data_account_key=%s WHERE id=%s',(body.account_key,pid))


@router.get('/integrations/connections')
def list_connections(actor=Depends(require_user)):
    with connection() as conn:
        rows=conn.execute('SELECT * FROM operating_connections ORDER BY created_at,id').fetchall()
        jobs=conn.execute('SELECT * FROM operating_sync_jobs ORDER BY created_at DESC LIMIT 100').fetchall()
        heartbeats=conn.execute("SELECT worker_id,updated_at,state FROM operating_worker_heartbeat WHERE updated_at>now()-interval '30 seconds'").fetchall()
        return {'connections':[_public_connection(conn,row) for row in rows],
                'jobs':[_public_job(j) for j in jobs],'worker_available':bool(heartbeats),
                'worker_enabled':os.getenv('SYNC_WORKER_ENABLED','true').lower()=='true'}


@router.post('/integrations/connections',status_code=201)
def create_connection(body:ConnectionBody,actor=Depends(require_admin)):
    config=body.model_dump(exclude={'mappings'})
    with connection() as conn:
        _validate_mappings(conn,body)
        if conn.execute('SELECT 1 FROM operating_connections WHERE provider=%s AND account_key=%s AND marketplace_id=%s',
                (body.provider,body.account_key,body.marketplace_id)).fetchone():raise HTTPException(409,'该店铺站点已有同类连接')
        fields=list(config)
        row=conn.execute('INSERT INTO operating_connections('+','.join(fields)+',created_by,updated_by,next_sync_at) VALUES('
            +','.join(['%s']*len(fields))+',%s,%s,now()) RETURNING *',tuple(config.values())+(actor['id'],actor['id'])).fetchone()
        for mapping in body.mappings:
            conn.execute('INSERT INTO operating_product_mappings VALUES(%s,%s,%s,%s)',(row['id'],mapping.product_id,mapping.sku,mapping.asin))
        _audit(conn,actor,'operating_connection',row['id'],'created',after=body.model_dump())
        result=_public_connection(conn,row);conn.commit()
        return result


@router.put('/integrations/connections/{identifier}')
def update_connection(identifier:UUID,body:ConnectionUpdate,actor=Depends(require_admin)):
    with connection() as conn:
        previous=conn.execute('SELECT * FROM operating_connections WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
        if not previous:raise HTTPException(404,'Connection not found')
        if previous['version']!=body.expected_version:raise HTTPException(409,'配置已被修改，请重新加载')
        previous_mappings=_mappings(conn,identifier)
        if any(previous[k]!=getattr(body,k) for k in ('provider','account_key','marketplace_id','currency','time_zone','ads_profile_id','env_prefix','region')):
            if conn.execute('SELECT 1 FROM operating_api_facts WHERE connection_id=%s LIMIT 1',(identifier,)).fetchone():
                raise HTTPException(409,'已有数据的连接不能改变账号或报告口径；请停用并建立独立连接')
        _validate_mappings(conn,body,identifier)
        config=body.model_dump(exclude={'mappings','expected_version'})
        row=conn.execute('UPDATE operating_connections SET '+','.join(k+'=%s' for k in config)+
             ',version=version+1,updated_by=%s,updated_at=now(),next_sync_at=now(),last_error_code=NULL WHERE id=%s RETURNING *',
             tuple(config.values())+(actor['id'],identifier)).fetchone()
        conn.execute("UPDATE operating_sync_jobs SET status='cancelled',error_code='configuration_changed',finished_at=now(),lease_token=NULL WHERE connection_id=%s AND status=ANY(%s)",
                     (identifier,list(ACTIVE)))
        conn.execute('DELETE FROM operating_product_mappings WHERE connection_id=%s',(identifier,))
        for m in body.mappings:conn.execute('INSERT INTO operating_product_mappings VALUES(%s,%s,%s,%s)',(identifier,m.product_id,m.sku,m.asin))
        _audit(conn,actor,'operating_connection',identifier,'updated',before={**previous,'mappings':previous_mappings},after=body.model_dump())
        result=_public_connection(conn,row);conn.commit();return result


def enqueue_sync(conn,source,body=None,actor=None):
    body=body or SyncRequest()
    if not source['enabled']:raise HTTPException(409,'请先启用此数据连接')
    if not _credentials(source)['configured']:raise HTTPException(409,{'code':'credentials_missing','message':ERRORS['credentials_missing']})
    existing=conn.execute('SELECT * FROM operating_sync_jobs WHERE connection_id=%s AND status=ANY(%s) ORDER BY created_at LIMIT 1',
                          (source['id'],list(ACTIVE))).fetchone()
    if existing:
        conn.execute("UPDATE operating_connections SET next_sync_at=now()+(sync_interval_minutes * interval '1 minute') WHERE id=%s",(source['id'],))
        return existing
    today=now_utc().astimezone(ZoneInfo(source['time_zone'])).date()
    end=body.end_date or (today if source['provider']=='sp_inventory' else today-timedelta(days=1))
    start=body.start_date or (end if source['provider']=='sp_inventory' else end-timedelta(days=source['lookback_days']-1))
    if start>end or (end-start).days>=31 or end>today:raise HTTPException(422,'同步窗口必须是过去或今天的连续 1–31 天')
    if source['provider']=='sp_inventory' and (start!=today or end!=today):raise HTTPException(422,'库存 API 只提供当前快照，不生成历史库存')
    row=conn.execute('''INSERT INTO operating_sync_jobs(connection_id,connection_version,start_date,end_date,requested_by)
        VALUES(%s,%s,%s,%s,%s) RETURNING *''',(source['id'],source['version'],start,end,actor['id'] if actor else None)).fetchone()
    conn.execute("UPDATE operating_connections SET next_sync_at=now()+(sync_interval_minutes * interval '1 minute') WHERE id=%s",(source['id'],))
    if actor:_audit(conn,actor,'operating_sync_job',row['id'],'queued',after={'connection_id':source['id'],'start_date':start,'end_date':end})
    return row


@router.post('/integrations/connections/{identifier}/sync',status_code=202)
def request_sync(identifier:UUID,body:SyncRequest|None=None,actor=Depends(require_admin)):
    with connection() as conn:
        source=conn.execute('SELECT * FROM operating_connections WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
        if not source:raise HTTPException(404,'Connection not found')
        row=enqueue_sync(conn,source,body,actor);conn.commit();return _public_job(row)


@router.post('/integrations/jobs/{identifier}/retry',status_code=202)
def retry_sync(identifier:UUID,actor=Depends(require_admin)):
    with connection() as conn:
        old=conn.execute('SELECT * FROM operating_sync_jobs WHERE id=%s',(identifier,)).fetchone()
        if not old:raise HTTPException(404,'Sync job not found')
        source=conn.execute('SELECT * FROM operating_connections WHERE id=%s FOR UPDATE',(old['connection_id'],)).fetchone()
        row=enqueue_sync(conn,source,SyncRequest(start_date=old['start_date'],end_date=old['end_date']),actor)
        conn.commit();return _public_job(row)


@router.post('/integrations/jobs/{identifier}/cancel')
def cancel_sync(identifier:UUID,actor=Depends(require_admin)):
    with connection() as conn:
        row=conn.execute('SELECT * FROM operating_sync_jobs WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
        if not row:raise HTTPException(404,'Sync job not found')
        if row['status'] in ACTIVE:
            row=conn.execute("UPDATE operating_sync_jobs SET status='cancelled',error_code='cancelled',finished_at=now(),lease_token=NULL WHERE id=%s RETURNING *",(identifier,)).fetchone()
            _audit(conn,actor,'operating_sync_job',identifier,'cancelled')
        conn.commit();return _public_job(row)


@router.get('/products/{product_id}/analytics/policy')
def get_policy(product_id:UUID,actor=Depends(require_user)):
    with connection() as conn:
        _product(conn,product_id)
        row=conn.execute('SELECT * FROM operating_policies WHERE product_id=%s',(product_id,)).fetchone()
        return {'policy':{**DEFAULT_POLICY,**(row['policy'] if row else {})},'version':row['version'] if row else 0}


@router.put('/products/{product_id}/analytics/policy')
def set_policy(product_id:UUID,body:PolicyUpdate,actor=Depends(require_admin)):
    with connection() as conn:
        _product(conn,product_id,True)
        old=conn.execute('SELECT * FROM operating_policies WHERE product_id=%s',(product_id,)).fetchone()
        if (old['version'] if old else 0)!=body.expected_version:raise HTTPException(409,'计算配置已被修改，请重新加载')
        policy=_json(body.policy.model_dump());version=body.expected_version+1
        conn.execute('''INSERT INTO operating_policies(product_id,policy,version,updated_by) VALUES(%s,%s,%s,%s)
            ON CONFLICT(product_id) DO UPDATE SET policy=EXCLUDED.policy,version=EXCLUDED.version,updated_by=EXCLUDED.updated_by,updated_at=now()''',
            (product_id,Jsonb(policy),version,actor['id']))
        conn.execute('INSERT INTO operating_policy_history(product_id,version,policy,actor_id) VALUES(%s,%s,%s,%s)',(product_id,version,Jsonb(policy),actor['id']))
        _audit(conn,actor,'operating_policy',product_id,'updated',before=old,after={'policy':policy,'version':version})
        conn.commit();return {'policy':policy,'version':version}


def load_product_analytics(conn,product_id,start_date=None,end_date=None):
    from .analytics_engine import calculate_product_metrics
    product=_product(conn,product_id)
    today=now_utc().astimezone(ZoneInfo(product['business_timezone'])).date()
    end=end_date or today
    start=start_date or end-timedelta(days=27)
    if start>end or (end-start).days>=366 or end>today:raise HTTPException(422,'指标日期范围须为过去或今天的连续 1–366 天')
    records={}
    queries={
      'sales':('sales_daily','sale_date',start),
      'advertising':('ad_performance_daily','report_date',start),
      'inventory':('inventory_snapshots','snapshot_date',None),
      'costs':('cost_records','effective_date',None),
    }
    for dataset,(table,field,lower) in queries.items():
        sql='SELECT r.*,i.created_at source_updated_at FROM '+table+' r JOIN data_imports i ON i.id=r.import_id WHERE r.product_id=%s AND r.'+field+'<=%s'
        params=[product_id,end]
        if lower:sql+=' AND r.'+field+'>=%s';params.append(lower)
        rows=conn.execute(sql+' ORDER BY r.'+field+' LIMIT 100001',params).fetchall()
        if len(rows)>100000:raise HTTPException(413,'所选数据超过单次分析上限，请缩短日期范围')
        for row in rows:row['source']='csv'
        records[dataset]=rows
    # Inventory takes latest not-after-as_of per SKU in the engine, never one
    # global MAX date. Keep all cost versions for date-effective matching.
    coverage={kind:{'complete_days':[],'last_synced_at':None,'source':'csv'} for kind in queries}
    for kind,rows in records.items():
        stamps=[r['source_updated_at'].isoformat() for r in rows if r.get('source_updated_at')]
        if stamps:coverage[kind]['last_synced_at']=max(stamps)
    cover=conn.execute('''SELECT x.*,c.provider FROM operating_data_coverage x
        JOIN operating_connections c ON c.id=x.connection_id WHERE x.product_id=%s AND x.fact_date<=%s''',(product_id,end)).fetchall()
    authoritative={}
    for row in cover:
        kind=row['dataset'];key=(kind,row['fact_date'])
        authoritative[key]=True
        if row['complete']:
            if row['fact_date']>=start:coverage[kind]['complete_days'].append(row['fact_date'].isoformat())
        stamp=row['fetched_at'].isoformat()
        if not coverage[kind]['last_synced_at'] or stamp>coverage[kind]['last_synced_at']:coverage[kind]['last_synced_at']=stamp
        coverage[kind]['source']='api'
        if kind=='advertising' and row['provider']=='ads_sp':coverage[kind]['ad_types_complete']=['SP']
    api_rows=conn.execute('''SELECT * FROM operating_api_facts WHERE product_id=%s AND fact_date<=%s
        AND (dataset='inventory' OR fact_date>=%s) ORDER BY fact_date,natural_key LIMIT 100001''',(product_id,end,start)).fetchall()
    if len(api_rows)>100000:raise HTTPException(413,'API 记录超过单次分析上限，请缩短日期范围')
    # Choose one source per dated partition. The API partition may itself be
    # partial; its explicit coverage then prevents complete-period claims.
    # CSV and old API versions remain intact in their tables/artifact history.
    for kind in records:
        date_key=queries[kind][1]
        records[kind]=[r for r in records[kind] if not authoritative.get((kind,r[date_key]))]
    for row in api_rows:
        if not authoritative.get((row['dataset'],row['fact_date'])):continue
        payload=dict(row['payload'],product_id=str(product_id),source='api',source_id=str(row['source_job_id']),
                     source_updated_at=row['fetched_at'].isoformat())
        records[row['dataset']].append(payload)
    policy_row=conn.execute('SELECT * FROM operating_policies WHERE product_id=%s',(product_id,)).fetchone()
    tasks=conn.execute('''SELECT t.* FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id
         WHERE pw.product_id=%s ORDER BY t.sequence''',(product_id,)).fetchall()
    cases=conn.execute('''SELECT ac.task_id,ac.status,ac.stages,ac.submitted_at started_at,ac.completed_at
        FROM approval_cases ac JOIN tasks t ON t.id=ac.task_id
        JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE pw.product_id=%s''',(product_id,)).fetchall()
    case_by_task={str(r['task_id']):r for r in cases}
    for task in tasks:
        if str(task['id']) in case_by_task:task['approval_case']=case_by_task[str(task['id'])]
    events=conn.execute('SELECT * FROM operating_events WHERE product_id=%s ORDER BY start_date',(product_id,)).fetchall()
    ranks=conn.execute('''SELECT ro.*,tq.keyword,tq.marketplace,tq.postal_code,tq.device FROM rank_observations ro
         LEFT JOIN tracking_queries tq ON tq.id=ro.tracking_query_id
         WHERE ro.product_id=%s AND ro.observed_at>=%s AND ro.observed_at<%s ORDER BY ro.observed_at''',
         (product_id,datetime.combine(start,datetime.min.time(),ZoneInfo(product['business_timezone'])),
          datetime.combine(end+timedelta(days=1),datetime.min.time(),ZoneInfo(product['business_timezone'])))).fetchall()
    sources=[_public_connection(conn,r) for r in conn.execute('''SELECT DISTINCT c.* FROM operating_connections c
         JOIN operating_product_mappings m ON m.connection_id=c.id WHERE m.product_id=%s''',(product_id,)).fetchall()]
    result=calculate_product_metrics(dict(product),records,start,end,now=now_utc(),
        policies={**DEFAULT_POLICY,**(policy_row['policy'] if policy_row else {})},coverage=coverage,
        tasks=tasks,events=events,rankings=ranks,sources=_json(sources))
    result['sources']=sources
    result['policy_version']=policy_row['version'] if policy_row else 0
    result['record_counts']={k:len(v) for k,v in records.items()}
    return result


@router.get('/products/{product_id}/analytics')
def product_analytics(product_id:UUID,start_date:date|None=Query(None),end_date:date|None=Query(None),actor=Depends(require_user)):
    with connection() as conn:return load_product_analytics(conn,product_id,start_date,end_date)
