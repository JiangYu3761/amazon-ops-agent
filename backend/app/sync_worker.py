"""Restart-safe read-only sync runner hosted in the API process.

Database leases and configuration versions fence every publish; no HTTP request
thread waits for an Amazon report. A second API process can safely share the DB.
"""
import hashlib
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from psycopg.types.json import Jsonb

from .db import connection
from .data_automation import ACTIVE, ERRORS, SyncRequest, _json, enqueue_sync

logger=logging.getLogger(__name__)
DATASETS={'sp_sales':'sales','sp_inventory':'inventory','ads_sp':'advertising'}
FIELDS={
 'sales':{'sale_date','sku','asin','units_ordered','ordered_revenue','currency','orders','sessions','page_views',
          'buy_box_percentage','unit_session_percentage'},
 'advertising':{'report_date','sku','asin','campaign_id','campaign_name','ad_group_id','adgroup_id','ad_id',
     'entity_id','entity_name','impressions','clicks','spend','attributed_sales','attributed_orders',
     'currency','ad_type','report_grain','attribution_days','mature','advertised_sku','advertised_asin'},
 'inventory':{'snapshot_date','sku','asin','fulfillable','reserved','inbound','unsellable','inbound_eta','currency','observed_at','source_updated_at','last_updated_time'},
}
NUMERIC={'units_ordered','ordered_revenue','orders','sessions','page_views','buy_box_percentage','unit_session_percentage',
         'impressions','clicks','spend','attributed_sales','attributed_orders','attribution_days',
         'fulfillable','reserved','inbound','unsellable'}
INTEGERS={'units_ordered','orders','sessions','page_views','impressions','clicks','attributed_orders','attribution_days',
          'fulfillable','reserved','inbound','unsellable'}


class InvalidBatch(Exception):pass


def normalize_batch(source,mappings,job,batch):
    """Fail before any write if one normalized row escapes the requested scope."""
    if len(batch.rows)>100000 or len(batch.coverage)>100000:raise InvalidBatch('report too large')
    expected=DATASETS[source['provider']]
    allowed={}
    for mapping in mappings:allowed.setdefault(str(mapping['product_id']),set()).add(mapping['sku'])
    rows=[];seen={};cover={}
    for raw in batch.rows:
        pid=str(raw.get('product_id'));dataset=raw.get('dataset')
        try:day=date.fromisoformat(str(raw['day']))
        except (ValueError,KeyError):raise InvalidBatch('invalid business date') from None
        if pid not in allowed or dataset!=expected or not job['start_date']<=day<=job['end_date']:
            raise InvalidBatch('product or date mismatch')
        key=raw.get('natural_key')
        if not isinstance(key,str) or not key or len(key)>600 or any(ord(c)<32 for c in key):raise InvalidBatch('invalid row key')
        payload=raw.get('payload')
        if not isinstance(payload,dict):raise InvalidBatch('invalid payload')
        payload={k:v for k,v in payload.items() if k in FIELDS[dataset]}
        if payload.get('sku') not in allowed[pid]:raise InvalidBatch('SKU mismatch')
        if dataset!='inventory' and payload.get('currency')!=source['currency']:raise InvalidBatch('currency mismatch')
        field={'sales':'sale_date','advertising':'report_date','inventory':'snapshot_date'}[dataset]
        if payload.get(field) is not None and str(payload[field])!=day.isoformat():raise InvalidBatch('payload date mismatch')
        payload[field]=day.isoformat()
        payload['currency']=source['currency']
        for name in NUMERIC & payload.keys():
            value=payload[name]
            if value is None:continue
            try:number=Decimal(str(value))
            except (InvalidOperation,ValueError):raise InvalidBatch('invalid number') from None
            if not number.is_finite() or number<0 or number>Decimal('1e15'):raise InvalidBatch('invalid amount')
            if name in INTEGERS and number!=number.to_integral_value():raise InvalidBatch('invalid count')
        identity=(pid,dataset,day,key)
        canonical=_json(payload)
        if identity in seen:
            if seen[identity]!=canonical:raise InvalidBatch('conflicting duplicate row')
            continue
        seen[identity]=canonical
        rows.append(dict(product_id=pid,dataset=dataset,day=day,natural_key=key,payload=canonical))
        cover.setdefault((pid,dataset,day),False)
    for raw in batch.coverage:
        pid=str(raw.get('product_id'));dataset=raw.get('dataset')
        try:day=date.fromisoformat(str(raw['day']))
        except (ValueError,KeyError):raise InvalidBatch('invalid coverage date') from None
        if pid not in allowed or dataset!=expected or not job['start_date']<=day<=job['end_date']:
            raise InvalidBatch('coverage scope mismatch')
        if not isinstance(raw.get('complete'),bool):raise InvalidBatch('invalid coverage flag')
        complete=raw['complete']
        # Confirm every mapped SKU when certifying sales/inventory completeness.
        # Ads report coverage semantics are determined by the connector's report grain.
        if complete and dataset in {'sales','inventory'}:
            present={r['payload']['sku'] for r in rows if r['product_id']==pid and r['day']==day}
            complete=allowed[pid].issubset(present)
        cover[(pid,dataset,day)]=complete
    return rows,[dict(product_id=k[0],dataset=k[1],day=k[2],complete=v) for k,v in cover.items()]


