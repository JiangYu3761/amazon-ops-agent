/* Product-scoped reviews of actual, approved advertising changes. */
window.adsDecisionReview = (() => {
  'use strict';
  let active;
  const routeKey='homa.ads-review.route.1';
  function savedRoute(){try{return JSON.parse(sessionStorage.getItem(routeKey));}catch{return null;}}
  function remember(state){
    if(state.ctx.mode!=='server'||!state.ctx.user||!state.ctx.product)return;
    try{sessionStorage.setItem(routeKey,JSON.stringify({product:state.ctx.product.id,actor:state.ctx.user.id,
      view:state.view,id:state.id,lane:state.lane,period:state.period,asOf:state.asOf}));}catch{}
  }
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'})[c]);
  const controls = {none:'无对照',matched:'匹配对照',randomized:'随机对照'};
  const effects={unknown:'尚未查清',none:'未发现重大干扰',controlled:'已核实并控制',uncontrolled:'有未控制的重大干扰'};
  const confidence={low:'低',moderate:'中',high:'高'};
  const bases={attributed:'归因销售或订单',traffic:'流量指标',operational:'投放操作指标'};
  const directions={positive:'正向',negative:'负向',no_detected_effect:'未检出差异'};
  const dispositions={not_required:'本次无需跟进',stopped:'有理由终止观察',scheduled:'已约定下次复核',
    linked:'已关联实际跟进单',owner_pending:'等待原经办人安排后续处置'};
  const outcomes = {effective:'有效',ineffective:'无效',inconclusive:'证据不足'};
  const sourceStates = {approved:'已批准',executed:'已执行',closed:'已复核'};
  const events = {start:'启动复盘',assess:'完成评估',reopen:'重开复盘',link:'关联后续单'};
  const lanes = {open:'待评复盘',closed:'已完成',sources:'来源变更',period:'周期复盘'};
  const button = (name,label,id='',primary=false) =>
    '<button type="button" class="btn'+(primary?' primary':'')+'" data-ar="'+name+'" data-id="'+escape(id)+'">'+escape(label)+'</button>';
  const text = value => escape(value ?? '—');
  function fact(label,value) {
    return '<div><dt>'+escape(label)+'</dt><dd>'+text(value)+'</dd></div>';
  }
  function fields(rows){return '<dl class="ar-facts">'+rows.map(([k,v])=>fact(k,v)).join('')+'</dl>';}
  function person(state,entry,role) {
    return entry[role+'_name'] || state.ctx.personName(entry[role+'_id']);
  }
  function selectedDate(zone) {
    return new Intl.DateTimeFormat('sv-SE',{year:'numeric',month:'2-digit',day:'2-digit',timeZone:zone || 'UTC'}).format(new Date());
  }
  function input(name,label,type='text',options={}) {
    const id='ar-'+name, required=options.optional?'':' required';
    const common=' id="'+id+'" name="'+name+'"'+required;
    let widget;
    if(type==='textarea')widget='<textarea'+common+' rows="2" maxlength="4000"></textarea>';
    else if(type==='select')widget='<select'+common+'>'+options.values.map(([v,t])=>'<option value="'+escape(v)+'">'+escape(t)+'</option>').join('')+'</select>';
    else widget='<input'+common+' type="'+type+'"'+(options.value?' value="'+escape(options.value)+'"':'')+
      (options.max?' max="'+escape(options.max)+'"':'')+(type==='number'?' min="0" step="1"':'')+'>';
    return '<label for="'+id+'">'+escape(label)+widget+'</label>';
  }
  const form = (op,body,submit) => '<form data-ar-form="'+op+'" class="ar-form"><fieldset>'+body+
    '<div class="ar-actions"><button class="btn primary" type="submit">'+escape(submit)+'</button></div></fieldset></form>';
  function planFields() {
    return input('expected','预期变化','textarea')+input('baseline','变更前基线','textarea')+
      input('evidence','计划依据或报告引用','textarea')+
      '<div class="ar-grid">'+input('metric_basis','评估指标口径','select',{values:Object.entries(bases)})+input('observation_end','观察期结束日','date')+input('attribution_days','归因窗（天）','number',{max:'90'})+
      input('review_date','计划复核日','date')+input('control_method','对照方法','select',{values:Object.entries(controls)})+'</div>'+
      '<div data-ar-control hidden>'+input('control_evidence','对照组设置与凭证','textarea',{optional:true})+'</div>';
  }
  function planSummary(plan) {
    if(!plan)return '';
    return fields([['预期变化',plan.expected],['基线',plan.baseline],['计划依据',plan.evidence],['观察期结束',plan.observation_end],
      ['指标口径',bases[plan.metric_basis||'attributed']],['归因窗',plan.attribution_days+' 天'],['计划复核日',plan.review_date],['对照方法',controls[plan.control_method]],
      ...(plan.control_evidence?[['对照凭证',plan.control_evidence]]:[]),...(plan.feedback?[['登记时前案提示',plan.feedback.message],['曾采纳校准',plan.feedback.applied?'是':'否']]:[])]);
  }
  const periods={day:'日',week:'周',month:'月'};
  function periodControls(state) {
    const disabled=state.loading?' disabled':'';
    return '<form data-ar-period class="wb-toolbar ar-period"><label>统计周期<select name="period"'+disabled+'>'+Object.entries(periods).map(([key,label])=>
      '<option value="'+key+'" '+(key===state.period?'selected':'')+'>'+label+'</option>').join('')+'</select></label>'+
      '<label>业务截止日<input required type="date" name="as_of"'+disabled+' value="'+escape(state.asOf)+'" max="'+escape(selectedDate(state.ctx.product.business_timezone))+'"></label>'+
      '<button type="submit" class="btn"'+disabled+'>查看周期</button></form>';
  }
  function periodView(state) {
    const r=state.periodData;
    if(!r)return '';
    const scopeText=r.scope && typeof r.scope==='object'?r.scope.label:null;
    const description=[r.period_start+' 至 '+r.period_end,'统计截至 '+r.as_of,r.timezone,scopeText].filter(Boolean).join(' · ');
    return '<div class="ar-detail"><p class="wb-context">'+escape(description)+'</p><p class="wb-context">'+text(r.scope_note)+'</p><dl class="ar-rollups">'+r.metrics.map(m=>
      '<div><dt>'+text(m.label)+'</dt><dd>'+text(m.N===0?'—（0/0）':m.n+' / '+m.N)+'</dd><small>'+text(m.denominator)+'</small></div>').join('')+
      '</dl>'+(r.items.length?'<div class="wb-table-wrap"><table class="wb-table"><thead><tr><th>待办 / 来源</th><th>复核依据</th><th>负责人</th><th>到期日</th></tr></thead><tbody>'+
      r.items.map(i=>'<tr><td>'+(i.item_id?button('workitem',i.title,i.item_id):i.review_id?button('detail',i.title,i.review_id):i.source_id?button('workitem',i.title,i.source_id):text(i.title))+
        '<small>'+[i.kind_label,i.status_label].filter(Boolean).map(escape).join(' · ')+'</small></td><td>'+text(i.reason||i.conclusion_label||outcomes[i.conclusion]||i.conclusion)+'</td><td>'+text(i.owner_name||i.owner_id)+'</td><td>'+text(i.due_date)+'</td></tr>').join('')+
      '</tbody></table></div>':'<p>当前周期在你的可见范围内没有待办。</p>')+(r.next_offset!=null?button('more','加载更多周期事项'):'')+'</div>';
  }
  function sourceRow(state,item) {
    const title=escape(item.title), meta=[item.number,item.site,item.object_ref].filter(Boolean).map(escape).join(' · ');
    return '<tr><td><button type="button" class="wb-link" data-ar="source" data-id="'+escape(item.id)+'">'+title+'</button><small>'+meta+'</small></td>'+
      '<td>'+text(sourceStates[item.status]||item.status)+'</td><td>'+text(person(state,item,'owner'))+'</td>'+
      '<td>'+text(item.review_id?'已启动':item.plan?'已预登记':'尚未登记')+'</td></tr>';
  }
  function reviewRow(state,r) {
    const date=r.assessment?.next_review_date||(r.status==='open'?r.plan?.review_date:null);
    return '<tr><td><button type="button" class="wb-link" data-ar="detail" data-id="'+escape(r.id)+'">'+text(r.source_title||r.object_ref)+'</button><small>'+
      [r.source_number,r.site,r.object_ref].filter(Boolean).map(escape).join(' · ')+'</small></td><td>'+text(r.status==='closed'?(outcomes[r.assessment?.outcome]||'已完成'):'待评')+
      '</td><td>'+text(person(state,r,r.disposition==='owner_pending'?'owner':'reviewer'))+'</td><td>'+text(date)+(r.disposition==='owner_pending'?'<small>待安排后续</small>':'')+'</td></tr>';
  }
  function queue(state) {
    const {lane,rows,next}=state;
    if(lane==='period')return periodView(state);
    if(!rows.length) {
      const empty={open:'当前产品没有待评复盘。',closed:'当前产品还没有已完成的复盘。',sources:'当前范围没有可见的已批准广告变更。'};
      return '<div class="ar-blank"><p>'+empty[lane]+'</p>'+(lane==='open'?button('lane','查看来源变更','sources'):'')+'</div>';
    }
    const source=lane==='sources';
    return '<div class="wb-table-wrap"><table class="wb-table"><thead><tr><th>来源变更 / 对象</th><th>状态</th><th>'+
      (source?'经办人':'当前负责人')+'</th><th>'+(source?'复盘登记':'复核日')+'</th></tr></thead><tbody>'+
      rows.map(row=>source?sourceRow(state,row):reviewRow(state,row)).join('')+'</tbody></table></div>'+
      (next!=null?'<div class="ar-actions">'+button('more','加载更多')+'</div>':'');
  }
  function sourceDetail(state,s) {
    let content=fields([['广告对象',s.object_ref],['站点',s.site],['经办人',person(state,s,'owner')],['独立复核人',person(state,s,'reviewer')],['变更状态',sourceStates[s.status]||s.status]]);
    if(s.section_id==='change'||!s.section_id)content+='<section class="ar-prior"><h3>前案提示</h3><div data-ar-prior></div></section>';
    if(s.plan)content+='<h3>已登记计划</h3>'+planSummary(s.plan);
    if(s.review_id)content+=button('detail','打开复盘',s.review_id,true);
    else if(s.can_plan)content+='<h3>执行前登记计划</h3>'+form('plan',planFields(),'登记并锁定计划');
    else if(s.can_start){
      content+=s.plan?form('start','<p>按执行前登记的计划启动复盘。</p>','启动复盘'):
        '<h3>执行后补登记</h3><p>凭执行回执记录基线和观察计划。补登记仅支持观察结论。</p>'+form('start',planFields(),'补登记并启动复盘');
    } else content+='<p class="ar-note">'+(s.status==='approved'?'原经办人可在执行前登记计划，执行回执登记后可启动复盘。':'原经办人可凭执行回执启动复盘。')+'</p>';
    return '<div class="ar-head"><h3>'+text(s.title)+'</h3>'+button('workitem','查看来源单',s.id)+'</div>'+content;
  }
  async function previousCase(state,source,host){
    const serial=state.serial;
    host.textContent='正在核对同一广告对象的前案…';
    try{
      const r=await state.ctx.api('/api/products/'+encodeURIComponent(state.ctx.product.id)+'/ads-review-feedback/sources/'+encodeURIComponent(source.id));
      if(state!==active||state.serial!==serial||!host.isConnected)return;
      state.feedback=r;
      host.innerHTML='<p>'+text(r.message)+'</p>'+
        (r.evidence_message?'<p class="ar-note '+(r.evidence_status==='current'?'':'ar-error')+'">'+text(r.evidence_message)+'</p>':'')+
        (r.reference?.review_id?button('detail','查看前案 '+r.reference.number+(r.reference.signed_version?' · 签署 v'+r.reference.signed_version:''),r.reference.review_id):
         r.reference?.source_id?button('workitem','查看前案来源 '+r.reference.number,r.reference.source_id):'')+
        (r.calibration?fields([['候选设置',r.calibration.tested_setting],['校准口径',bases[r.calibration.metric_basis]],
          ['校准归因窗',r.calibration.attribution_days+' 天'],['对照方法',controls[r.calibration.control_method]]]):'')+
        (source.can_plan&&r.state==='candidate'&&r.candidate_version?
          '<label class="ar-choice"><input type="checkbox" data-ar-calibration value="'+escape(r.candidate_version)+'"> 本次计划采纳该校准候选</label>':'');
    }catch(error){
      if(state!==active||!host.isConnected)return;
      host.innerHTML='<p>'+text(readable(error))+'</p>'+button('prior','重查前案');
    }
  }
  function assessForm(r,state) {
    const date=selectedDate(state.ctx.product.business_timezone);
    return form('assess','<div class="ar-grid">'+input('as_of','证据截至日','date',{value:date,max:date})+
      input('refreshed_on','来源报告最后刷新日','date',{optional:true,max:date})+
      input('outcome','观察结论','select',{values:[['','请选择'],...Object.entries(outcomes)]})+'</div>'+
      input('actual','实际表现及基线对比','textarea')+input('interference','同期干扰与替代解释','textarea')+
      '<div class="ar-grid">'+input('material_interference','重大干扰核查','select',{values:Object.entries(effects)})+
      input('confidence','证据可信度','select',{values:Object.entries(confidence)})+'</div>'+
      input('guardrails','预算、库存及利润边界核对','textarea')+input('evidence','独立核验的报告或回执','textarea')+
      '<div class="ar-checks"><label><input type="checkbox" name="execution_ok"> 已核实执行生效</label>'+
      '<label><input type="checkbox" name="guardrail_ok"> 经营边界已通过</label></div>'+
      '<p class="ar-note" data-ar-gate aria-live="polite"></p>'+
      (r.basis==='prospective'&&r.plan?.control_method==='randomized'?
        '<label class="ar-checkbox"><input type="checkbox" name="incremental"> 有可归因的实验结论</label><div data-ar-direction hidden>'+input('effect_direction','实验效果方向','select',{values:Object.entries(directions)})+'</div>':'')+
      '<div data-ar-increment hidden>'+input('control_result','实际对照结果或干扰控制凭证','textarea',{optional:true})+'</div>'+
      input('next_review_date','下次复核日（如需跟踪）','date',{optional:true})+
      input('stop_reason','终止观察的原因（如不再跟进）','textarea',{optional:true}),'提交独立评估');
  }
  function history(r) {
    return '<details class="ar-history"><summary>办理历史（'+(r.history||[]).length+'）</summary><ol>'+
      [...(r.history||[])].sort((a,b)=>b.version-a.version).map(h=>'<li><b>v'+text(h.version)+' · '+text(events[h.action]||h.action)+'</b>　'+
        text(h.actor_name)+'　'+text(h.created_at?new Date(h.created_at).toLocaleString('zh-CN',{hour12:false}):'')+
        (h.payload?.reason?'<p>重开原因　'+text(h.payload.reason)+'</p>':'')+
        (h.payload?.outcome?'<details><summary>当次结论 · '+text(outcomes[h.payload.outcome]||h.payload.outcome)+'</summary>'+
          fields([['实际表现',h.payload.actual],['复核证据',h.payload.evidence],['干扰',h.payload.interference],['经营边界',h.payload.guardrails],
          ['证据截至',h.payload.as_of],['报告刷新',h.payload.refreshed_on],['下次复核',h.payload.next_review_date],['终止原因',h.payload.stop_reason||null]])+'</details>':'')+'</li>').join('')+'</ol></details>';
  }
  function linked(state,r) {
    const links=r.links||[];
    return '<details class="ar-history"><summary>后续办理（'+links.length+'）</summary>'+
      (links.length?'<ul>'+links.map(l=>'<li>'+button('workitem',l.title||l.number,l.id)+'　'+text(l.kind==='strategy_candidate'?'策略候选':'跟进')+' · '+text(l.owner_name||state.ctx.personName(l.owner_id))+'</li>').join('')+'</ul>':'<p>尚无已关联的后续单。</p>')+
      (r.can_link?'<div class="ar-actions">'+button('child','新建广告跟进单')+button('linkOptions','选择已有子单')+'</div><div data-ar-child-options></div>':'')+'</details>';
  }
  function detail(state,r) {
    const a=r.assessment,limit=r.basis==='retrospective'?'执行后补登记，仅可记录观察结果。':
      r.plan?.control_method!=='randomized'?'当前计划未登记随机对照，结果仅说明观察期变化。':'增量判断需核实归因成熟、执行生效、经营边界及实际对照结果。';
    let html='<div class="ar-head"><h3>'+text(r.source_title||r.object_ref)+'</h3>'+button('workitem','查看来源单',r.source_id)+'</div>'+
      fields([['状态',r.status==='closed'?'已完成':'待评'],['广告对象',r.object_ref],['经办人',person(state,r,'owner')],['复核人',person(state,r,'reviewer')],
      ['归因成熟日',r.matures_on],['下次复核',a?.next_review_date||(r.status==='open'?r.plan?.review_date:null)]])+
      '<p class="ar-note">'+escape(limit)+'</p><details class="ar-history"><summary>计划与执行凭证</summary>'+planSummary(r.plan)+
      fields([['执行凭证',r.execution?.evidence],['登记时间',r.execution?.created_at]])+'</details>';
    if(r.plan?.feedback?.applied)html+='<section class="ar-prior"><h3>已采纳的校准依据</h3><div data-ar-prior></div></section>';
    if(a)html+='<h3>本次评估</h3>'+fields([['结论',outcomes[a.outcome]||a.outcome],['证据截至',a.as_of],
      ['实际表现',a.actual],['同期干扰',a.interference],['干扰核查',effects[a.material_interference]],['证据可信度',confidence[a.confidence]],['来源刷新日',a.refreshed_on],['经营边界',a.guardrails],['证据',a.evidence],['后续处置',dispositions[r.disposition]],...(a.stop_reason?[['终止原因',a.stop_reason]]:[]),
      ['实验结论',a.incremental?(directions[a.effect_direction]||'已记录'):'仅观察结论'],...(a.control_result?[['对照结果',a.control_result]]:[])]);
    if(r.can_assess&&r.status==='open')html+='<h3>独立评估</h3>'+assessForm(r,state);
    else if(r.status==='open')html+='<p class="ar-note">等待 '+text(person(state,r,'reviewer'))+' 提交独立评估。</p>';
    if(r.can_reopen&&r.status==='closed')html+='<details class="ar-history"><summary>重开复盘</summary>'+form('reopen',input('reason','重开原因及待核事项','textarea'),'确认重开')+'</details>';
    return html+linked(state,r)+history(r);
  }
  function frame(state) {
    const {node,ctx}=state;
    node.innerHTML='<section class="panel wb-queue ar-panel" aria-labelledby="ar-title"><header><div><h2 id="ar-title">广告变更复盘</h2>'+
      (ctx.product?'<p>'+escape(ctx.product.model)+' · '+escape(ctx.product.marketplace||'')+'</p>':'')+'</div><div data-ar-tools></div></header>'+
      '<div data-ar-message role="status" aria-live="polite"></div><div data-ar-body></div></section>';
    state.msg=node.querySelector('[data-ar-message]');state.body=node.querySelector('[data-ar-body]');
    state.tools=node.querySelector('[data-ar-tools]');
  }
  function notice(state,message='',error=false) {
    if(active!==state)return;
    state.msg.textContent=message;state.msg.className=message?'ar-message'+(error?' ar-error':''):'';
  }
  function readable(error) {
    return error.status===409?error.message+'。可刷新读取最新版本。':error.message;
  }
  function draw(state) {
    if(active!==state)return;
    const {ctx,body}=state;
    remember(state);
    state.tools.innerHTML=ctx.mode==='server'&&ctx.user&&ctx.product?button('refresh','刷新复盘'):'';
    if(ctx.mode!=='server'){body.innerHTML='<p class="wb-empty">连接工作台服务器后，可使用真实变更单办理复盘。</p>';return;}
    if(!ctx.user){body.innerHTML='<p class="wb-empty">登录后查看你经办或审核的广告变更。 '+button('login','登录')+'</p>';return;}
    if(!ctx.product){body.innerHTML='<p class="wb-empty">请在顶部选择具体产品，查看该产品的广告复盘。</p>';return;}
    if(state.view==='list') {
      const nav=Object.entries(lanes).map(([k,v])=>'<button type="button" class="wb-tab '+(k===state.lane?'selected':'')+
        '" aria-pressed="'+(k===state.lane)+'" data-ar="lane" data-id="'+k+'">'+v+'</button>').join('');
      body.innerHTML='<nav class="ar-tabs" aria-label="广告复盘队列">'+nav+'</nav>'+(state.lane==='period'?periodControls(state):'')+
        (state.loading?'<p class="wb-empty">正在读取…</p>':state.failed?'<p class="wb-empty">'+button('refresh','重新读取')+'</p>':queue(state));
    }else body.innerHTML='<div class="ar-detail"><div class="ar-back">'+button('back','返回复盘列表')+'</div>'+
      (state.loading?'<p>正在读取…</p>':state.failed?button('refresh','重新读取'):
       state.view==='source'?sourceDetail(state,state.record):detail(state,state.record))+'</div>';
    const host=body.querySelector('[data-ar-prior]');
    if(host)previousCase(state,state.view==='source'?state.record:{id:state.record.source_id},host);
    const assess=body.querySelector('form[data-ar-form="assess"]');
    if(assess)syncInputs(assess);
    const planForm=body.querySelector('form[data-ar-form=plan], form[data-ar-form=start]');
    if(planForm)syncInputs(planForm);
  }
  async function read(state,more=false) {
    const n=++state.serial;
    state.loading=!more;state.failed=false;notice(state);draw(state);
    try {
      if(state.view==='list'&&state.lane==='period'){
        const params=new URLSearchParams({period:state.period,as_of:state.asOf,offset:String(more?state.periodData.next_offset:0)});
        const result=await state.ctx.api('/api/products/'+encodeURIComponent(state.ctx.product.id)+'/ads-review-periods?'+params);
        if(n!==state.serial||state!==active)return;
        state.periodData=more?{...result,items:[...state.periodData.items,...result.items]}:result;
      } else if(state.view==='list'||state.view==='source') {
        const sources=state.view==='source'||state.lane==='sources';
        const params=new URLSearchParams({limit:'50',offset:String(more?state.next:0)});
        if(!sources)params.set('status',state.lane);
        if(state.view==='source') {
          // The selected row was read with the authenticated source queue. Refresh it across pages when needed.
          let offset=0, row=null;
          do {
            params.set('offset',String(offset));
            const page=await state.ctx.api(state.base+'/sources?'+params);
            if(n!==state.serial||state!==active)return;
            row=page.items.find(r=>r.id===state.id);
            offset=page.next_offset;
          }while(!row&&offset!=null);
          if(!row)throw new Error('来源单已移出当前可见范围。');
          state.record=row;
        }else{
          const page=await state.ctx.api(state.base+(sources?'/sources':'')+'?'+params);
          if(n!==state.serial||state!==active)return;
          state.rows=more?[...state.rows,...page.items]:page.items;state.next=page.next_offset;
        }
      }else {
        const result=await state.ctx.api(state.base+'/'+encodeURIComponent(state.id));
        if(n!==state.serial||state!==active)return;
        state.record=result;
      }
    } catch(error) {
      if(n!==state.serial||state!==active)return;
      state.failed=true;notice(state,readable(error),true);
      if(error.status===401)state.msg.append(' ',loginButton());
    } finally {if(n===state.serial&&state===active){state.loading=false;draw(state);}}
  }
  function loginButton() {
    const el=document.createElement('button');el.type='button';el.className='btn';el.dataset.ar='login';el.textContent='重新登录';return el;
  }
  async function childOptions(state) {
    const host=state.node.querySelector('[data-ar-child-options]'), serial=state.serial;
    host.textContent='正在读取子单…';
    try{
      const source=await state.ctx.api('/api/workbench/items/'+encodeURIComponent(state.record.source_id));
      if(active!==state||serial!==state.serial||!host.isConnected)return;
      const linkedIds=new Set((state.record.links||[]).map(l=>l.id)),children=(source.child_refs||[]).filter(c=>!linkedIds.has(c.id));
      host.innerHTML=children.length?form('links',input('item_id','后续子单','select',{values:children.map(c=>[c.id,c.number+' · '+c.title])})+
        input('kind','关联用途','select',{values:[['follow_up','跟进办理'],['strategy_candidate','广告策略候选']]}),'关联子单'):'<p>当前没有可关联的子单。可新建跟进单，保存后再关联。</p>';
    }catch(error){if(active===state&&host.isConnected)host.textContent=readable(error);}
  }
  function syncInputs(f) {
    if(f.elements.metric_basis){f.elements.attribution_days.min=f.elements.metric_basis.value==='attributed'?'1':'0';}
    const c=f.elements.control_method, control=f.querySelector('[data-ar-control]');
    if(control){control.hidden=c.value==='none';f.elements.control_evidence.required=!control.hidden;}
    const inc=f.elements.incremental, incr=f.querySelector('[data-ar-increment]');
    const direction=f.querySelector('[data-ar-direction]');
    if(direction)direction.hidden=!inc?.checked;
    if(incr){incr.hidden=!inc?.checked&&f.elements.material_interference?.value!=='controlled';f.elements.control_result.required=!incr.hidden;}
    const gate=f.querySelector('[data-ar-gate]');
    if(gate){
      const e=f.elements, immature=e.as_of.value<(active.record.matures_on||'');
      const need=immature?'证据截至日尚未覆盖归因成熟日':!e.refreshed_on.value||e.refreshed_on.value<(active.record.matures_on||'')?'报告尚无归因成熟后的刷新凭证':
        !e.execution_ok.checked?'尚未确认执行生效':['unknown','uncontrolled'].includes(e.material_interference.value)?'重大干扰尚未排除':e.confidence.value==='low'?'证据可信度较低':'';
      const high=need||(!e.guardrail_ok.checked?'尚未确认经营边界通过':'');
      gate.textContent=need?need+'，可登记证据不足。':high?high+'，当前无法确认有效。':'可按本次证据评估观察结果。';
      const problem=e.outcome.value==='effective'?high:e.outcome.value==='ineffective'?need:'';
      e.outcome.setCustomValidity(problem);
      e.stop_reason.setCustomValidity(e.stop_reason.value.trim()&&e.next_review_date.value?'请选择下次复核日期或终止观察原因其中一项。':'');
      if(inc){
        const incompatible=(e.outcome.value==='effective'&&e.effect_direction.value!=='positive')||e.outcome.value==='inconclusive';
        inc.setCustomValidity(inc.checked&&(e.confidence.value!=='high'||incompatible)?'可归因的实验结论需要高可信证据、明确的观察结果及相符方向。':'');
      }
    }
  }
  async function submit(state,f) {
    if(state.writing||!f.reportValidity())return;
    const op=f.dataset.arForm,record=state.record;
    const body=Object.fromEntries(new FormData(f));
    if('attribution_days' in body)body.attribution_days=Number(body.attribution_days);
    if(body.control_method==='none')delete body.control_evidence;
    if(op==='assess'){
      for(const name of ['execution_ok','guardrail_ok','incremental'])body[name]=Boolean(f.elements[name]?.checked);
      if(!body.incremental)delete body.effect_direction;
      if(!body.incremental&&body.material_interference!=='controlled')delete body.control_result;
      if(!body.refreshed_on)delete body.refreshed_on;
      if(!body.next_review_date)delete body.next_review_date;
      if(!body.stop_reason?.trim())delete body.stop_reason;
    }
    const calibration=state.body.querySelector('[data-ar-calibration]:checked');
    if(op==='plan'&&calibration)body.calibration_version=calibration.value;
    const isSource=op==='plan'||op==='start';
    if(!isSource)body.expected_version=record.version;
    const path=state.base+(isSource?'/sources/':'/')+encodeURIComponent(record.id)+'/'+op;
    const fingerprint=JSON.stringify([path,body]);
    if(state.writeKey!==fingerprint){state.writeKey=fingerprint;state.requestId=crypto.randomUUID();}
    body.request_id=state.requestId;
    f.querySelector('fieldset').disabled=true;state.writing=true;
    notice(state,'正在保存…');
    try{
      const result=await state.ctx.api(path,{method:'POST',body:JSON.stringify(body)});
      if(active!==state)return;
      state.writeKey=null;
      if(op==='plan'){await read(state);}
      else {state.view='detail';state.id=result.id;state.record=result;state.failed=false;state.loading=false;++state.serial;draw(state);}
      if(!state.failed)notice(state,{plan:'事前计划已登记。',start:'复盘已启动。',assess:'独立评估已保存。',reopen:'复盘已重开，历史版本已保留。',links:'后续子单已关联。'}[op]);
    }catch(error){
      if(active!==state)return;
      notice(state,readable(error),true);
      if(error.status===401)state.msg.append(' ',loginButton());
    }finally{state.writing=false;if(f.isConnected)f.querySelector('fieldset').disabled=false;}
  }
  function mount(node,ctx) {
    const key=[ctx.mode,ctx.user?.id,ctx.product?.id].join('/');
    if(active?.node===node&&active.key===key)return;
    unmount();
    const state={ctx,node,key,base:'/api/products/'+encodeURIComponent(ctx.product?.id||'')+'/ads-reviews',
      view:'list',lane:'open',period:'week',asOf:selectedDate(ctx.product?.business_timezone),rows:[],serial:0,loading:false,writing:false};
    const saved=savedRoute();
    if(saved&&saved.product===ctx.product?.id&&saved.actor===ctx.user?.id&&Object.hasOwn(lanes,saved.lane)){
      state.lane=saved.lane;
      if(Object.hasOwn(periods,saved.period)&&/^\d{4}-\d{2}-\d{2}$/.test(saved.asOf)){state.period=saved.period;state.asOf=saved.asOf;}
      if(saved.id&&['source','detail'].includes(saved.view)){state.id=saved.id;state.view=saved.view;}
    }
    active=state;frame(state);
    node.onclick=ev=>{
      const el=ev.target.closest('button[data-ar]');
      if(!el||state.writing)return;
      const {ar:act,id}=el.dataset;
      if(act==='login')return ctx.login();
      if(act==='workitem')return ctx.openItem(id);
      if(act==='child'){
        const r=state.record;
        return ctx.createChild({product_id:ctx.product.id,page_id:'ads',section_id:'change',parent_id:r.source_id,
          title:'跟进 '+r.source_title,site:r.site,object_ref:r.object_ref});
      }
      if(act==='linkOptions')return childOptions(state);
      if(act==='prior')return previousCase(state,state.view==='source'?state.record:{id:state.record.source_id},state.body.querySelector('[data-ar-prior]'));
      if(act==='source'){state.view='source';state.id=id;state.record=state.rows.find(r=>r.id===id);++state.serial;notice(state);return draw(state);}
      if(act==='detail'){state.view='detail';state.id=id;}
      if(act==='back'||act==='lane'){state.view='list';if(act==='lane')state.lane=id;}
      if(['detail','back','lane','refresh','more'].includes(act))read(state,act==='more');
    };
    node.onchange=ev=>{
      if(ev.target.matches('[data-ar-calibration]')&&ev.target.checked&&state.feedback?.calibration){
        const plan=node.querySelector('form[data-ar-form="plan"]');
        if(plan){for(const field of ['metric_basis','attribution_days','control_method'])plan.elements[field].value=state.feedback.calibration[field];syncInputs(plan);}
      }
      const f=ev.target.closest('form[data-ar-form]');if(f)syncInputs(f);
    };
    node.onsubmit=ev=>{
      const f=ev.target;
      if(f.matches('[data-ar-period]')){ev.preventDefault();state.period=f.elements.period.value;state.asOf=f.elements.as_of.value;read(state);}
      else if(f.matches('[data-ar-form]')){ev.preventDefault();submit(state,f);}
    };
    if(ctx.mode==='server'&&ctx.user&&ctx.product)read(state);else draw(state);
  }
  function unmount(forget=false){
    if(forget){try{sessionStorage.removeItem(routeKey);}catch{}}
    if(active){active.node.onclick=active.node.onchange=active.node.onsubmit=null;active.node.replaceChildren();active=null;}
  }
  return {mount,unmount,savedRoute};
})();
