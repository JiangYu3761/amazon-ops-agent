"""Read-only Amazon report/inventory collectors, with resumable report checkpoints.

No database or live scheduler is started by this module. Missing credential bindings
fail before network I/O. SP business sales are requested one day at SKU granularity.
Ads uses advertised-product rows only; campaign totals are never copied to products.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import io
import json
import os
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


@dataclass
class SyncBatch:
    rows: list
    coverage: list
    checkpoint: dict
    pending: bool = False
    retry_after: int = 0
    warnings: list = field(default_factory=list)


class ConnectorError(Exception):
    def __init__(self, code, detail, retryable=False, retry_after=0):
        self.code=code;self.detail=detail;self.retryable=bool(retryable);self.retry_after=int(retry_after)
        super().__init__(detail)


SP_HOSTS={'NA':'sellingpartnerapi-na.amazon.com','EU':'sellingpartnerapi-eu.amazon.com','FE':'sellingpartnerapi-fe.amazon.com'}
ADS_HOSTS={'NA':'advertising-api.amazon.com','EU':'advertising-api-eu.amazon.com','FE':'advertising-api-fe.amazon.com'}
TOKEN_URL='https://api.amazon.com/auth/o2/token'
MAX_COMPRESSED=32*1024*1024
MAX_UNCOMPRESSED=128*1024*1024
MAX_ROWS=50000
MAX_DAYS=31  # Internal bounded-job limit, not an assertion about Amazon's API limits.
_TOKEN_CACHE={}
_PROFILE_CACHE={}
_TOKEN_LOCK=threading.Lock()
_PROFILE_LOCK=threading.Lock()
ADS_WARNING='SP 商品粒度 v3 适配器尚需当前授权账户实机验收；7日归因窗口已过不代表平台不会继续回补。'


def _error(code,detail,retryable=False,retry_after=0):
    raise ConnectorError(code,detail,retryable,retry_after)


def _env_names(connection):
    prefix=connection.get('env_prefix') or 'AMAZON'
    if not isinstance(prefix,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,50}',prefix):
        _error('invalid_configuration','凭据环境变量前缀无效')
    family='ADS' if connection.get('provider')=='ads_sp' else 'SP_API'
    return [f'{prefix}_{family}_{suffix}' for suffix in ('CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN')]


def credentials_status(connection):
    names=_env_names(connection)
    missing=[name for name in names if not os.environ.get(name)]
    return {'configured':not missing,'required_env':names,'missing_env':missing}


def _instant(value):
    if callable(value):value=value()
    value=value or datetime.now(timezone.utc)
    if not isinstance(value,datetime):_error('invalid_configuration','同步时钟必须提供 datetime')
    if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _day(value):
    try:
        if isinstance(value,datetime):return value.date()
        if isinstance(value,date):return value
        parsed=date.fromisoformat(value)
        if parsed.isoformat()!=value:raise ValueError()
        return parsed
    except (ValueError,TypeError):_error('invalid_configuration','日期必须使用 YYYY-MM-DD')


def _retry_after(response,instant):
    value=response.headers.get('Retry-After','')
    try:return max(1,min(3600,int(value)))
    except ValueError:
        try:return max(1,min(3600,int((parsedate_to_datetime(value)-instant).total_seconds())))
        except (TypeError,ValueError):return 30


def _check_response(response,instant):
    status=response.status_code
    if status==429:_error('rate_limited','Amazon 请求受到限流，将按退避策略重试',True,_retry_after(response,instant))
    if status>=500:_error('upstream_unavailable','Amazon 服务暂不可用',True,30)
    if status==401:_error('authorization_expired','Amazon 授权失效，请重新授权')
    if status==403:_error('permission_denied','Amazon 拒绝访问，请检查应用、店铺或广告账户授权')
    if 300<=status<400:_error('unexpected_redirect','Amazon 返回了未允许的重定向')
    if status<200 or status>=300:_error('request_rejected','Amazon 拒绝了报告配置或请求，请核对当前账户支持的报表与字段')


def _json(response,instant,allow_list=False):
    _check_response(response,instant)
    try:
        value=response.json()
        if not isinstance(value,list if allow_list else dict):raise ValueError()
        return value
    except (ValueError,TypeError):_error('invalid_response','Amazon 返回了无法解析的数据')


def _integer(value,nullable=False):
    if value is None and nullable:return None
    if isinstance(value,bool) or not isinstance(value,(int,str)):_error('invalid_report','报表数量字段无效')
    try:
        if isinstance(value,str) and not re.fullmatch(r'\d+',value):raise ValueError()
        result=int(value)
        if result<0:raise ValueError()
        return result
    except (ValueError,TypeError):_error('invalid_report','报表数量字段无效')


def _money(value,nullable=False):
    if value is None and nullable:return None
    if isinstance(value,bool):_error('invalid_report','报表金额字段无效')
    try:
        amount=Decimal(str(value))
        if not amount.is_finite() or amount<0:raise ValueError()
        return str(amount)
    except (ValueError,InvalidOperation):_error('invalid_report','报表金额字段无效')


def _stable(value):
    if isinstance(value,bool) or not isinstance(value,(str,int)) or not str(value):_error('invalid_report','报表缺少稳定的实体标识')
    return str(value)


def _object(value):
    if not isinstance(value,dict):_error('invalid_report','报告对象字段格式无效')
    return value


def _remote_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}',value):
        _error('invalid_response','Amazon 报告或文档标识格式无效')
    return value


def _safe_download_url(value):
    try:
        parsed=urlsplit(value)
        host=parsed.hostname or ''
        allowed=(re.fullmatch(r'[a-z0-9][a-z0-9.-]*\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com',host)
                 or re.fullmatch(r's3(?:[.-][a-z0-9-]+)?\.amazonaws\.com',host))
        if parsed.scheme!='https' or not allowed or parsed.username or parsed.password or parsed.port not in (None,443) or parsed.fragment:
            raise ValueError()
        return value
    except (ValueError,TypeError,AttributeError):_error('unsafe_download_url','报告下载地址不属于允许的 Amazon HTTPS S3 域名')


class _Collector:
    def __init__(self,connection,mappings,start_date,end_date,checkpoint,client,instant):
        self.connection=connection;self.provider=connection.get('provider')
        if self.provider not in {'sp_sales','sp_inventory','ads_sp'}:_error('unsupported_provider','不支持的 Amazon 数据源')
        self.start=_day(start_date);self.end=_day(end_date);self.instant=instant
        if self.end<self.start or (self.end-self.start).days>=MAX_DAYS:_error('invalid_window','单个同步任务日期范围须为1至31天')
        try:self.zone=ZoneInfo(connection.get('time_zone') or 'UTC')
        except (ZoneInfoNotFoundError,TypeError):_error('invalid_configuration','账户业务时区无效')
        self.today=instant.astimezone(self.zone).date()
        if self.end>self.today:_error('invalid_window','同步日期不能晚于当前账户日期')
        region=connection.get('region')
        if region not in SP_HOSTS:_error('invalid_configuration','Amazon 区域必须为 NA、EU 或 FE')
        for key in ('account_key','marketplace_id','currency'):
            if not isinstance(connection.get(key),str) or not connection[key].strip():_error('invalid_configuration','账户标识、站点和币种必须完整配置')
        if self.provider=='ads_sp' and not connection.get('ads_profile_id'):_error('invalid_configuration','缺少广告 profile ID')
        self.client=client;self.base='https://'+(ADS_HOSTS if self.provider=='ads_sp' else SP_HOSTS)[region]
        self.mapping=[];self.by_sku={};self.by_asin={}
        for row in mappings:
            if not isinstance(row,dict) or not row.get('product_id') or not isinstance(row.get('sku'),str) or not row['sku']:
                _error('invalid_mapping','产品绑定缺少产品标识或 SKU')
            item={k:str(row[k]) if row.get(k) is not None else None for k in ('product_id','sku','asin')}
            if item['sku'] in self.by_sku:_error('ambiguous_mapping','同一账户的 SKU 绑定重复，无法安全归属产品')
            self.mapping.append(item);self.by_sku[item['sku']]=item
            if item['asin']:self.by_asin.setdefault(item['asin'],[]).append(item)
        if not self.mapping:_error('invalid_mapping','尚未配置产品与卖家 SKU 绑定')
        scope={k:connection.get(k) for k in ('provider','region','account_key','marketplace_id','currency','time_zone','ads_profile_id')}
        scope.update(start=self.start.isoformat(),end=self.end.isoformat(),mappings=sorted(self.mapping,key=lambda x:x['sku']))
        self.fingerprint=hashlib.sha256(json.dumps(scope,sort_keys=True).encode()).hexdigest()
        if checkpoint:
            if not isinstance(checkpoint,dict) or checkpoint.get('scope')!=self.fingerprint or checkpoint.get('version')!=1:
                _error('stale_checkpoint','同步绑定或日期范围已变化，请新建同步任务')
            self.checkpoint=json.loads(json.dumps(checkpoint))
        else:self.checkpoint={'version':1,'scope':self.fingerprint,'reports':{},'completed':{}}
        if not isinstance(self.checkpoint.get('reports'),dict) or not isinstance(self.checkpoint.get('completed'),dict):
            _error('stale_checkpoint','同步断点格式无效，请新建任务')
        if any(not isinstance(v,list) for v in self.checkpoint['completed'].values()):
            _error('stale_checkpoint','同步断点结果格式无效，请新建任务')
        self.warning=[]

    def request(self,method,url,**kwargs):
        try:return self.client.request(method,url,timeout=30,follow_redirects=False,**kwargs)
        except (httpx.TimeoutException,httpx.TransportError):_error('network_error','无法连接 Amazon 服务',True,30)

    def token(self,force=False):
        names=_env_names(self.connection);values=[os.environ[n] for n in names]
        cache_key=(names[0],self.connection['account_key'],hashlib.sha256(('\0'.join(values)).encode()).hexdigest())
        epoch=self.instant.timestamp()
        with _TOKEN_LOCK:
            cached=_TOKEN_CACHE.get(cache_key)
            if not force and cached and cached['expires_at']>epoch:return cached['token']
            value=_json(self.request('POST',TOKEN_URL,data=dict(client_id=values[0],client_secret=values[1],refresh_token=values[2],grant_type='refresh_token')),self.instant)
            token=value.get('access_token');expires=value.get('expires_in')
            if not isinstance(token,str) or not token or isinstance(expires,bool):_error('invalid_authorization_response','Amazon 授权响应缺少有效 token 或有效期')
            try:expires=int(expires)
            except (TypeError,ValueError):_error('invalid_authorization_response','Amazon 授权响应有效期无效')
            if expires<=30:_error('invalid_authorization_response','Amazon 授权响应有效期不足')
            _TOKEN_CACHE[cache_key]={'token':token,'expires_at':epoch+min(expires,86400)-30}
            return token

    def api(self,method,path,*,allow_list=False,**kwargs):
        for attempt in range(2):
            token=self.token(force=bool(attempt))
            if self.provider=='ads_sp':
                headers={'Authorization':'Bearer '+token,'Amazon-Advertising-API-ClientId':os.environ[_env_names(self.connection)[0]],
                    'Amazon-Advertising-API-Scope':str(self.connection['ads_profile_id']),'Accept':'application/json'}
                if path=='/v2/profiles':headers.pop('Amazon-Advertising-API-Scope',None)
                if method=='POST':headers['Content-Type']='application/vnd.createasyncreportrequest.v3+json'
            else:headers={'x-amz-access-token':token,'Accept':'application/json','Content-Type':'application/json'}
            response=self.request(method,self.base+path,headers=headers,**kwargs)
            if response.status_code!=401 or attempt:return _json(response,self.instant,allow_list=allow_list)
        _error('authorization_expired','Amazon 授权失效，请重新授权')

    def download(self,url,compressed):
        url=_safe_download_url(url)
        try:
            request=self.client.build_request('GET',url,timeout=30)
            for name in ('authorization','x-amz-access-token','amazon-advertising-api-clientid','amazon-advertising-api-scope','cookie'):
                request.headers.pop(name,None)
            response=self.client.send(request,auth=None,stream=True,follow_redirects=False)
            try:
                if response.status_code in (403,404):
                    _error('download_unavailable','报告下载暂不可用，将重新取得下载地址',True,10)
                _check_response(response,self.instant)
                encoding=response.headers.get('content-encoding','').lower()
                if encoding not in ('','identity','gzip'):_error('unsupported_compression','不支持的报告传输压缩格式')
                compressed=compressed or encoding=='gzip'
                length=response.headers.get('content-length')
                if length:
                    try:
                        if int(length)<0:_error('invalid_report','报告长度声明无效')
                        too_large=int(length)>MAX_COMPRESSED
                    except ValueError:_error('invalid_report','报告长度声明无效')
                    if too_large:_error('report_too_large','报告超过允许的下载大小')
                chunks=[];size=0
                # iter_raw avoids HTTPX transparently decompressing a bomb first.
                source=[response.content] if response.is_stream_consumed else response.iter_raw()
                for chunk in source:
                    size+=len(chunk)
                    if size>MAX_COMPRESSED:_error('report_too_large','报告超过允许的下载大小')
                    chunks.append(chunk)
                data=b''.join(chunks)
            finally:response.close()
        except (httpx.TimeoutException,httpx.TransportError):_error('network_error','报告下载暂时失败',True,30)
        if compressed:
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as archive:data=archive.read(MAX_UNCOMPRESSED+1)
            except (OSError,EOFError):_error('invalid_report','压缩报告损坏或格式无效')
        if len(data)>MAX_UNCOMPRESSED:_error('report_too_large','报告解压后超过允许大小')
        try:return json.loads(data)
        except (ValueError,UnicodeDecodeError):_error('invalid_report','报告不是有效的 JSON 数据')

    def coverage(self,rows,dataset,days):
        expected={}
        for item in self.mapping:expected.setdefault(item['product_id'],set()).add(item['sku'])
        result=[]
        for product_id,skus in expected.items():
            for day in days:
                selected=[r for r in rows if r['product_id']==product_id and r['day']==day]
                seen={r['payload'].get('sku') for r in selected}
                complete=bool(selected) and skus.issubset(seen) and all(not r['payload'].get('_incomplete') for r in selected)
                if dataset!='inventory' and _day(day)>=self.today:complete=False
                result.append(dict(product_id=product_id,dataset=dataset,day=day,complete=complete))
        return result

    def _sales_rows(self,report,day):
        if not isinstance(report,dict):_error('invalid_report','销售报告顶层格式无效')
        spec=_object(report.get('reportSpecification',{}));options=_object(spec.get('reportOptions',{}))
        if (spec.get('reportType')!='GET_SALES_AND_TRAFFIC_REPORT' or options.get('asinGranularity')!='SKU'
            or options.get('dateGranularity')!='DAY' or spec.get('marketplaceIds')!=[self.connection['marketplace_id']]
            or str(spec.get('dataStartTime',''))[:10]!=day or str(spec.get('dataEndTime',''))[:10]!=day):
            _error('report_scope_mismatch','销售报告范围、站点或 SKU／日期粒度与任务不一致')
        items=report.get('salesAndTrafficByAsin')
        if not isinstance(items,list) or len(items)>MAX_ROWS:_error('invalid_report','销售报告明细格式或数量无效')
        rows=[];seen=set()
        for item in items:
            if not isinstance(item,dict):_error('invalid_report','销售报告明细格式无效')
            sku=item.get('sku')
            if sku is not None and not isinstance(sku,str):_error('invalid_report','销售报告 SKU 格式无效')
            mapping=self.by_sku.get(sku)
            if not mapping:continue
            if sku in seen:_error('duplicate_report_row','销售报告存在重复 SKU，已停止整批发布')
            seen.add(sku)
            if mapping['asin'] and item.get('childAsin')!=mapping['asin']:_error('product_identity_mismatch','销售报告 ASIN 与已绑定 SKU 不一致')
            values=_object(item.get('salesByAsin',{}));money=_object(values.get('orderedProductSales',{}))
            if money.get('currencyCode')!=self.connection['currency']:_error('currency_mismatch','销售报告币种与账户配置不一致')
            payload=dict(sale_date=day,sku=sku,asin=item.get('childAsin'),units_ordered=_integer(values.get('unitsOrdered')),
                         ordered_revenue=_money(money.get('amount')),currency=money['currencyCode'])
            traffic=_object(item.get('trafficByAsin') or {})
            for source,target in [('sessions','sessions'),('pageViews','page_views')]:
                payload[target]=_integer(traffic.get(source),nullable=True)
            for source,target in [('buyBoxPercentage','buy_box_percentage'),('unitSessionPercentage','unit_session_percentage')]:
                payload[target]=_money(traffic.get(source),nullable=True)
            rows.append(dict(product_id=mapping['product_id'],dataset='sales',day=day,natural_key='sku:'+sku,payload=payload))
        return rows

    def sales(self):
        days=[(self.start+timedelta(days=n)).isoformat() for n in range((self.end-self.start).days+1)]
        for day in days:
            if day in self.checkpoint['completed']:continue
            report_id=self.checkpoint['reports'].get(day)
            if not report_id:
                value=self.api('POST','/reports/2021-06-30/reports',json={'reportType':'GET_SALES_AND_TRAFFIC_REPORT',
                    'reportOptions':{'dateGranularity':'DAY','asinGranularity':'SKU'},'marketplaceIds':[self.connection['marketplace_id']],
                    'dataStartTime':day+'T00:00:00Z','dataEndTime':day+'T23:59:59Z'})
                report_id=value.get('reportId')
                if not isinstance(report_id,str) or not report_id:_error('invalid_response','报告创建响应缺少 reportId')
                self.checkpoint['reports'][day]=_remote_id(report_id)
                return SyncBatch([],[],self.checkpoint,True,30,[])
            value=self.api('GET','/reports/2021-06-30/reports/'+_remote_id(report_id))
            status=value.get('processingStatus')
            if status in {'IN_QUEUE','IN_PROGRESS'}:return SyncBatch([],[],self.checkpoint,True,30,[])
            if status=='CANCELLED':_error('report_cancelled','Amazon 取消了报告，不能将取消推断为零销量')
            if status=='FATAL':_error('report_failed','Amazon 报告生成失败')
            if status!='DONE' or not value.get('reportDocumentId'):_error('invalid_response','报告状态或文档标识无效')
            document=self.api('GET','/reports/2021-06-30/documents/'+_remote_id(value['reportDocumentId']))
            compression=document.get('compressionAlgorithm')
            if compression not in (None,'GZIP'):_error('unsupported_compression','不支持的报告压缩算法')
            report=self.download(document.get('url'),compression=='GZIP')
            self.checkpoint['completed'][day]=self._sales_rows(report,day)
        rows=[r for day in days for r in self.checkpoint['completed'][day]]
        if len(rows)>MAX_ROWS:_error('report_too_large','标准化结果超过单任务行数限制')
        return SyncBatch(rows,self.coverage(rows,'sales',days),{},warnings=[])

    def inventory(self):
        day=self.today.isoformat()
        if not self.start<=self.today<=self.end:_error('inventory_history_unavailable','FBA 当前库存接口不能回填历史库存，任务窗口必须包含账户今天')
        rows=[];seen=set()
        skus=list(self.by_sku)
        for offset in range(0,len(skus),50):
            chunk=skus[offset:offset+50];next_token=None;tokens=set();pages=0;began=time.monotonic()
            while True:
                params={'details':'true','granularityType':'Marketplace','granularityId':self.connection['marketplace_id'],
                        'marketplaceIds':self.connection['marketplace_id'],'sellerSkus':','.join(chunk)}
                if next_token:params['nextToken']=next_token
                value=self.api('GET','/fba/inventory/v1/summaries',params=params)
                items=_object(value.get('payload',{})).get('inventorySummaries')
                if not isinstance(items,list):_error('invalid_response','库存响应缺少 inventorySummaries')
                for item in items:
                    item=_object(item);sku=item.get('sellerSku')
                    if sku is not None and not isinstance(sku,str):_error('invalid_report','库存 SKU 格式无效')
                    mapping=self.by_sku.get(sku)
                    if not mapping or sku not in chunk:continue
                    if sku in seen:_error('duplicate_report_row','库存分页存在重复 SKU，已停止整批发布')
                    seen.add(sku)
                    if mapping['asin'] and item.get('asin')!=mapping['asin']:_error('product_identity_mismatch','库存 ASIN 与绑定 SKU 不一致')
                    detail=_object(item.get('inventoryDetails') or {})
                    reserved=_object(detail.get('reservedQuantity') or {});unfulfillable=_object(detail.get('unfulfillableQuantity') or {})
                    inbound=[_integer(detail.get(k),nullable=True) for k in ('inboundWorkingQuantity','inboundShippedQuantity','inboundReceivingQuantity')]
                    payload=dict(snapshot_date=day,sku=sku,asin=item.get('asin'),fulfillable=_integer(detail.get('fulfillableQuantity'),nullable=True),
                        reserved=_integer(reserved.get('totalReservedQuantity'),nullable=True),inbound=sum(inbound) if all(v is not None for v in inbound) else None,
                        unsellable=_integer(unfulfillable.get('totalUnfulfillableQuantity'),nullable=True),observed_at=self.instant.isoformat(),source_updated_at=item.get('lastUpdatedTime'))
                    if any(payload[k] is None for k in ('fulfillable','reserved','inbound','unsellable')):payload['_incomplete']=True
                    rows.append(dict(product_id=mapping['product_id'],dataset='inventory',day=day,natural_key='sku:'+sku,payload=payload))
                pages+=1;next_token=_object(value.get('pagination') or {}).get('nextToken')
                if len(rows)>MAX_ROWS or pages>1000:_error('report_too_large','库存分页超过单任务安全限制')
                if not next_token:break
                if not isinstance(next_token,str) or next_token in tokens:_error('invalid_pagination','库存分页令牌重复或无效')
                tokens.add(next_token)
                if time.monotonic()-began>25:_error('inventory_pagination_expired','库存连续分页耗时过长，将从完整范围重新查询',True,30)
        return SyncBatch(rows,self.coverage(rows,'inventory',[day]),{},warnings=['当前库存完整查询；未使用会遗漏在途变动的 startDateTime 增量。'])

    def verify_ads_profile(self):
        names=_env_names(self.connection)
        credential_scope=hashlib.sha256(('\0'.join(os.environ[name] for name in names)).encode()).hexdigest()
        key=(self.base,str(self.connection['ads_profile_id']),self.connection['account_key'],credential_scope)
        with _PROFILE_LOCK:cached=_PROFILE_CACHE.get(key)
        if cached and cached['expires_at']>self.instant.timestamp():
            profile=cached['profile']
        else:
            profiles=self.api('GET','/v2/profiles',allow_list=True)
            if len(profiles)>10000:_error('invalid_response','广告账户列表超过允许范围')
            matches=[p for p in profiles if isinstance(p,dict) and str(p.get('profileId'))==str(self.connection['ads_profile_id'])]
            if len(matches)!=1:_error('profile_not_authorized','授权账户列表中未找到唯一的目标广告 profile')
            source=matches[0]
            # Cache only identity metadata, never the full account list or tokens.
            profile={k:source.get(k) for k in ('countryCode','currencyCode','timezone')}
            info=source.get('accountInfo')
            profile['marketplaceStringId']=info.get('marketplaceStringId') if isinstance(info,dict) else None
            with _PROFILE_LOCK:
                _PROFILE_CACHE[key]={'expires_at':self.instant.timestamp()+300,'profile':profile}
        country=lambda v:'GB' if str(v).upper()=='UK' else str(v).upper()
        if not profile.get('countryCode') or not self.connection.get('marketplace') or country(profile['countryCode'])!=country(self.connection['marketplace']):
            _error('profile_marketplace_mismatch','广告 profile 所属市场与配置站点不一致')
        if profile.get('currencyCode')!=self.connection['currency']:
            _error('profile_currency_mismatch','广告 profile 币种与配置不一致，不能重标金额币种')
        if profile.get('timezone')!=self.connection.get('time_zone'):
            _error('profile_timezone_mismatch','广告 profile 时区与配置不一致，不能混用报表日期')
        if profile.get('marketplaceStringId') and profile['marketplaceStringId']!=self.connection['marketplace_id']:
            _error('profile_marketplace_mismatch','广告 profile 对应的 marketplace ID 与配置不一致')

    def _ads_rows(self,items):
        if not isinstance(items,list) or len(items)>MAX_ROWS:_error('invalid_report','广告商品报告格式或数量无效')
        rows=[];seen=set()
        for item in items:
            if not isinstance(item,dict):_error('invalid_report','广告商品报告明细格式无效')
            day=_day(item.get('date'))
            if not self.start<=day<=self.end:_error('report_scope_mismatch','广告报告日期不属于任务窗口')
            sku=item.get('advertisedSku');asin=item.get('advertisedAsin')
            if (sku is not None and not isinstance(sku,str)) or (asin is not None and not isinstance(asin,str)):_error('invalid_report','广告商品标识格式无效')
            mapping=self.by_sku.get(sku)
            if not mapping:
                candidates=self.by_asin.get(asin,[])
                if len(candidates)==1:mapping=candidates[0]
                elif len(candidates)>1:_error('ambiguous_mapping','广告报告 ASIN 对应多个卖家 SKU，不能分摊整笔花费')
            if not mapping:continue
            if sku and sku!=mapping['sku']:_error('product_identity_mismatch','广告商品 SKU 与绑定不一致')
            if mapping['asin'] and asin!=mapping['asin']:_error('product_identity_mismatch','广告商品 ASIN 与绑定不一致')
            identifiers=[_stable(item.get(k)) for k in ('campaignId','adGroupId','adId')]
            key=json.dumps([*identifiers,mapping['sku']],separators=(',',':'))
            identity=(day.isoformat(),key)
            if identity in seen:_error('duplicate_report_row','广告商品报告存在重复实体，已停止整批发布')
            seen.add(identity)
            sales=_money(item.get('sales7d'),nullable=True);orders=_integer(item.get('purchases7d'),nullable=True)
            payload=dict(report_date=day.isoformat(),sku=mapping['sku'],asin=asin,campaign_id=identifiers[0],ad_group_id=identifiers[1],ad_id=identifiers[2],
                campaign_name=str(item.get('campaignName') or identifiers[0]),entity_name=str(item.get('adGroupName') or identifiers[1]),
                impressions=_integer(item.get('impressions')),clicks=_integer(item.get('clicks')),spend=_money(item.get('cost')),
                attributed_sales=sales,attributed_orders=orders,currency=self.connection['currency'],ad_type='SP',report_grain='advertised_product',
                attribution_days=7,mature=self.today>day+timedelta(days=7))
            if sales is None or orders is None:payload['_incomplete']=True
            rows.append(dict(product_id=mapping['product_id'],dataset='advertising',day=day.isoformat(),natural_key=key,payload=payload))
        return rows

    def ads(self):
        self.verify_ads_profile()
        report_id=self.checkpoint['reports'].get('ads')
        if not report_id:
            value=self.api('POST','/reporting/reports',json={'name':'HOMA SP advertised products','startDate':self.start.isoformat(),'endDate':self.end.isoformat(),
                'configuration':{'adProduct':'SPONSORED_PRODUCTS','reportTypeId':'spAdvertisedProduct','groupBy':['advertiser'],'timeUnit':'DAILY','format':'GZIP_JSON',
                    'columns':['date','campaignId','campaignName','adGroupId','adGroupName','adId','advertisedAsin','advertisedSku','impressions','clicks','cost','purchases7d','sales7d']}})
            report_id=value.get('reportId')
            if not isinstance(report_id,str) or not report_id:_error('invalid_response','广告报告创建响应缺少 reportId')
            self.checkpoint['reports']['ads']=_remote_id(report_id)
            return SyncBatch([],[],self.checkpoint,True,30,[ADS_WARNING])
        value=self.api('GET','/reporting/reports/'+_remote_id(report_id))
        status=value.get('status')
        if status in {'PENDING','PROCESSING','IN_PROGRESS'}:return SyncBatch([],[],self.checkpoint,True,30,[ADS_WARNING])
        if status=='FAILED':_error('report_failed','Amazon 广告报告生成失败')
        if status!='COMPLETED':_error('invalid_response','广告报告状态无效')
        config=_object(value.get('configuration',{}))
        if (config.get('adProduct')!='SPONSORED_PRODUCTS' or config.get('reportTypeId')!='spAdvertisedProduct'
            or config.get('timeUnit')!='DAILY' or config.get('format')!='GZIP_JSON' or config.get('groupBy')!=['advertiser']
            or value.get('startDate')!=self.start.isoformat() or value.get('endDate')!=self.end.isoformat()):
            _error('report_scope_mismatch','广告报告类型、粒度或日期与商品同步任务不一致')
        rows=self._ads_rows(self.download(value.get('url'),True))
        days=[(self.start+timedelta(days=n)).isoformat() for n in range((self.end-self.start).days+1)]
        return SyncBatch(rows,self.coverage(rows,'advertising',days),{},warnings=[ADS_WARNING])


def collect_connection(connection,mappings,start_date,end_date,checkpoint,*,client=None,now=None)->SyncBatch:
    """Collect one bounded job; persist pending checkpoint before the next poll.

    Money values are decimal strings. Missing source values remain null; incomplete
    partitions never claim complete coverage. Caller owns DB atomic publication.
    """
    status=credentials_status(connection)
    if not status['configured']:_error('credentials_missing','尚未配置此数据源所需的授权环境变量')
    own=client is None
    client=client or httpx.Client(timeout=30,follow_redirects=False)
    try:
        collector=_Collector(connection,mappings,start_date,end_date,checkpoint,client,_instant(now))
        return {'sp_sales':collector.sales,'sp_inventory':collector.inventory,'ads_sp':collector.ads}[collector.provider]()
    finally:
        if own:client.close()