def safe_checkpoint(value):
    if not isinstance(value,dict):raise InvalidBatch('invalid checkpoint')
    def visit(item):
        if isinstance(item,dict):
            for key,child in item.items():
                if any(s in str(key).lower() for s in ('access_token','refresh_token','client_secret','authorization','signed_url','download_url')):
                    raise InvalidBatch('credential in checkpoint')
                visit(child)
        elif isinstance(item,list):
            for child in item:visit(child)
        elif isinstance(item,str) and ('X-Amz-Signature=' in item or 'X-Amz-Credential=' in item):raise InvalidBatch('signed URL in checkpoint')
    visit(value)
    result=_json(value)
    if len(json.dumps(result))>32*1024*1024:raise InvalidBatch('checkpoint too large')
    return result


def schedule_due():
    # Reclaim expired jobs in a separate short transaction. Publishing locks the
    # connection before the job; never hold a job lock while acquiring connections.
    with connection() as conn:
        conn.execute("""UPDATE operating_sync_jobs SET status='retry_wait',error_code='worker_interrupted',
            lease_token=NULL,lease_expires_at=NULL,next_attempt_at=now()
            WHERE status='running' AND lease_expires_at<now()""")
        conn.commit()
    with connection() as conn:
        if not conn.execute('SELECT pg_try_advisory_xact_lock(723011122) held').fetchone()['held']:return 0
        # Leases are also renewed by an independent heartbeat while HTTP is busy.
        sources=conn.execute("""SELECT * FROM operating_connections WHERE enabled AND
            (next_sync_at IS NULL OR next_sync_at<=now()) ORDER BY next_sync_at NULLS FIRST
            FOR UPDATE SKIP LOCKED LIMIT 20""").fetchall()
        count=0
        from .amazon_connectors import credentials_status
        for source in sources:
            if not credentials_status(source)['configured']:
                conn.execute("UPDATE operating_connections SET last_error_code='credentials_missing',next_sync_at=now()+interval '5 minutes' WHERE id=%s",(source['id'],))
                continue
            enqueue_sync(conn,source,compensate=True);count+=1
        conn.commit();return count


