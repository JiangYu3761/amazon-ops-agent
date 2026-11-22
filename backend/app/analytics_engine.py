"""Deterministic product metrics. No network calls, database writes or trading actions.

Amounts are accumulated as Decimal before ratios are formed. An absent report day
is never a zero day unless coverage explicitly identifies it as complete.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP, localcontext
from math import isfinite
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ENGINE_VERSION='1.0.0'
D=Decimal
COST_FIELDS=('product_cost','first_mile_cost','fba_fee','referral_fee','other_cost')
TITLES={'shop':'店铺与账户','compliance':'产品与合规','listing':'Listing 与素材','ads':'广告运营','inventory':'备货与库存','calendar':'活动与排期','finance':'成本与利润','service':'订单与售后','review':'经营复盘'}
DEFAULT_POLICIES={'lead_time_days':None,'safety_days':None,'review_days':7,'moq':1,'pack_size':1,'target_acos':None,'inventory_max_age_days':2}


def _day(value,tz=None):
    if isinstance(value,datetime):return value.astimezone(tz).date() if value.tzinfo and tz else value.date()
    if isinstance(value,date):return value
    if isinstance(value,str):
        if len(value)==10:return date.fromisoformat(value)
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.astimezone(tz).date() if parsed.tzinfo and tz else parsed.date()
    raise ValueError('Expected an ISO date')


def _moment(value,tz):
    if value is None:return datetime.now(timezone.utc)
    if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if isinstance(value,date) and not isinstance(value,datetime):value=datetime.combine(value,time.min,tzinfo=tz)
    if not isinstance(value,datetime):raise ValueError('now must be a date or datetime')
    return value.replace(tzinfo=tz) if value.tzinfo is None else value


def _decimal(value):
    if isinstance(value,bool) or value is None:return None
    try:
        n=D(str(value))
        return n if n.is_finite() and abs(n)<=D('1e24') else None
    except (InvalidOperation,ValueError,TypeError):return None


def _number(value,unit):
    if value is None:return None
    n=value if isinstance(value,D) else D(str(value))
    if unit in {'units','count'} and n==n.to_integral_value():return int(n)
    precision=D('.01') if unit=='money' else D('.000001')
    return float(n.quantize(precision,rounding=ROUND_HALF_UP))


def _json(value):
    if isinstance(value,D):return _number(value,'ratio')
    if isinstance(value,UUID):return str(value)
    if isinstance(value,float) and not isfinite(value):return None
    if isinstance(value,(date,datetime)):return value.isoformat()
    if isinstance(value,dict):return {str(k):_json(v) for k,v in value.items()}
    if isinstance(value,(list,tuple,set)):return [_json(v) for v in value]
    return value


def _calculate_product_metrics(product,records,start_date,end_date,*,now=None,policies=None,coverage=None,tasks=None,events=None,rankings=None,sources=None):
    """Compute scoped evidence, with explicit coverage, cost and attribution limits.

    Percent values already contain the factor 100; ROAS is a ratio. ``as_of`` is
    the requested inventory cut-off, capped at the product's current business day.
    Advertising maturity additionally uses the actual evaluation time and the
    last source refresh (or an explicit mature flag), never the report date alone.
    """
    product=dict(product or {});records=records or {};coverage=coverage or {}
    policy={**DEFAULT_POLICIES,**(policies or {})};issues=[];bad=set()
    try:tz=ZoneInfo(product.get('business_timezone') or 'America/Los_Angeles')
    except (ZoneInfoNotFoundError,ValueError,TypeError):
        tz=ZoneInfo('UTC');issues.append({'code':'invalid_timezone','dataset':'product','message':'业务时区无效，已使用 UTC 并标记口径不完整'})
    moment=_moment(now,tz);today=moment.astimezone(tz).date();start=_day(start_date,tz);end=_day(end_date,tz)
    if start>end:raise ValueError('start_date must not exceed end_date')
    asof=min(end,today);currency=str(product.get('currency') or 'USD').upper();pid=str(product.get('id')) if product.get('id') is not None else None
    days=[start+timedelta(days=i) for i in range(max(0,(asof-start).days+1))]
    closed=[day for day in days if day<today]
    if end>=today:issues.append({'code':'open_or_future_period','dataset':'period','message':'当前业务日或未来日期尚未结束，不能视为完整营业日'})
    def issue(code,dataset,message):
        entry={'code':code,'dataset':dataset,'message':message}
        if entry not in issues:issues.append(entry)
        bad.add(dataset)
    def scoped(row):return row.get('product_id') is None or pid is None or str(row['product_id'])==pid
    invalid_cost_versions=[]
    def prepare(dataset,date_field,required,aliases=None):
        output=[];aliases=aliases or {}
        for source in records.get(dataset,[]) or []:
            if not isinstance(source,dict):issue('invalid_row',dataset,'存在格式不正确的记录，已排除');continue
            if not scoped(source):issue('foreign_product',dataset,'已排除其他产品记录');continue
            row=dict(source)
            try:day=_day(row.get(date_field,row.get('date')),tz)
            except (ValueError,TypeError):issue('missing_report_date',dataset,'存在缺少有效日期的记录，已排除');continue
            if day>asof or dataset in {'sales','advertising'} and day<start:continue
            if dataset in {'sales','advertising','costs'} and str(row.get('currency') or '').upper()!=currency:
                issue('currency_mismatch',dataset,'已排除币种不明或与产品币种不一致的金额记录');continue
            if dataset in {'sales','inventory','costs'} and not str(row.get('sku') or '').strip():
                issue('missing_sku',dataset,'记录缺少 SKU，已排除');continue
            parsed={};valid=True
            for name in required:
                value=row.get(name)
                if value is None:
                    for alias in aliases.get(name,[]):
                        if row.get(alias) is not None:value=row[alias];break
                n=_decimal(value)
                if n is None or n<0 or name in {'units_ordered','impressions','clicks','attributed_orders','fulfillable','reserved','inbound','unsellable'} and n!=n.to_integral_value():
                    issue('invalid_numeric',dataset,'必需数值缺失、无效或为负，受影响记录已排除');valid=False;break
                parsed[name]=n
            if valid:output.append({**row,**parsed,'_day':day})
            elif dataset=='costs':invalid_cost_versions.append({'sku':row.get('sku'),'_day':day,'_invalid':True})
        return sorted(output,key=lambda r:(r['_day'],str(r.get('sku','')),str(r.get('campaign_name','')),str(r.get('entity_name','')),str(r.get('ad_type','')),str(r.get('report_grain','')),str(r.get('attribution_days',''))))
    sales=prepare('sales','sale_date',['units_ordered','ordered_revenue'])
    ads=prepare('advertising','report_date',['impressions','clicks','spend','attributed_sales','attributed_orders'],{'attributed_sales':['sales'],'attributed_orders':['orders']})
    stock=prepare('inventory','snapshot_date',['fulfillable','reserved','inbound','unsellable'])
    costs=prepare('costs','effective_date',COST_FIELDS)
    # Sales and effective-cost identities come from the existing database keys.
    def unique(rows,dataset,fields):
        grouped=defaultdict(list)
        for row in rows:grouped[(row.get('sku'),row['_day'])].append(row)
        out=[]
        for _,group in sorted(grouped.items(),key=lambda item:(str(item[0][0]),item[0][1])):
            if len({tuple(row[f] for f in fields) for row in group})>1:
                issue('conflicting_business_key',dataset,'同一 SKU 和日期存在冲突记录，无法确定应使用哪一条，已排除')
                if dataset=='costs':invalid_cost_versions.append({'sku':group[0].get('sku'),'_day':group[0]['_day'],'_invalid':True})
                continue
            chosen=dict(group[0])
            if dataset=='sales':
                for optional_field in ('orders','sessions','page_views'):
                    if len({_decimal(row.get(optional_field)) for row in group})>1:
                        chosen[optional_field]=None;issue('conflicting_optional_metric',dataset,'同一销售日可选订单或会话指标冲突，该指标停算')
            out.append(chosen)
        return out
    sales=unique(sales,'sales',('units_ordered','ordered_revenue'))
    costs=unique(costs,'costs',COST_FIELDS)
    cov={};complete={}
    for dataset in ('sales','advertising','inventory','costs'):
        meta=coverage.get(dataset) if isinstance(coverage.get(dataset),dict) else {}
        declared=set()
        for value in meta.get('complete_days',[]) or []:
            try:declared.add(_day(value,tz))
            except (ValueError,TypeError):issue('invalid_coverage_date',dataset,'完整日标记中存在无效日期')
        complete[dataset]=bool(days) and len(closed)==len(days) and set(days).issubset(declared) and dataset not in bad
        observed={row['_day'] for row in {'sales':sales,'advertising':ads,'inventory':stock,'costs':costs}[dataset] if row['_day']>=start}
        cov[dataset]={'complete_days':[d.isoformat() for d in sorted(declared) if start<=d<=asof and d<today],
                      'observed_days':[d.isoformat() for d in sorted(observed)],'missing_days':[d.isoformat() for d in days if d not in declared or d>=today],
                      'is_complete':complete[dataset],'last_synced_at':meta.get('last_synced_at'),'source':meta.get('source')}
    modules={key:{'title':title,'metrics':[],'findings':[],'missing_sources':[]} for key,title in TITLES.items()}
    def metric(module,key,label,value,unit,formula,*,status='ready',reason='',missing=None,inputs=None,datasets=None):
        m={'key':key,'label':label,'value':_number(value,unit),'unit':unit,'status':status,'formula':formula,'reason':reason,
           'missing_inputs':list(missing or []),'inputs':_json(inputs or {}),'source_datasets':list(datasets or [])}
        modules[module]['metrics'].append(m);return m
    def absent(module,key,label,formula,missing,datasets=None):
        for item in missing:
            if item not in modules[module]['missing_sources']:modules[module]['missing_sources'].append(item)
        return metric(module,key,label,None,'percent' if 'rate' in key or 'acos' in key or 'conversion' in key else 'money' if key in {'net_profit','settled_revenue'} else 'count',formula,status='missing',reason='尚无可验证的来源或必要输入',missing=missing,datasets=datasets)
    def source_status(dataset,rows):
        if complete[dataset]:return 'ready',''
        if rows:return 'partial','仅汇总已取得记录；缺少明确完整日覆盖，缺失日期未按零补齐'
        return 'missing','没有可计算记录，也没有覆盖本期间的完整日确认'
    sales_status,sales_reason=source_status('sales',sales);ad_status,ad_reason=source_status('advertising',ads)
    revenue=sum((r['ordered_revenue'] for r in sales),D(0)) if sales or complete['sales'] else None
    units=sum((r['units_ordered'] for r in sales),D(0)) if sales or complete['sales'] else None
    def ratio(module,key,label,numerator,denominator,formula,*,unit='percent',status='ready',reason='',datasets=None,inputs=None):
        info={**(inputs or {}),'numerator':numerator,'denominator':denominator}
        if numerator is None or denominator is None:return metric(module,key,label,None,unit,formula,status='missing',reason=reason or '分子或分母缺少必要输入',missing=['numerator' if numerator is None else 'denominator'],inputs=info,datasets=datasets)
        if denominator==0:return metric(module,key,label,None,unit,formula,status='undefined',reason='分母为 0，比例未定义',inputs=info,datasets=datasets)
        return metric(module,key,label,numerator/denominator*(100 if unit=='percent' else 1),unit,formula,status=status,reason=reason,inputs=info,datasets=datasets)
    metric('shop','ordered_revenue','订购销售额',revenue,'money','Σ ordered_revenue（产品币种）',status=sales_status,reason=sales_reason,inputs={'currency':currency,'row_count':len(sales)},datasets=['sales'])
    metric('shop','units_ordered','订购件数',units,'units','Σ units_ordered',status=sales_status,reason=sales_reason,datasets=['sales'])
    optional={}
    for key,label in [('orders','订单数'),('sessions','会话数'),('page_views','页面浏览量')]:
        vals=[_decimal(r.get(key)) for r in sales]
        valid=bool(vals) and all(v is not None and v>=0 and v==v.to_integral_value() for v in vals)
        optional[key]=sum(vals,D(0)) if valid else None
        metric('shop' if key=='orders' else 'listing',key,label,optional[key],'count','Σ '+key,status=sales_status if valid else 'missing',reason=sales_reason if valid else '现有销售记录未完整提供 '+key,missing=[] if valid else [key],datasets=['sales'])
    ratio('shop','average_selling_price','平均订购单价',revenue,units,'Σ 订购销售额 ÷ Σ 订购件数',unit='money',status=sales_status,reason=sales_reason,datasets=['sales'])
    ratio('shop','average_order_value','平均订单金额',revenue,optional['orders'],'Σ 订购销售额 ÷ Σ 订单数',unit='money',status=sales_status,reason=sales_reason,datasets=['sales'])
    absent('shop','account_health','账户健康','读取账户健康与绩效通知',['account_health'])
    ratio('listing','unit_session_percentage','件数会话百分比',units,optional['sessions'],'Σ 订购件数 ÷ Σ sessions × 100',status=sales_status,reason=sales_reason,datasets=['sales'])
    ratio('listing','order_conversion_rate','订单会话转化率',optional['orders'],optional['sessions'],'Σ 订单数 ÷ Σ sessions × 100',status=sales_status,reason=sales_reason,datasets=['sales'])
    # Keep attribution cohorts visible; conflicting grains cannot be added together.
    deduped_ads={}
    for row in ads:
        key=(row['_day'],str(row.get('ad_type')),str(row.get('report_grain')),str(row.get('attribution_days')),str(row.get('campaign_name')),str(row.get('entity_name','')))
        if key in deduped_ads:
            old=deduped_ads[key]
            if old is not None and any(old[k]!=row[k] for k in ('spend','attributed_sales','attributed_orders','clicks','impressions')):
                issue('conflicting_ad_row','advertising','同一广告业务键存在冲突记录，已排除该业务键');deduped_ads[key]=None
            elif old is not None:
                old['mature']=False if old.get('mature') is False or row.get('mature') is False else True if old.get('mature') is True or row.get('mature') is True else None
        else:deduped_ads[key]=dict(row)
    ads=[row for row in deduped_ads.values() if row is not None]
    if any(row is None for row in deduped_ads.values()):complete['advertising']=False;cov['advertising']['is_complete']=False
    groups=defaultdict(list);grain_by_type=defaultdict(set)
    for row in ads:
        kind=str(row.get('ad_type') or 'unknown').upper();grain=str(row.get('report_grain') or 'unknown').lower()
        window=_decimal(row.get('attribution_days'));window=int(window) if window is not None and window>0 and window==window.to_integral_value() and window<=90 else None
        groups[(kind,grain,window)].append(row);grain_by_type[kind].add(grain)
    mixed_grain=any(len(values)>1 for values in grain_by_type.values())
    # Repeated traffic identities with different attribution windows would duplicate spend.
    identities=defaultdict(set)
    for group,rows in groups.items():
        for row in rows:identities[(row['_day'],group[0],row.get('campaign_name'),row.get('entity_name',''))].add(group)
    overlapping=mixed_grain or any(len(v)>1 for v in identities.values())
    if overlapping:issue('overlapping_ad_grain','advertising','存在可能重叠的广告粒度或归因窗口，合并广告总计已停算，请分别查看分组')
    mature_rows=[];immature_rows=[];unknown_rows=[];group_info=[]
    refreshed=coverage.get('advertising',{}).get('last_synced_at') if isinstance(coverage.get('advertising'),dict) else None
    try:refresh_time=_moment(refreshed,tz) if refreshed else None
    except (ValueError,TypeError):refresh_time=None
    if refresh_time and refresh_time>moment:
        issue('future_refresh_timestamp','advertising','来源刷新时间晚于计算时刻，不能作为归因成熟证据');refresh_time=None
    for (kind,grain,window),rows in sorted(groups.items(),key=lambda item:(item[0][0],item[0][1],item[0][2] or 0)):
        counts={'mature':0,'immature':0,'unknown':0};sums={k:sum((r[k] for r in rows),D(0)) for k in ('spend','attributed_sales','attributed_orders','clicks','impressions')}
        for row in rows:
            if window is None:state='unknown'
            elif row['_day']+timedelta(days=window)>=today or row.get('mature') is False:state='immature'
            else:
                closed_at=datetime.combine(row['_day']+timedelta(days=window+1),time.min,tzinfo=tz)
                state='mature' if row.get('mature') is True or refresh_time and refresh_time>=closed_at else 'unknown'
            {'mature':mature_rows,'immature':immature_rows,'unknown':unknown_rows}[state].append(row);counts[state]+=1
        group_info.append({'ad_type':kind,'report_grain':grain,'attribution_days':window,'maturity_counts':counts,**sums,
                           'acos':sums['spend']/sums['attributed_sales']*100 if sums['attributed_sales'] else None,
                           'roas':sums['attributed_sales']/sums['spend'] if sums['spend'] else None})
    actual_ad_types={kind for kind,_,_ in groups}
    ad_coverage=coverage.get('advertising',{}) if isinstance(coverage.get('advertising'),dict) else {}
    declared_types=ad_coverage.get('ad_types_complete',[])
    declared_types={str(kind).upper() for kind in declared_types} if isinstance(declared_types,list) else set()
    full_ad_scope={'SP','SB','SD'}.issubset(declared_types) and 'UNKNOWN' not in actual_ad_types
    known_types=actual_ad_types-{'UNKNOWN'}
    sp_only=(actual_ad_types=={'SP'} or not actual_ad_types and declared_types=={'SP'}) and not full_ad_scope
    scope_label='全渠道广告（SP / SB / SD）' if full_ad_scope else 'SP 广告' if sp_only else ' / '.join(sorted(known_types))+' 广告（渠道覆盖未完整确认）' if known_types else '广告渠道未确认'
    scope_reason='已显式确认 SP、SB、SD 渠道覆盖' if full_ad_scope else '当前仅覆盖 SP 广告，不包含未接入的 SB、SD；不能据此评估全渠道 TACOS' if sp_only else '广告渠道未完整确认，不能作为全渠道 TACOS 或全广告效果'
    ad_scope={'ad_types':sorted(actual_ad_types),'ad_types_complete':sorted(declared_types),'all_channels_complete':full_ad_scope,'label':scope_label,'reason':scope_reason}
    modules['ads']['scope']=ad_scope;modules['ads']['description']=scope_reason
    cov['advertising']['ad_types_complete']=sorted(declared_types)
    metadata_missing=any(kind=='UNKNOWN' or grain=='unknown' or window is None for kind,grain,window in groups)
    if metadata_missing:issue('unknown_ad_metadata','advertising','广告类型、报表粒度或归因窗口缺失，不能形成确定的成熟效果判断')
    if unknown_rows:issue('unverified_ad_maturity','advertising','部分广告归因成熟度缺少窗口或最终刷新时间证据')
    attribution_mixed=len({window for _,_,window in groups if window is not None})>1
    if attribution_mixed:issue('mixed_attribution_windows','advertising','广告包含不同归因窗口，已分组保留；合计效果比率仅供参照')
    ads_confident=complete['advertising'] and not metadata_missing and not immature_rows and not unknown_rows and not overlapping and not attribution_mixed and 'advertising' not in bad
    totals={k:(sum((r[k] for r in ads),D(0)) if ads or complete['advertising'] else None) if not overlapping else None for k in ('spend','attributed_sales','attributed_orders','impressions','clicks')}
    for key,label,unit in [('spend','广告花费','money'),('attributed_sales','广告归因销售额','money'),('attributed_orders','广告归因订单','count'),('impressions','广告展示量','count'),('clicks','广告点击量','count')]:
        metric('ads',key,('SP '+label if sp_only else label),totals[key],unit,'Σ '+key+'（不重叠粒度）',status='unavailable' if overlapping else 'ready' if ads_confident else ad_status if ad_status=='missing' else 'partial',
               reason='报表粒度可能重叠，不能相加' if overlapping else ('归因窗口尚未成熟或元数据不完整' if ads and not ads_confident else ad_reason),inputs={'groups':group_info,'currency':currency,'ad_scope':ad_scope},datasets=['advertising'])
    confidence='ready' if ads_confident else 'partial'
    ad_explanation='归因窗口和刷新证据完整' if ads_confident else '按已取得广告记录计算；归因成熟度或期间覆盖不完整，不据此直接执行否词、竞价或价格调整'
    ratio('ads','ctr','广告点击率',totals['clicks'],totals['impressions'],'Σ clicks ÷ Σ impressions × 100',status=confidence,reason=ad_explanation,datasets=['advertising'])
    ratio('ads','cpc','平均点击花费',totals['spend'],totals['clicks'],'Σ spend ÷ Σ clicks',unit='money',status=confidence,reason=ad_explanation,datasets=['advertising'])
    ratio('ads','acos','广告 ACOS',totals['spend'],totals['attributed_sales'],'Σ spend ÷ Σ attributed_sales × 100',status=confidence,reason=ad_explanation,datasets=['advertising'])
    ratio('ads','roas','广告 ROAS',totals['attributed_sales'],totals['spend'],'Σ attributed_sales ÷ Σ spend',unit='ratio',status=confidence,reason=ad_explanation,datasets=['advertising'])
    ratio('ads','ad_order_conversion_rate','广告订单转化率',totals['attributed_orders'],totals['clicks'],'Σ attributed_orders ÷ Σ clicks × 100',status=confidence,reason=ad_explanation,datasets=['advertising'])
    ratio('ads','tacos','全渠道 TACOS' if full_ad_scope else 'SP广告花费率' if sp_only else '已接入渠道广告花费率',totals['spend'],revenue,('SP广告花费' if sp_only else '全渠道广告花费' if full_ad_scope else '已接入渠道广告花费')+' ÷ 订购销售额 × 100；广告归因销售额不重复加到总销售额',status='ready' if ads_confident and complete['sales'] and (full_ad_scope or sp_only) else 'partial',reason=scope_reason+'；'+ad_explanation,inputs={'ad_scope':ad_scope},datasets=['advertising','sales'])
    mature_spend=sum((r['spend'] for r in mature_rows),D(0)) if mature_rows else None
    mature_sales=sum((r['attributed_sales'] for r in mature_rows),D(0)) if mature_rows else None
    ratio('ads','mature_acos','已确认成熟记录 ACOS',mature_spend,mature_sales,'成熟且已刷新记录 Σ spend ÷ Σ attributed_sales × 100',status='ready' if ads_confident else 'partial',reason='仅使用有明确归因窗口且已完成最终刷新确认的记录',datasets=['advertising'])
    metric('ads','immature_spend','尚未成熟记录花费',sum((r['spend'] for r in immature_rows),D(0)) if ads else None,'money','Σ 未成熟记录 spend',status='partial' if ads else 'missing',reason='未成熟归因不作为确定优化结论',inputs={'unknown_maturity_spend':sum((r['spend'] for r in unknown_rows),D(0)),'groups':group_info},datasets=['advertising'])
    for ad_metric in modules['ads']['metrics']:
        ad_metric['inputs'].setdefault('ad_scope',_json(ad_scope))
        if ad_metric['key']!='tacos':ad_metric['reason']=scope_reason+('；'+ad_metric['reason'] if ad_metric['reason'] else '')
    target=_decimal(policy.get('target_acos'))
    if target is not None and target>=0 and mature_spend is not None and mature_sales and mature_spend/mature_sales*100>target:
        modules['ads']['findings'].append({'type':'mature_acos_above_target','severity':'warning','title':'已确认成熟记录 ACOS 高于设定目标','evidence':_json({'spend':mature_spend,'sales':mature_sales,'target_acos':target}),'action':'核对成熟报表、利润和库存后人工评审；本引擎不会修改定向、竞价或价格'})
    # Latest snapshot is selected independently for every SKU, not globally.
    latest={};ambiguous=set()
    for row in stock:
        sku=row.get('sku')
        if not sku:issue('missing_sku','inventory','库存记录缺少 SKU，已排除');continue
        old=latest.get(sku)
        if old and old['_day']==row['_day'] and any(old[k]!=row[k] for k in ('fulfillable','reserved','inbound','unsellable')):
            ambiguous.add(sku);issue('conflicting_inventory_snapshot','inventory','同一 SKU 最新日库存快照冲突，受影响 SKU 未计入可售总计')
        elif not old or row['_day']>old['_day']:latest[sku]=dict(row);ambiguous.discard(sku)
        elif old['_day']==row['_day'] and old.get('inbound_eta')!=row.get('inbound_eta'):
            latest[sku]['inbound_eta']=None;issue('conflicting_inbound_eta','inventory','同一快照的在途 ETA 冲突，不能计为可靠到货量')
    for sku in ambiguous:latest.pop(sku,None)
    sale_skus={r.get('sku') for r in sales if r.get('sku')};expected_skus=sale_skus|set(latest)
    if product.get('sku'):expected_skus.add(product['sku'])
    missing_skus=expected_skus-set(latest)
    max_age=_decimal(policy.get('inventory_max_age_days'));max_age=int(max_age) if max_age is not None and max_age>=0 else 2
    stale=[sku for sku,r in latest.items() if (asof-r['_day']).days>max_age]
    inventory_status='missing' if not latest else 'partial' if stale or missing_skus or ambiguous or 'inventory' in bad else 'ready'
    inventory_reason='；'.join(filter(None,['存在缺失 SKU 快照' if missing_skus else '', '部分库存快照过期' if stale else '', '快照冲突或字段不完整' if ambiguous or 'inventory' in bad else '']))
    stock_inputs={'snapshot_dates':{sku:r['_day'].isoformat() for sku,r in latest.items()},'stale_skus':sorted(stale),'missing_skus':sorted(missing_skus),'as_of':asof.isoformat()}
    for key,label in [('fulfillable','当前可售库存'),('reserved','预留库存'),('inbound','在途库存'),('unsellable','不可售库存')]:
        metric('inventory',key,label,sum((r[key] for r in latest.values()),D(0)) if latest else None,'units','每 SKU 最新且不晚于 as_of 的 '+key+' 求和',status=inventory_status,reason=inventory_reason or ('fulfillable 已为可售量，不再重复扣除 reserved' if key=='fulfillable' else '在途量不等于当前可售量' if key=='inbound' else ''),inputs=stock_inputs,datasets=['inventory'])
    closed_coverage={_day(value,tz) for value in cov['sales']['complete_days']}
    full_demand=bool(closed) and set(closed).issubset(closed_coverage) and 'sales' not in bad and all(r.get('sku') for r in sales)
    closed_units=sum((r['units_ordered'] for r in sales if r['_day']<today),D(0))
    daily=closed_units/D(len(closed)) if full_demand else None
    metric('inventory','daily_sales_rate','完整覆盖日均销量',daily,'units','完整覆盖期间 Σ units_ordered ÷ 完整自然日数',status='ready' if daily is not None else 'missing',reason='' if daily is not None else '需要全部已结束销售日的显式完整覆盖；缺失日不能按零补齐',missing=[] if daily is not None else ['sales.complete_days'],inputs={'complete_day_count':len(closed),'closed_day_count':len(closed),'closed_days':[d.isoformat() for d in closed],'excluded_open_days':[d.isoformat() for d in days if d>=today],'units_in_closed_days':closed_units},datasets=['sales'])
    available=sum((r['fulfillable'] for r in latest.values()),D(0)) if latest else None
    ratio('inventory','days_of_supply','可售库存覆盖天数',available,daily,'可售库存 ÷ 完整日均销量；不扣第二次预留、不计无 ETA 在途',unit='days',status=('estimated' if inventory_status=='ready' else inventory_status) if daily is not None else 'missing',reason=inventory_reason or '按已结束完整日的历史均速估算，不是未来销量承诺',datasets=['inventory','sales'])
    cycles={k:_decimal(policy.get(k)) for k in ('lead_time_days','safety_days','review_days')}
    cycle_missing=[k for k,v in cycles.items() if v is None or v<0]
    moq=_decimal(policy.get('moq'));pack=_decimal(policy.get('pack_size'))
    if moq is None or moq<1 or moq!=moq.to_integral_value():cycle_missing.append('moq')
    if pack is None or pack<1 or pack!=pack.to_integral_value():cycle_missing.append('pack_size')
    rec=None;reorder=None;position=None;per_sku=[]
    if daily is not None and not cycle_missing and latest and inventory_status=='ready':
        horizon=sum(cycles.values(),D(0));reorder=daily*(cycles['lead_time_days']+cycles['safety_days']);rec=D(0);position=D(0)
        for sku in sorted(expected_skus):
            row=latest[sku];rate=sum((r['units_ordered'] for r in sales if r.get('sku')==sku and r['_day']<today),D(0))/D(len(closed));reliable=D(0)
            try:eta=_day(row.get('inbound_eta'),tz)
            except (ValueError,TypeError):eta=None
            if eta and asof<=eta<=asof+timedelta(days=int(horizon)):reliable=row['inbound']
            stock_position=row['fulfillable']+reliable;raw=max(D(0),rate*horizon-stock_position)
            quantity=(max(raw,moq)/pack).to_integral_value(rounding=ROUND_CEILING)*pack if raw>0 else D(0)
            rec+=quantity;position+=stock_position
            per_sku.append({'sku':sku,'daily_rate':rate,'fulfillable':row['fulfillable'],'reliable_inbound':reliable,'inbound_eta':eta,'target_units':rate*horizon,'recommended_units':quantity})
    replenishment_missing=([] if daily is not None else ['sales.complete_days'])+cycle_missing+([] if latest and inventory_status=='ready' else ['fresh_inventory_per_sku'])
    for key,label,value,formula in [('reorder_point','补货触发库存',reorder,'日均销量 ×（补货提前期 + 安全天数）'),('inventory_position','计划周期内库存位置',position,'可售库存 + 周期内有明确 ETA 的在途量'),('recommended_replenishment','建议补货数量',rec,'逐 SKU max(0,目标周期需求−库存位置)，按 MOQ 和整箱倍数向上取整后求和')]:
        metric('inventory',key,label,value,'units',formula,status='estimated' if value is not None else 'missing',reason='需求估算而非采购指令；无 ETA 在途不抵减补货需求' if value is not None else '销售覆盖、周期参数或库存新鲜度不足，不能给出补货数量',missing=replenishment_missing,inputs={'policies':policy,'per_sku':per_sku},datasets=['sales','inventory'])
    # Cost version is selected as it was effective on each individual sale date.
    known_cost=D(0);covered_units=D(0);uncovered_units=D(0);cost_lines=[]
    for sale in sales:
        matches=[r for r in costs+invalid_cost_versions if r.get('sku')==sale.get('sku') and r['_day']<=sale['_day']]
        cost=max(matches,key=lambda r:(r['_day'],bool(r.get('_invalid')))) if matches else None
        if cost and cost.get('_invalid'):cost=None
        if cost:
            unit_cost=sum((cost[k] for k in COST_FIELDS),D(0));line=unit_cost*sale['units_ordered'];known_cost+=line;covered_units+=sale['units_ordered']
            cost_lines.append({'sku':sale.get('sku'),'sale_date':sale['_day'],'effective_date':cost['_day'],'units':sale['units_ordered'],'unit_cost':unit_cost,'line_cost':line})
        else:uncovered_units+=sale['units_ordered']
    cost_complete=units is not None and uncovered_units==0 and (bool(sales) or complete['sales'])
    metric('finance','ordered_revenue','订购销售额',revenue,'money','Σ ordered_revenue；广告归因销售额不再次相加',status=sales_status,reason=sales_reason,datasets=['sales'])
    metric('finance','known_unit_cost_total','已匹配单件成本合计',known_cost if sales else D(0) if complete['sales'] else None,'money','逐笔 units_ordered × 该 SKU 在销售日有效的五项单件成本之和',status='ready' if cost_complete and complete['sales'] else 'partial' if sales else 'missing',reason='未匹配成本的销售不视为零成本',inputs={'covered_units':covered_units,'uncovered_units':uncovered_units,'lines':cost_lines},datasets=['sales','costs'])
    ratio('finance','cost_coverage_percent','销量成本匹配率',covered_units,units,'已匹配有效成本的销量 ÷ 总销量 × 100',status='ready' if cost_complete else 'partial',datasets=['sales','costs'])
    contribution=revenue-known_cost if cost_complete and revenue is not None else None
    after_ads=contribution-totals['spend'] if contribution is not None and totals['spend'] is not None else None
    missing_costs=[] if cost_complete else ['effective_cost_for_every_sold_sku']
    finance_status='estimated' if complete['sales'] and complete['advertising'] else 'partial'
    for key,label,value,formula in [('contribution_before_ads','广告前已知成本贡献额',contribution,'订购销售额 − 销售日有效单件成本合计'),('contribution_after_ads','广告后已知成本贡献额',after_ads,'订购销售额 − 已知单件成本 − 广告花费')]:
        metric('finance',key,('扣 SP 花费后的已知成本贡献额' if sp_only else '扣已接入渠道广告花费后的已知成本贡献额') if key.endswith('after_ads') and not full_ad_scope else label,value,'money',formula,status=('estimated' if complete['sales'] and (key.endswith('before_ads') or complete['advertising'] and full_ad_scope) else 'partial') if value is not None else 'missing',reason='仅已知成本口径，未接全退款、税费、仓储及期间费用，不是真实净利润'+('；'+scope_reason if key.endswith('after_ads') else ''),missing=missing_costs+(['advertising'] if key.endswith('after_ads') and totals['spend'] is None else []),datasets=['sales','costs']+(['advertising'] if key.endswith('after_ads') else []))
    ratio('finance','break_even_acos','已知成本口径盈亏平衡 ACOS',contribution,revenue,'（订购销售额−已知单件成本）÷订购销售额×100',status='estimated',reason='不含未接入费用，仅供人工核对，不作为自动竞价依据',datasets=['sales','costs'])
    absent('finance','net_profit','真实净利润','完整收入 − 完整实际费用',['refunds','taxes','storage_fees','period_expenses','settlement_reconciliation'])
    absent('finance','settled_revenue','实际回款','实际结算与支付记录',['settlement_reports'])
    absent('service','refund_rate','退款率','退款订单数 ÷ 对应期间订单数 × 100',['refund_orders','orders_cohort'])
    absent('service','return_rate','退货率','退货件数 ÷ 对应销售批次件数 × 100',['return_reports','sales_cohort'])
    absent('compliance','verified_certifications','外部认证有效性','核验认证机构、适用型号和正式证据',['verified_external_certification_evidence'])
    def contextual(rows):return [r for r in rows or [] if isinstance(r,dict) and scoped(r)]
    task_rows=contextual(tasks)
    for module in ('shop','compliance','listing','ads','inventory','calendar','finance','service','review'):
        subset=task_rows if module=='review' else [r for r in task_rows if r.get('module_code')==module]
        active=[t for t in subset if t.get('status') not in {'completed','not_applicable'}]
        late=[]
        for t in active:
            try:
                if _day(t.get('due_date'),tz)<today:late.append(t)
            except (ValueError,TypeError):pass
        for key,label,count in [('open_tasks','未完成工作事项',len(active)),('overdue_tasks','逾期工作事项',len(late))]:
            metric(module,key,label,D(count) if tasks is not None else None,'count','当前事项中按状态与产品业务日核对的数量',status='ready' if tasks is not None else 'missing',reason='' if tasks is not None else '尚无工作事项来源',missing=[] if tasks is not None else ['tasks'],datasets=['tasks'])
        if late:modules[module]['findings'].append({'type':'overdue_work','severity':'warning','title':str(len(late))+' 项工作已逾期','evidence':{'task_ids':[str(t.get('id')) for t in late]},'action':'由当前负责人核实承诺日期、阻塞原因和下一步；不自动修改审批状态'})
    eligible=[t for t in task_rows if t.get('status')!='not_applicable']
    ratio('review','task_completion_percent','工作事项完成率',D(sum(t.get('status')=='completed' for t in eligible)) if tasks is not None else None,D(len(eligible)) if tasks is not None else None,'已完成事项数 ÷ 适用事项数 × 100',datasets=['tasks'])
    event_rows=contextual(events);event_count=0
    for e in event_rows:
        try:
            event_start=_day(e.get('start_date'),tz);event_end=_day(e.get('end_date') or e.get('start_date'),tz)
            if e.get('status')!='cancelled' and event_start<=end and event_end>=start:event_count+=1
        except (ValueError,TypeError):issue('invalid_event_date','events','排期中存在无效日期，已排除')
    metric('calendar','events_in_period','期间活动与里程碑',D(event_count) if events is not None else None,'count','开始和结束日期与所选期间有交集且未取消的活动数量',status='ready' if events is not None else 'missing',reason='' if events is not None else '未提供活动排期来源',missing=[] if events is not None else ['events'],datasets=['events'])
    rank_rows=[]
    for r in contextual(rankings):
        try:day=_day(r.get('observed_at') or r.get('date'),tz)
        except (ValueError,TypeError):continue
        rank=_decimal(r.get('rank_value'))
        if start<=day<=asof and rank is not None and rank>0:rank_rows.append(r)
    metric('review','rank_observations','期间排名观测数',D(len(rank_rows)) if rankings is not None else None,'count','按所选期间计数，排名类型保持分离',status='ready' if rankings is not None else 'missing',reason='观测数量不代表已建立可比较趋势；自然位、广告位和 BSR 不混合',missing=[] if rankings is not None else ['rankings'],inputs={'by_type':{kind:sum(r.get('observation_type')==kind for r in rank_rows) for kind in ['organic','sponsored','bsr']}},datasets=['rankings'])
    if not complete['sales']:issue('incomplete_days','sales','销售日期覆盖未完整确认；销量均速及补货结论受限')
    if not complete['advertising']:issue('incomplete_days','advertising','广告日期覆盖未完整确认；期间效果仅代表已有记录')
    if stale:issue('stale_inventory','inventory','库存快照超过允许时效')
    if missing_skus:issue('missing_inventory_skus','inventory','部分销售 SKU 没有可用库存快照')
    if uncovered_units:issue('missing_effective_cost','costs','部分销量找不到销售日有效的完整单件成本')
    times=[]
    for meta in coverage.values():
        if isinstance(meta,dict) and meta.get('last_synced_at'):
            try:
                refreshed_at=_moment(meta['last_synced_at'],tz).astimezone(timezone.utc)
                if refreshed_at<=moment:times.append(refreshed_at)
            except (ValueError,TypeError):pass
    quality='missing' if not any((sales,ads,stock,costs)) and not any(complete.values()) else 'partial' if issues else 'ready'
    return _json({'engine_version':ENGINE_VERSION,'product':{'id':product.get('id'),'model':product.get('model'),'currency':currency,'business_timezone':str(tz)},
        'period':{'start_date':start.isoformat(),'end_date':end.isoformat(),'as_of':asof.isoformat(),'timezone':str(tz)},'generated_at':moment.astimezone(timezone.utc).isoformat(),
        'data_quality':{'status':quality,'issues':sorted(issues,key=lambda x:(x['dataset'],x['code'])),'coverage':cov,'last_updated_at':max(times).isoformat() if times else None},
        'modules':modules,'sources':list(sources or [])})


def calculate_product_metrics(product,records,start_date,end_date,*,now=None,policies=None,coverage=None,tasks=None,events=None,rankings=None,sources=None):
    """Public deterministic calculator; Decimal precision covers all source-schema products."""
    with localcontext() as context:
        context.prec=64
        return _calculate_product_metrics(product,records,start_date,end_date,now=now,policies=policies,coverage=coverage,tasks=tasks,events=events,rankings=rankings,sources=sources)