def claim_job():
    with connection() as conn:
        row=conn.execute("""SELECT * FROM operating_sync_jobs WHERE status IN ('queued','waiting','retry_wait')
            AND next_attempt_at<=now() ORDER BY next_attempt_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not row:return None
        lease=uuid4()
        row=conn.execute("""UPDATE operating_sync_jobs SET status='running',lease_token=%s,
            lease_expires_at=now()+interval '120 seconds',started_at=COALESCE(started_at,now()) WHERE id=%s RETURNING *""",(lease,row['id'])).fetchone()
        conn.commit();return row


def _locked_job(conn,job):
    source=conn.execute('SELECT * FROM operating_connections WHERE id=%s FOR UPDATE',(job['connection_id'],)).fetchone()
    current=conn.execute('SELECT * FROM operating_sync_jobs WHERE id=%s FOR UPDATE',(job['id'],)).fetchone()
    if not current or current['status']!='running' or current['lease_token']!=job['lease_token']:return None,None
    if not current['lease_expires_at'] or current['lease_expires_at']<=datetime.now(timezone.utc):
        conn.execute("""UPDATE operating_sync_jobs SET status='retry_wait',error_code='worker_interrupted',
            lease_token=NULL,lease_expires_at=NULL,next_attempt_at=now() WHERE id=%s""",(job['id'],))
        return source,None
    if not source['enabled'] or source['version']!=job['connection_version']:
        conn.execute("UPDATE operating_sync_jobs SET status='cancelled',error_code='configuration_changed',finished_at=now(),lease_token=NULL WHERE id=%s",(job['id'],))
        return source,None
    return source,current


def _newer_ads_partitions(conn, job, receipt):
    # A late download of a cached report must not roll back a later observed revision.
    # Compare per partition so nonoverlapping dates in the same report still update.
    observed = receipt.get('ads_generated_at') or receipt.get('ads_requested_at')
    rows = conn.execute("""SELECT c.product_id,c.fact_date FROM operating_data_coverage c
        JOIN operating_sync_jobs j ON j.id=c.source_job_id
        WHERE c.connection_id=%s AND c.dataset='advertising' AND c.fact_date BETWEEN %s AND %s
          AND j.connection_version=%s
          AND COALESCE(j.checkpoint->>'ads_generated_at',j.checkpoint->>'ads_requested_at')::timestamptz
              > COALESCE(%s::timestamptz,'-infinity'::timestamptz)""",
        (job['connection_id'],job['start_date'],job['end_date'],job['connection_version'],observed)).fetchall()
    return {(str(row['product_id']),row['fact_date']) for row in rows}


def _advance_after_ads_backfill(conn, source, job):
    if source['provider']=='ads_sp' and job['requested_by'] is None:
        conn.execute("""UPDATE operating_connections SET next_sync_at=now()
            WHERE id=%s AND %s::date < (now() AT TIME ZONE time_zone)::date-1""",
            (source['id'],job['end_date']))


def publish_batch(job,batch):
    with connection() as conn:
        source,current=_locked_job(conn,job)
        if not current:conn.commit();return False
        mappings=conn.execute('SELECT product_id,sku,asin FROM operating_product_mappings WHERE connection_id=%s',(source['id'],)).fetchall()
        checkpoint=safe_checkpoint(batch.checkpoint)
        rows,coverage=normalize_batch(source,mappings,current,batch)
        digest=hashlib.sha256(json.dumps(_json({'rows':rows,'coverage':coverage}),sort_keys=True,separators=(',',':')).encode()).hexdigest()
        newly_published_count=0
        warnings = list(batch.warnings)
        if rows or coverage:
            artifact=conn.execute('''INSERT INTO operating_sync_artifacts(job_id,connection_id,content_sha256,normalized_rows,coverage)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(job_id,content_sha256) DO NOTHING RETURNING id''',
                (job['id'],source['id'],digest,Jsonb(_json(rows)),Jsonb(_json(coverage)))).fetchone()
            new_artifact = artifact is not None
            if artifact is None:
                artifact=conn.execute('SELECT id FROM operating_sync_artifacts WHERE job_id=%s AND content_sha256=%s',(job['id'],digest)).fetchone()
            if source['provider']=='ads_sp' and 'ads_as_of_day' in checkpoint:
                newer = _newer_ads_partitions(conn,current,checkpoint)
                if newer:
                    rows = [r for r in rows if (r['product_id'],r['day']) not in newer]
                    coverage = [c for c in coverage if (c['product_id'],c['day']) not in newer]
                    warnings.append('部分日期已有更新的广告报告，本次旧报告已归档。')
            if new_artifact:newly_published_count=len(rows)
            # Replace exactly the returned report partitions; retain prior reports
            # in artifacts, including rows removed by a later Amazon correction.
            for c in coverage:
                conn.execute('DELETE FROM operating_api_facts WHERE connection_id=%s AND product_id=%s AND dataset=%s AND fact_date=%s',
                    (source['id'],c['product_id'],c['dataset'],c['day']))
                conn.execute('''INSERT INTO operating_data_coverage VALUES(%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT(connection_id,product_id,dataset,fact_date) DO UPDATE SET complete=EXCLUDED.complete,source_job_id=EXCLUDED.source_job_id,fetched_at=now()''',
                    (source['id'],c['product_id'],c['dataset'],c['day'],c['complete'],job['id']))
            for row in rows:
                conn.execute('''INSERT INTO operating_api_facts VALUES(%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT(connection_id,product_id,dataset,fact_date,natural_key) DO UPDATE SET payload=EXCLUDED.payload,
                    source_job_id=EXCLUDED.source_job_id,artifact_id=EXCLUDED.artifact_id,fetched_at=now()''',
                    (source['id'],row['product_id'],row['dataset'],row['day'],row['natural_key'],Jsonb(row['payload']),job['id'],artifact['id']))
        if batch.pending:
            delay=max(5,min(900,int(batch.retry_after or 30)))
            conn.execute("""UPDATE operating_sync_jobs SET status='waiting',checkpoint=%s,warnings=%s,
                row_count=row_count+%s,next_attempt_at=now()+(%s * interval '1 second'),lease_token=NULL,lease_expires_at=NULL,error_code=NULL WHERE id=%s""",
                (Jsonb(checkpoint),Jsonb(_json(warnings)),newly_published_count,delay,job['id']))
        else:
            conn.execute("""UPDATE operating_sync_jobs SET status='succeeded',checkpoint=%s,warnings=%s,row_count=row_count+%s,
                finished_at=now(),lease_token=NULL,lease_expires_at=NULL,error_code=NULL WHERE id=%s""",(Jsonb(checkpoint),Jsonb(_json(warnings)),newly_published_count,job['id']))
            # Advance to the next overdue chunk or the ordinary recent window as soon
            # as a scheduled historical pull finishes, even on once-daily connections.
            conn.execute('UPDATE operating_connections SET last_success_at=now(),last_error_code=NULL WHERE id=%s',(source['id'],))
            _advance_after_ads_backfill(conn,source,current)
        conn.commit();return True


def fail_job(job,code='sync_failed',retryable=False,retry_after=30):
    code=code if code in ERRORS else 'sync_failed'
    with connection() as conn:
        source,current=_locked_job(conn,job)
        if not current:conn.commit();return
        attempt=current['attempt']+1
        retry=retryable and attempt<=6 and datetime.now(timezone.utc)-current['created_at']<timedelta(hours=24)
        delay=max(5,min(3600,max(int(retry_after or 30),2**attempt*10)))
        conn.execute("""UPDATE operating_sync_jobs SET status=%s,attempt=%s,error_code=%s,
             next_attempt_at=now()+(%s * interval '1 second'),finished_at=CASE WHEN %s THEN NULL ELSE now() END,
             lease_token=NULL,lease_expires_at=NULL WHERE id=%s""",('retry_wait' if retry else 'failed',attempt,code,delay,retry,job['id']))
        conn.execute('UPDATE operating_connections SET last_error_code=%s WHERE id=%s',(code,source['id']))
        if not retry:
            _advance_after_ads_backfill(conn,source,current)
        conn.commit()


def execute_job(job,collector=None):
    from .amazon_connectors import ConnectorError, collect_connection
    try:
        with connection() as conn:
            source=conn.execute('SELECT * FROM operating_connections WHERE id=%s',(job['connection_id'],)).fetchone()
            mappings=conn.execute('SELECT product_id,sku,asin FROM operating_product_mappings WHERE connection_id=%s',(job['connection_id'],)).fetchall()
        if datetime.now(timezone.utc)-job['created_at']>timedelta(hours=24):
            fail_job(job,'report_timeout');return
        if not source or source['version']!=job['connection_version'] or not source['enabled']:
            with connection() as conn:_locked_job(conn,job);conn.commit()
            return
        batch=(collector or collect_connection)(dict(source),[dict(m) for m in mappings],job['start_date'],job['end_date'],job['checkpoint'])
        publish_batch(job,batch)
    except ConnectorError as exc:
        fail_job(job,getattr(exc,'code','sync_failed'),getattr(exc,'retryable',False),getattr(exc,'retry_after',30))
    except InvalidBatch:
        fail_job(job,'invalid_report')
    except Exception as exc:
        # Exception text/HTTP bodies can carry credentials or signed URLs.
        logger.warning('Sync job stopped (%s)',type(exc).__name__)
        fail_job(job,'sync_failed')


class SyncWorker:
    def __init__(self):
        self.stop_event=threading.Event();self.identifier='api-'+str(os.getpid())+'-'+uuid4().hex[:12]
        self.active=None;self.thread=None;self.heartbeat_thread=None

    def heartbeat(self):
        while not self.stop_event.is_set():
            try:
                with connection() as conn:
                    conn.execute('''INSERT INTO operating_worker_heartbeat(worker_id,state) VALUES(%s,%s)
                        ON CONFLICT(worker_id) DO UPDATE SET updated_at=now(),state=EXCLUDED.state''',(self.identifier,'running' if self.active else 'ready'))
                    active=self.active
                    if active:conn.execute("UPDATE operating_sync_jobs SET lease_expires_at=now()+interval '120 seconds' WHERE id=%s AND lease_token=%s AND status='running' AND lease_expires_at>now()",active)
                    conn.execute("DELETE FROM operating_worker_heartbeat WHERE updated_at<now()-interval '1 day'")
                    conn.commit()
            except Exception as exc:logger.warning('Sync heartbeat unavailable (%s)',type(exc).__name__)
            self.stop_event.wait(5)

    def run(self):
        while not self.stop_event.is_set():
            try:
                schedule_due();job=claim_job()
                if job:
                    self.active=(job['id'],job['lease_token'])
                    try:execute_job(job)
                    finally:self.active=None
                    continue
            except Exception as exc:logger.warning('Sync scheduler unavailable (%s)',type(exc).__name__)
            self.stop_event.wait(3)

    def start(self):
        if os.getenv('SYNC_WORKER_ENABLED','true').lower()!='true':return
        self.thread=threading.Thread(target=self.run,name='amazon-sync',daemon=True)
        self.heartbeat_thread=threading.Thread(target=self.heartbeat,name='amazon-sync-heartbeat',daemon=True)
        self.heartbeat_thread.start();self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=5)
        if self.heartbeat_thread:self.heartbeat_thread.join(timeout=5)
        try:
            with connection() as conn:
                conn.execute('DELETE FROM operating_worker_heartbeat WHERE worker_id=%s',(self.identifier,));conn.commit()
        except Exception:pass
