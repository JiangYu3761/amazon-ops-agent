/* Product-scoped approval sheet. Opening it never changes the SOP route or filters. */
window.approvalsUI = (() => {
  const $a = s => document.querySelector(s);
  const all = s => Array.from(document.querySelectorAll(s));
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const types = {text:'单行文字',textarea:'多行说明',number:'数字',date:'日期',select:'单选',multiselect:'多选',person:'人员',location:'地点',attachment:'附件'};
  const kindLabels = {application:'申请',approval:'审批',handling:'办理',cc:'抄送',end:'结束'};
  const stateLabels = {draft:'草稿',in_progress:'流转中',completed:'已完成',waiting:'未到达',pending:'待处理',returned:'退回补充'};
  const eventLabels = {handled:'提交办理结果',notified:'抄送通知',ended:'完成归档',configured:'调整审批配置',configuration_changed:'调整审批配置',save:'保存草稿',saved:'保存草稿',draft_saved:'保存草稿',submit:'提交申请',submitted:'提交申请',accept:'确认接手',accepted:'确认接手',complete:'提交办理结果',stage_completed:'完成办理',approve:'审批通过',approved:'审批通过',reject:'退回补充',rejected:'退回补充',reopen:'重新打开',reopened:'重新打开',attachment_uploaded:'上传附件',plan_changed:'调整预计完成时间',case_created:'建立审批单',configured_recall:'调整配置并撤回'};
  let data = null, selected = null, panelTab = 'work', configTab = '__application', configDraft = null;
  let applicationAnswers = {}, stageAnswers = {}, progressDraft = {}, opinionDraft = {}, dirty = false, busy = false, previousFocus = null, requestSerial = 0, isDemo = false;
  const plain = x => x === null || x === undefined || x === '' ? '待填写' : String(x);
  const dateOnly = x => !x?'未排期':String(x).includes('T')?new Date(x).toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'}):String(x).slice(0,10);
  const localEvent = x => x ? new Date(x).toLocaleString('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).replace(' ','T') : '';
  const localEventTime = x => x ? new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(x)).replace(' ','T') : '';
  const datetime = x => x ? new Date(x).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) : '尚未发生';
  const button = (label, action, primary=false, attrs='') => `<button type="button" class="btn ${primary?'primary':''}" data-oa="${action}" ${attrs}>${label}</button>`;
  const svg = name => typeof icon === 'function' ? icon(name) : '';
  const name = id => data?.people?.find(p=>p.id===id)?.display_name || (typeof ws!=='undefined' ? ws.people?.find(p=>p.id===id)?.display_name : '') || '待分配';
  const names = ids => ids?.length ? ids.map(name).join('、') : '待分配';
  const stageConfig = () => data?.config?.stages?.find(s=>s.key===selected);
  const stageRuntime = key => data?.case?.stages?.find(s=>s.key===key);
  const currentKey = () => data?.has_case&&!data?.user?null:(data?.case?.status==='completed'?data?.config?.stages?.at(-1)?.key:data?.case?.current_stage_key || data?.config?.stages?.[0]?.key);
  const userId = () => data?.user?.id;
  function portal() {let root=$a('#approvalPortal');if(!root){root=document.createElement('div');root.id='approvalPortal';document.body.append(root);}return root;}
  function notify(message) {if(typeof toast==='function')toast(message);}
  function endpoint(suffix='') {return `/api/approvals/tasks/${data.task.id}${suffix}`;}
  function isCurrent() {return selected===currentKey() && data?.case?.status!=='completed' && !(!data?.case&&['completed','not_applicable'].includes(data?.task?.status));}
  function executor(stage=stageConfig()) {return !!userId() && [...(stage?.assignee_ids||[]),...(stage?.collaborator_ids||[])].includes(userId());}
  function reviewer(stage=stageConfig()) {
    if(!userId() || !(stage?.reviewer_ids||[]).includes(userId()))return false;
    const decisions=stageRuntime(stage.key)?.decisions||[];
    if(decisions.some(d=>(d.user_id||d.reviewer_id)===userId()&&d.decision==='approved'))return false;
    if(stage.review_mode==='sequential'){
      const done=new Set(decisions.filter(d=>d.decision==='approved').map(d=>d.user_id||d.reviewer_id));
      return stage.reviewer_ids.find(id=>!done.has(id))===userId();
    }
    return true;
  }
  function canEdit() {const s=stageConfig();return !isDemo && !busy && isCurrent() && (s?.type==='approval'?reviewer(s):executor(s));}
  function seedDrafts() {
    applicationAnswers=structuredClone(data.case?.application_answers||{});
    stageAnswers={};progressDraft={};opinionDraft={};
    for(const stage of data.config.stages){const runtime=stageRuntime(stage.key)||{};stageAnswers[stage.key]=structuredClone(runtime.answers||{});progressDraft[stage.key]={forecast_end:runtime.forecast_end||'',delay_reason:runtime.delay_reason||'',next_action:runtime.next_action||'',external_event_at:localEventTime(runtime.external_event_at)};}
    dirty=false;
  }
  async function open(taskId) {
    if(dirty && !confirm('有尚未保存的内容，确认打开其他事项？'))return;
    previousFocus=document.activeElement;const serial=++requestSerial;
    portal().innerHTML='<div class="oa-page oa-loading" role="dialog" aria-modal="true"><p>正在读取产品审批单、预设阶段与流转记录…</p></div>';
    try{
      isDemo=typeof mode!=='undefined'&&mode==='demo';
      data=isDemo?await demoDetail(taskId):await api(`/api/approvals/tasks/${taskId}`);
      if(serial!==requestSerial)return;
      normalize();selected=currentKey()||data.config.stages[0]?.key;panelTab='work';configDraft=null;seedDrafts();draw(false);
      $a('.oa-back')?.focus();
    }catch(error){if(serial!==requestSerial)return;portal().innerHTML=`<section class="oa-page oa-failure" role="dialog" aria-modal="true"><div>${svg('flag')}<h2>审批单未能读取</h2><p>${esc(error.message)}</p>${button('返回 SOP 清单','close')}</div></section>`;}
  }
  function normalize() {
    data.config ||= {version:0,fields:data.template?.fields||[],stages:data.template?.stages||[]};
    data.config.fields ||= data.template?.fields||[];data.config.stages ||= data.template?.stages||[];
    data.people ||= [];data.history ||= [];data.permissions ||= {};data.product ||= {};
    for(const s of data.config.stages){s.fields ||= [];s.assignee_ids ||= [];s.reviewer_ids ||= [];s.collaborator_ids ||= [];s.review_mode ||= 'all';}
  }
  async function demoDetail(taskId) {
    const task=ws.tasks.find(t=>t.id===taskId);if(!task)throw new Error('未找到这项产品事项');
    const templates=await fetch('approval-templates.json').then(r=>{if(!r.ok)throw new Error('未能读取业务审批模板');return r.json();});
    const key=task.sop_key;
    let template=templates.tasks?.[key] || templates.templates?.[key] || templates[key] || templates.fallback?.[task.module_code];
    if(Array.isArray(templates.templates))template=templates.templates.find(t=>t.key===key)||template;
    if(!template)throw new Error('当前示例事项暂未找到对应业务模板，请连接服务器读取完整模板');
    return {task,product:ws.products.find(p=>p.id===task.product_id),template,config:{version:0,fields:template.fields,stages:template.stages.map(s=>({...s,assignee_ids:[],reviewer_ids:[],collaborator_ids:[]}))},case:null,user:null,people:[],permissions:{configure:false},history:[]};
  }
  async function refresh({preserve=false}={}) {
    if(!data||isDemo)return;
    const result=await api(endpoint());data=result;normalize();
    if(!data.config.stages.some(s=>s.key===selected))selected=currentKey();
    if(!preserve)seedDrafts();
    if(typeof refreshWorkspace==='function')await refreshWorkspace(false);
    if(typeof render==='function')render();
    draw(true);
  }
  function close(force=false) {
    if(busy&&!force)return;
    if(dirty&&!force&&!confirm('有尚未保存的内容，确认返回 SOP 清单？'))return;
    requestSerial++;portal().innerHTML='';data=null;dirty=false;configDraft=null;previousFocus?.focus();
  }
  function draw(keepScroll=true) {
    if(!data)return;const scroll=keepScroll?($a('.oa-scroll')?.scrollTop||0):0;
    const state=data.case?.status||(data.has_case?'private':['completed','not_applicable'].includes(data.task.status)?'legacy_completed':'not_started');const stateName={private:'已有审批单 · 登录查看',not_started:'尚未发起',draft:'申请草稿',in_progress:'审批流转中',completed:'已完成',legacy_completed:'旧版已完成'}[state]||state;
    const current=data.config.stages.find(s=>s.key===currentKey());const runtime=stageRuntime(current?.key)||{};
    const pendingIds=current?.type==='approval'?current.reviewer_ids:current?.assignee_ids;
    portal().innerHTML=`<section class="oa-page" role="dialog" aria-modal="true" aria-labelledby="oaTitle">
      <header class="oa-topbar"><button class="oa-back" data-oa="close">${svg('arrow')}<span>返回全流程 SOP</span></button><div class="oa-breadcrumb">${esc(data.product.model||'当前产品')}<span>/</span>${esc(data.template?.name||data.task.title)}</div><div class="oa-header-actions"><button type="button" class="btn oa-upgrade-entry" data-action="upgrade" aria-label="版本与升级">${svg('upload')}<span>版本与升级</span></button>${isDemo?'<span class="oa-preview-label">本机示例 · 只读预览</span>':!data.user?button('登录处理审批','login'):''}${data.permissions.configure?button(svg('settings')+'配置审批流程','configure'):''}${button(svg('clock')+'流转记录','history')}</div></header>
      <div class="oa-scroll"><div class="oa-content">
      <section class="oa-heading"><div class="oa-document-icon">${svg('file')}</div><div class="oa-title-group"><div class="oa-title-kicker">PRODUCT APPROVAL · ${esc(data.product.model||'')} ${data.product.data_source==='sample'?'<span>示例产品</span>':''}</div><div class="oa-title-row"><h1 id="oaTitle">${esc(data.template?.name||data.task.title)}<span> · ${esc(data.product.model||'')}</span></h1><span class="oa-case-state ${esc(state)}">${stateName}</span></div><p>${esc(data.task.acceptance_criteria||'按阶段提交资料、处理交接并完成指定人员验收。')}</p></div></section>
      <section class="oa-meta"><div><span>审批单号</span><strong>${esc(data.case?.number||(data.has_case?'登录后查看':'提交后生成'))}</strong></div><div><span>发起人 / 发起时间</span><strong>${data.case?esc(name(data.case.initiator_id)):data.has_case?'登录后查看':'尚未发起'}</strong><small>${data.case?datetime(data.case.created_at):data.has_case?'已有业务记录，登录查看详情':'填写申请后开始流转'}</small></div><div><span>当前阶段</span><strong>${state==='completed'?'全部环节已完成':esc(data.has_case&&!data.user?'登录后查看当前环节':current?.name||'申请准备')}</strong><small>${state==='private'?'业务记录需登录查看':data.config.stages.filter(s=>stageRuntime(s.key)?.status==='completed').length+' / '+data.config.stages.length+' 个环节完成'}</small></div><div class="oa-pending-person"><span>当前待处理人</span><strong>${state==='completed'?'已归档':state==='private'?'登录后查看':esc(names(pendingIds))}</strong><small>${current?.type==='approval'?'按指定审核顺序处理':current?.type==='handling'?'办理人员接手并提交结果':'准备申请资料并提交'}</small></div></section>
      ${!data.user&&!isDemo?`<div class="oa-notice">${svg('shield')}<span>你正在查看预设申请表和完整审批链。登录后按个人分工处理事项，填写内容与附件需要授权访问。</span></div>`:''}
      <div class="oa-layout"><div class="oa-primary">${panelTab==='history'?historyMarkup():panelTab==='config'?configMarkup():workMarkup()}</div><aside class="oa-flow-panel">${flowMarkup()}</aside></div>
      <div class="oa-footnote">${svg('shield')}流程配置仅由管理员维护；业务处理按指定人员授权。原计划、交付版本及处理记录保留可追溯。</div>
      </div></div><footer class="oa-actions">${footerMarkup()}</footer></section>`;
    if($a('.oa-scroll'))$a('.oa-scroll').scrollTop=scroll;
  }
  function sectionHeading(number,title,subtitle='',extra='') {return `<div class="oa-section-heading"><div><span class="oa-section-number">${number}</span><h2>${title}</h2></div>${extra}${subtitle?`<p>${subtitle}</p>`:''}</div>`;}
  function workMarkup() {
    const s=stageConfig(),rt=stageRuntime(selected)||{},can=canEdit();
    if(!s)return '<div class="oa-card oa-empty">未找到对应业务环节</div>';
    const appEditable=can&&s.type==='application';
    return `${(data.blockers||[]).length?`<div class="oa-dependency-alert">${svg('clock')}<div><b>等待前置事项完成</b><p>${esc(data.blockers.map(x=>x.title).join('、'))}</p><small>可以准备并保存资料；前置事项完成后再提交验收。</small></div></div>`:''}<${s.type==='application'?'section':'details'} class="oa-card oa-application-card">${s.type==='application'?'':'<summary class="oa-application-summary">'}${sectionHeading('01','申请信息','与当前产品绑定，随审批单保留申请快照。',`<span class="oa-template-tag">模板 v${esc(data.template?.version||1)}</span>`)}${s.type==='application'?'':'</summary>'}<div class="oa-form-body"><div class="oa-product-facts"><div><span>产品型号</span><strong>${esc(data.product.model||'待填写')}</strong></div><div><span>产品名称</span><strong>${esc(data.product.name||'待填写')}</strong></div><div><span>适用市场</span><strong>${esc(data.product.marketplace||data.task.marketplace||'待填写')}</strong></div></div><form id="oaApplicationForm" class="oa-form-grid">${data.config.fields.map(f=>fieldMarkup(f,applicationAnswers,appEditable,'application')).join('')}</form></div></${s.type==='application'?'section':'details'}>
      <section class="oa-card oa-stage-card">${sectionHeading('02',esc(s.name),`${esc(kindLabels[s.type]||s.type)}环节 · ${selected===currentKey()?'当前待推进的工作':'查看该环节的要求与记录'}`,`<span class="oa-stage-state ${esc(rt.status||'waiting')}">${esc(stateLabels[rt.status||'waiting']||rt.status)}</span>`)}
      <div class="oa-stage-responsibility"><div><span>${s.type==='approval'?'指定审核人':'负责人 / 办理人'}</span><strong>${esc(names(s.type==='approval'?s.reviewer_ids:s.assignee_ids))}</strong></div><div><span>协作人员</span><strong>${esc(s.collaborator_ids.length?names(s.collaborator_ids):'未指定')}</strong></div><div><span>收到 / 处理时间</span><strong>${datetime(rt.received_at)}</strong><small>${rt.completed_at?'处理于 '+datetime(rt.completed_at):rt.started_at?'接手于 '+datetime(rt.started_at):'尚未处理'}</small></div></div>
      ${!data.case?`<div class="oa-stage-note"><b>完整业务流程已预设</b><p>${!data.user?'登录后由指定人员发起申请。':!executor(data.config.stages[0])?'由管理员先分配申请人和各阶段的办理、审核人员，再由指定申请人提交。':'填写申请信息后提交，单据将自动交给下一环节指定人员。'}</p></div>`:''}
      ${data.case&&selected!==currentKey()&&data.case.status!=='completed'?'<div class="oa-stage-note"><p>你正在查看流程中的其他环节。当前待办位置在右侧突出显示，可随时切回。</p></div>':''}
      <div class="oa-time-grid"><div><span>原计划开始</span><strong>${dateOnly(rt.baseline_start||s.planned_start)}</strong></div><div><span>原计划完成</span><strong>${dateOnly(rt.baseline_end||s.planned_end)}</strong></div><div><span>最新预计完成</span><strong class="${rt.forecast_end&&rt.baseline_end&&rt.forecast_end>rt.baseline_end?'oa-delayed':''}">${dateOnly(progressDraft[selected]?.forecast_end||rt.forecast_end||s.planned_end)}</strong></div><div><span>实际开始 → 完成</span><strong>${rt.started_at?dateOnly(rt.started_at):'尚未开始'} → ${rt.completed_at?dateOnly(rt.completed_at):'尚未完成'}</strong></div></div>
      <div class="oa-form-body"><form id="oaStageForm" class="oa-form-grid">${s.fields.filter(f=>s.type!=='application'||!data.config.fields.some(a=>a.id===f.id)).map(f=>fieldMarkup(f,stageAnswers[selected]||{},can,'stage')).join('')}</form>${s.type==='approval'?`<div class="oa-review-explanation">${svg('people')}<span>${{all:'全部指定审核人通过后流转',any:'任一指定审核人通过后流转',sequential:'按指定人员顺序依次审核'}[s.review_mode]}${s.return_to?' · 退回至「'+esc(data.config.stages.find(x=>x.key===s.return_to)?.name||s.return_to)+'」':''}</span></div>`:''}
      ${can?`<form id="oaProgressForm" class="oa-progress-form"><h3>时间与办理说明</h3><div class="oa-form-grid"><label class="oa-field">最新预计完成<input type="date" data-progress="forecast_end" value="${esc(progressDraft[selected]?.forecast_end||'')}"></label><label class="oa-field">外部事件实际发生时间<input type="datetime-local" data-progress="external_event_at" value="${esc(progressDraft[selected]?.external_event_at||'')}"><small>登记回执、收到草稿或正本的实际时间；系统另记录入人和录入时间。</small></label><label class="oa-field oa-wide">延误原因<textarea rows="2" data-progress="delay_reason" placeholder="如延期，请说明原因">${esc(progressDraft[selected]?.delay_reason||'')}</textarea></label><label class="oa-field oa-wide">下一步安排<textarea rows="2" data-progress="next_action" placeholder="谁将在何时补充什么资料或处理事项">${esc(progressDraft[selected]?.next_action||'')}</textarea></label></div></form>`:progressReadOnly(rt)}
      ${can&&s.type==='approval'?`<label class="oa-field oa-review-opinion">审核意见<textarea id="oaOpinion" rows="3" maxlength="2000" placeholder="记录通过依据，或说明需要补充、修订的内容及反馈要求。退回时必填。">${esc(opinionDraft[selected]||'')}</textarea></label>`:''}
      ${decisionsMarkup(rt)}</div></section>${historyMini()}`;
  }
  function progressReadOnly(rt) {if(!rt.delay_reason&&!rt.next_action&&!rt.external_event_at)return '';return `<div class="oa-progress-readonly">${rt.external_event_at?`<p><span>外部事件时间</span>${datetime(rt.external_event_at)}</p>`:''}${rt.delay_reason?`<p><span>延误原因</span>${esc(rt.delay_reason)}</p>`:''}${rt.next_action?`<p><span>下一步安排</span>${esc(rt.next_action)}</p>`:''}</div>`;}
  function fieldMarkup(f,values,editable,scope) {
    const value=values[f.id]??'', id=`oa_${scope}_${f.id}`, attrs=`id="${esc(id)}" data-answer="${esc(f.id)}" data-scope="${scope}" data-type="${esc(f.type)}" ${editable?'':'disabled'} ${f.required?'required':''} aria-label="${esc(f.label)}"`;
    let input;
    if(f.type==='textarea')input=`<textarea rows="3" maxlength="10000" ${attrs} placeholder="${esc(f.help||'请填写')}">${esc(value)}</textarea>`;
    else if(['select','multiselect'].includes(f.type))input=`<select ${attrs} ${f.type==='multiselect'?'multiple':''}>${f.type==='select'?'<option value="">请选择</option>':''}${(f.options||[]).map(x=>`<option ${f.type==='multiselect'?(Array.isArray(value)&&value.includes(x)?'selected':''):(x===value?'selected':'')}>${esc(x)}</option>`).join('')}</select>`;
    else if(f.type==='person')input=`<select ${attrs}><option value="">待指定</option>${data.people.map(p=>`<option value="${esc(p.id)}" ${p.id===value?'selected':''}>${esc(p.display_name)}</option>`).join('')}</select>`;
    else if(f.type==='attachment')input=attachmentsMarkup(f,values,editable,scope);
    else input=`<input ${attrs} type="${f.type==='number'?'number':f.type==='date'?'date':'text'}" ${f.type==='number'?'step="any"':''} maxlength="10000" value="${esc(value)}" placeholder="${esc(f.help||'待填写')}">`;
    return `<div class="oa-field ${['textarea','attachment'].includes(f.type)?'oa-wide':''}"><label for="${esc(id)}">${esc(f.label)}${f.required?'<i>*</i>':'<small>选填</small>'}</label>${input}${f.help?`<small>${esc(f.help)}</small>`:''}</div>`;
  }
  function attachmentsMarkup(f,values,editable,scope) {
    const entries=Array.isArray(values[f.id])?values[f.id]:[];
    return `<div class="oa-attachments">${entries.map((file,i)=>`<div class="oa-file">${svg('file')}<div><a href="${file.kind==='file'?'/api/approvals/attachments/'+esc(file.id):esc(safeHref(file.url))}" target="_blank" rel="noopener noreferrer">${esc(file.title||'交付文件')}</a><small>${esc(file.version||'v1')} · ${file.kind==='file'?'系统附件':'外部资料链接'}</small></div>${editable?`<button type="button" class="text-btn" data-oa="remove-file" data-field="${esc(f.id)}" data-scope="${scope}" data-index="${i}">移除</button>`:''}</div>`).join('')||'<div class="oa-file-empty">尚未提交附件</div>'}${editable?`<div class="oa-upload-actions"><label class="btn">${svg('upload')}上传附件<input type="file" data-oa-upload="${esc(f.id)}" data-scope="${scope}" aria-label="上传${esc(f.label)}" hidden></label><small>单文件最多 10 MB</small></div><div class="oa-add-link"><input data-link-name="${esc(scope+':'+f.id)}" aria-label="资料名称" placeholder="资料名称"><input data-link-url="${esc(scope+':'+f.id)}" aria-label="资料链接" type="url" placeholder="https:// 飞书 / 网盘链接"><input data-link-version="${esc(scope+':'+f.id)}" aria-label="资料版本" placeholder="版本，如 v1"><button class="btn" type="button" data-oa="add-link" data-field="${esc(f.id)}" data-scope="${scope}">添加链接</button></div>`:''}</div>`;
  }
  function safeHref(raw) {try{const url=new URL(raw);return ['http:','https:'].includes(url.protocol)&&!url.username&&!url.password?url.href:'#';}catch{return '#';}}
  function decisionsMarkup(rt) {return (rt.decisions||[]).length?`<div class="oa-decisions"><h3>本环节审核记录</h3>${rt.decisions.map(d=>`<div class="oa-decision ${esc(d.decision)}"><span>${d.decision==='approved'?'✓':'↩'}</span><section><b>${esc(name(d.user_id||d.reviewer_id))} · ${d.decision==='approved'?'同意':'退回'}</b><p>${esc(d.comment||'已核对并通过')}</p><small>${datetime(d.at||d.created_at)}</small></section></div>`).join('')}</div>`:'';}
  function flowMarkup() {
    return `<div class="oa-flow-heading"><h2>审批流程</h2><span>${data.config.stages.length} 个环节</span></div><div class="oa-flow"><div class="oa-flow-start"><span></span><div><b>流程开始</b><small>${data.case?datetime(data.case.created_at):'提交申请后开始'}</small></div></div>${data.config.stages.map((s,i)=>{
      const rt=stageRuntime(s.key)||{},state=rt.status||'waiting',current=s.key===currentKey()&&data.case?.status!=='completed',ids=s.type==='approval'?s.reviewer_ids:s.assignee_ids;
      return `<button class="oa-flow-node ${state} ${current?'current':''} ${selected===s.key?'selected':''}" data-oa="stage" data-key="${esc(s.key)}" aria-current="${current?'step':'false'}"><span class="oa-flow-dot">${state==='completed'?'✓':String(i+1).padStart(2,'0')}</span><div><div class="oa-flow-node-title"><b>${esc(s.name)}</b><span>${esc(kindLabels[s.type])}</span></div><p>${svg('people')}${esc(names(ids))}</p><small>${state==='completed'?'完成于 '+datetime(rt.completed_at):current?(data.case?'当前待处理':'尚未发起'):'计划 '+dateOnly(rt.baseline_end||s.planned_end)}</small>${rt.received_at&&state!=='completed'?`<small>收到于 ${datetime(rt.received_at)}</small>`:''}${rt.decisions?.length?`<small>${rt.decisions.filter(d=>d.decision==='approved').length} 人同意${rt.decisions.some(d=>d.decision==='rejected')?' · 有退回意见':''}</small>`:''}</div></button>`;
    }).join('')}<div class="oa-flow-end ${data.case?.status==='completed'?'complete':''}"><span>${data.case?.status==='completed'?'✓':''}</span><div><b>完成归档</b><small>${data.case?.status==='completed'?'所有必需环节已通过':'最终验收后完成整个事项'}</small></div></div></div><div class="oa-flow-explanation">${svg('shield')}审核人按环节独立指定。办理和审批分别记录，不会因其他环节完成而自动通过验收。</div>`;
  }
  function historyMini() {return `<section class="oa-card">${sectionHeading('03','流转与交付记录','提交、接手、补件、审核和时间变更均保留。',button('查看全部','history'))}<div class="oa-history-mini">${data.history.slice(0,5).map(eventMarkup).join('')||(data.has_case&&!data.user?'<p class="oa-empty-note">登录后查看已有审批单的处理人与流转记录。</p>':'<p class="oa-empty-note">尚未发生流转。发起后，这里将记录每次处理人与时间。</p>')}</div></section>`;}
  function eventMarkup(event) {
    const payload=event.payload||event.after_data||{},stage=data.config.stages.find(s=>s.key===event.stage_key);
    return `<article class="oa-event"><i></i><div><div><b>${esc(event.actor_name||event.display_name||'系统')} · ${esc(eventLabels[event.action]||event.action)}</b><time>${datetime(event.created_at||event.at)}</time></div>${stage?`<small>${esc(stage.name)}</small>`:''}${payload.comment||payload.reason?`<p>${esc(payload.comment||payload.reason)}</p>`:''}${payload.external_event_at?`<p>外部事件实际时间：${datetime(payload.external_event_at)}；本条记录由上述人员录入。</p>`:''}<details><summary>查看本次记录与快照</summary><div class="oa-event-snapshot">${snapshotMarkup(payload)}</div></details></div></article>`;
  }
  function snapshotMarkup(payload) {
    const blocks=[];
    const renderValues=(values,fields=[])=>Object.entries(values||{}).map(([key,value])=>`<div><b>${esc(fields.find(f=>f.id===key)?.label||key)}</b><span>${renderValue(value)}</span></div>`).join('');
    if(payload.application_answers)blocks.push('<h4>申请信息</h4>'+renderValues(payload.application_answers,data.config.fields));
    if(payload.answers)blocks.push('<h4>环节填写内容</h4>'+renderValues(payload.answers,data.config.stages.find(s=>s.key===payload.stage_key)?.fields));
    for(const key of ['forecast_end','delay_reason','next_action','return_to','baseline_start','baseline_end','config_version'])if(payload[key])blocks.push(`<div><b>${esc({forecast_end:'预计完成',delay_reason:'延误原因',next_action:'下一步安排',return_to:'退回环节',baseline_start:'原计划开始',baseline_end:'原计划完成',config_version:'配置版本'}[key])}</b><span>${esc(payload[key])}</span></div>`);
    if(payload.stage_snapshot){const snapshot=payload.stage_snapshot;blocks.push('<h4>环节快照 · '+esc(snapshot.name||'')+'</h4>'+renderValues(snapshot.answers,snapshot.fields)+['baseline_start','baseline_end','forecast_end','started_at','completed_at','external_event_at','delay_reason','next_action'].filter(key=>snapshot[key]).map(key=>`<div><b>${esc({baseline_start:'原计划开始',baseline_end:'原计划完成',forecast_end:'预计完成',started_at:'实际开始',completed_at:'实际完成',external_event_at:'外部事件实际时间',delay_reason:'延误原因',next_action:'下一步安排'}[key])}</b><span>${esc(snapshot[key])}</span></div>`).join(''));}
    if(payload.previous_case){const previous=payload.previous_case;blocks.push(`<details><summary>变更前的单据快照 · ${esc(previous.number||'原版本')}</summary><div>${renderValues(previous.application_answers,previous.definition?.fields)}${(previous.stages||[]).map(stage=>`<h4>${esc(stage.name)} · ${esc(stateLabels[stage.status]||stage.status)}</h4>${renderValues(stage.answers,stage.fields)}`).join('')}</div></details>`);}
    if(payload.config)blocks.push(`<h4>配置版本 ${esc(payload.config.version||'')}</h4><p>申请项目：${esc((payload.config.fields||[]).map(f=>f.label).join('、'))}</p>${(payload.config.stages||[]).map(stage=>`<div><b>${esc(stage.name)}</b><span>${esc(kindLabels[stage.type])} · ${esc(names(stage.type==='approval'?stage.reviewer_ids:stage.assignee_ids))}<br>计划 ${dateOnly(stage.planned_start)} → ${dateOnly(stage.planned_end)}<br>${esc((stage.fields||[]).map(f=>f.label).join('、'))}</span></div>`).join('')}`);
    if(!blocks.length)blocks.push(`<p>${esc(payload.filename||payload.comment||payload.reason||'此操作已记录处理人和系统时间。')}</p>`);
    return blocks.join('');
  }
  function renderValue(value) {if(Array.isArray(value))return value.map(item=>typeof item==='object'?`<a href="${item.kind==='file'?'/api/approvals/attachments/'+esc(item.id):esc(safeHref(item.url))}" target="_blank" rel="noopener noreferrer">${esc(item.title||'资料')} · ${esc(item.version||'v1')}</a>`:esc(item)).join('<br>');return esc(value===null||value===''?'未填写':typeof value==='object'?JSON.stringify(value):value);}
  function historyMarkup() {return `<section class="oa-card">${sectionHeading('','审批记录与历史版本','原计划、每轮提交、退回原因和附件版本均可追溯。',button('返回当前事项','work'))}<div class="oa-history-full">${data.history.map(eventMarkup).join('')||'<p class="oa-empty-note">尚未发生审批流转。</p>'}</div></section>${legacyMarkup()}`;}
  function legacyMarkup() {
    const legacy=data.legacy||{};
    const sections=[];
    if(legacy.submissions?.length)sections.push(`<h3>旧表单提交</h3>${legacy.submissions.map((record,index)=>`<details class="oa-legacy-item"><summary><span>提交版本 ${legacy.submissions.length-index} · ${esc(name(record.submitted_by))}</span><small>${datetime(record.created_at)}</small></summary><div>${(record.snapshot?.fields||[]).map(f=>`<section><b>${esc(f.label)}</b><p>${renderValue(record.answers?.[f.id])}</p></section>`).join('')}${(record.decisions||[]).map(d=>`<section class="oa-legacy-opinion"><b>${esc(name(d.reviewer_id||d.user_id))} · ${d.decision==='approved'?'通过':'退回'}</b><p>${esc(d.comment||'已处理')}</p><small>${datetime(d.created_at||d.at)}</small></section>`).join('')}</div></details>`).join('')}`);
    if(legacy.approvals?.length)sections.push(`<h3>旧审批记录</h3>${legacy.approvals.map(record=>`<div class="oa-legacy-item"><header><b>${esc(name(record.approver_id))} · ${esc({approved:'已通过',rejected:'已退回',pending:'待处理'}[record.decision]||'审批记录')}</b><small>${datetime(record.decided_at)}</small></header><p>${esc(record.comment||'未填写意见')}</p></div>`).join('')}`);
    if(legacy.evidence?.length)sections.push(`<h3>旧交付证据</h3>${legacy.evidence.map(record=>`<div class="oa-legacy-item"><header><b>${esc(record.note||'交付资料')}</b><small>${datetime(record.created_at)}</small></header><p>${safeHref(record.uri)!=='#'?`<a href="${esc(safeHref(record.uri))}" target="_blank" rel="noopener noreferrer">打开原资料链接</a>`:esc(record.uri||'未记录内容')}</p><small>登记人：${esc(name(record.uploaded_by))}</small></div>`).join('')}`);
    if(legacy.documents?.length)sections.push(`<h3>旧文件索引</h3>${legacy.documents.map(record=>`<div class="oa-legacy-item"><header><b>${esc(record.title||'交付文件')} · ${esc(record.version||'未登记版本')}</b><small>${datetime(record.created_at)}</small></header>${safeHref(record.url)!=='#'?`<a href="${esc(safeHref(record.url))}" target="_blank" rel="noopener noreferrer">打开文件</a>`:''}<p>${esc(record.note||'')}</p></div>`).join('')}`);
    if(!sections.length)return '';
    return `<section class="oa-card"><div class="oa-section-heading"><div><h2>旧版本记录</h2></div><p>升级前的数据保留，不作为新审批链的自动通过依据。</p></div><div class="oa-legacy-records">${sections.join('')}</div></section>`;
  }
  function beginConfig() {configDraft=structuredClone(data.config);configDraft.dependencies=[...(data.config.dependencies||data.dependencies||[])];for(const s of configDraft.stages){s.fields ||= [];s.assignee_ids ||= [];s.reviewer_ids ||= [];s.collaborator_ids ||= [];s.review_mode ||= 'all';}configTab='__application';panelTab='config';}
  function memberSelect(id) {return `<option value="">待分配</option>${data.people.map(p=>`<option value="${esc(p.id)}" ${p.id===id?'selected':''}>${esc(p.display_name)}${p.department?' · '+esc(p.department):''}</option>`).join('')}`;}
  function peopleChecks(key,ids,stage) {return `<div class="oa-people-checks">${data.people.map(p=>`<label><input type="checkbox" data-config-person="${key}" data-config-stage="${esc(stage)}" value="${esc(p.id)}" ${ids.includes(p.id)?'checked':''}><span>${esc(p.display_name)}<small>${esc(p.department||'协作成员')}</small></span></label>`).join('')||'<p class="oa-empty-note">暂无登录成员，请先通过“成员与账户”开通人员。</p>'}</div>`;}
  function configMarkup() {
    if(!data.permissions.configure)return '<section class="oa-card oa-empty">只有流程管理员能维护审批配置。</section>';
    if(!configDraft)beginConfig();
    const s=configDraft.stages.find(x=>x.key===configTab),target=s||configDraft,fields=target.fields;
    return `<section class="oa-card"><div class="oa-section-heading"><div><span class="oa-section-number">⚙</span><h2>配置审批流程</h2></div><p>仅应用于当前产品的这项事项。修改后保存为新版本，已运行审批单保留快照。</p></div><div class="oa-config-body"><div class="oa-config-tabs"><button data-oa="config-tab" data-key="__application" class="${configTab==='__application'?'active':''}">申请表单</button>${configDraft.stages.map((x,i)=>`<button data-oa="config-tab" data-key="${esc(x.key)}" class="${configTab===x.key?'active':''}">${i+1}. ${esc(x.name)}</button>`).join('')}</div>
      <form id="oaConfigForm">${s?`<div class="oa-config-stage-heading"><h3>环节设置</h3><div>${button('上移环节','stage-up',false,configDraft.stages.indexOf(s)===0?'disabled':'')}${button('下移环节','stage-down',false,configDraft.stages.indexOf(s)===configDraft.stages.length-1?'disabled':'')}${button('删除环节','stage-remove')}</div></div><div class="oa-form-grid"><label class="oa-field">环节名称<input data-config-stage-field="name" required value="${esc(s.name)}" maxlength="100"></label><label class="oa-field">环节类型<select data-config-stage-field="type">${Object.entries(kindLabels).map(([key,label])=>`<option value="${key}" ${s.type===key?'selected':''}>${label}</option>`).join('')}</select></label><label class="oa-field">计划开始<input type="date" data-config-stage-field="planned_start" value="${esc(s.planned_start||'')}"></label><label class="oa-field">计划完成<input type="date" data-config-stage-field="planned_end" value="${esc(s.planned_end||'')}"></label></div><h4>${s.type==='approval'?'指定审核人':'负责人 / 办理人'}</h4>${peopleChecks(s.type==='approval'?'reviewer_ids':'assignee_ids',s.type==='approval'?s.reviewer_ids:s.assignee_ids,s.key)}<h4>协作人员</h4>${peopleChecks('collaborator_ids',s.collaborator_ids,s.key)}${s.type==='approval'?`<div class="oa-form-grid"><label class="oa-field">审核方式<select data-config-stage-field="review_mode">${[['all','全部指定人员通过'],['any','任一指定人员通过'],['sequential','按指定顺序依次通过']].map(([key,label])=>`<option value="${key}" ${s.review_mode===key?'selected':''}>${label}</option>`).join('')}</select></label><label class="oa-field">退回补充环节<select data-config-stage-field="return_to"><option value="">请选择前面的补充或修订环节</option>${configDraft.stages.slice(0,configDraft.stages.indexOf(s)).filter(x=>x.type!=='end'&&x.type!=='cc').map(x=>`<option value="${esc(x.key)}" ${s.return_to===x.key?'selected':''}>${esc(x.name)}</option>`).join('')}</select></label></div>${s.review_mode==='sequential'?`<div class="oa-config-review-order">${s.reviewer_ids.map((id,i)=>`<div><span>${i+1}. ${esc(name(id))}</span>${i?button('上移','reviewer-up',false,`data-index="${i}"`):''}</div>`).join('')}</div>`:''}`:''}`:'<div class="oa-config-intro">维护发起申请时需要收集的产品与认证信息。阶段专用的草稿、正本和处理资料在对应环节配置。</div>'}
      <div class="oa-config-stage-heading"><h3>${s?'本环节填写项目':'申请填写项目'}</h3><span>${fields.length} 项</span></div><div class="oa-config-fields">${fields.map((f,i)=>`<div class="oa-config-field"><div class="oa-config-field-top"><span>${String(i+1).padStart(2,'0')}</span><b>${esc(types[f.type])}</b><button type="button" class="text-btn" data-oa="field-remove" data-index="${i}">移除</button></div><div class="oa-form-grid"><label class="oa-field">项目名称<input data-config-field-index="${i}" data-config-field-key="label" value="${esc(f.label)}" required maxlength="100"></label><label class="oa-field">填写类型<select data-config-field-index="${i}" data-config-field-key="type">${Object.entries(types).map(([key,label])=>`<option value="${key}" ${f.type===key?'selected':''}>${label}</option>`).join('')}</select></label><label class="oa-field oa-wide">填写提示<input data-config-field-index="${i}" data-config-field-key="help" value="${esc(f.help||'')}" maxlength="500"></label>${['select','multiselect'].includes(f.type)?`<label class="oa-field oa-wide">选项（每行一个）<textarea data-config-field-index="${i}" data-config-field-key="options" required>${esc((f.options||[]).join('\n'))}</textarea></label>`:''}</div><label class="oa-check"><input type="checkbox" data-config-field-index="${i}" data-config-field-key="required" ${f.required?'checked':''}>完成此环节前必须填写</label></div>`).join('')}</div>${button(svg('plus')+'添加填写项目','field-add')}
      <section class="oa-config-dependencies"><h3>此事项的前置任务</h3><p>仅选择同一产品的事项；全部前置完成后才能提交验收。系统会检查循环关系。</p><div>${(typeof ws!=='undefined'?ws.tasks:[]).filter(t=>t.product_id===data.task.product_id&&t.id!==data.task.id).map(t=>`<label><input type="checkbox" data-config-dependency="${esc(t.id)}" ${configDraft.dependencies.includes(t.id)?'checked':''}><span>${esc(t.title)}<small>${['completed','not_applicable'].includes(t.status)?'已完成':'待完成'}</small></span></label>`).join('')||'<span class="oa-empty-note">当前产品暂无其他可选事项</span>'}</div></section>
      <label class="oa-field oa-config-reason">配置变更原因<textarea id="oaConfigReason" placeholder="调整已设置的计划时间或撤回流程时，请说明原因">${esc(configDraft.reason||'')}</textarea></label>
      <div class="oa-config-add-stage">${button(svg('plus')+'添加流程环节','stage-add')}</div>${data.case&&data.case.status!=='draft'?`<div class="oa-config-recall"><label class="oa-check"><input type="checkbox" id="oaRecallPending" ${configDraft.recall_pending?'checked':''} required>确认撤回当前流程的处理进度与审核，按新配置重新提交；原版本记录保留。</label></div>`:''}</form></div></section>`;
  }
  function footerMarkup() {
    if(busy)return '<span class="oa-saving">正在保存，请稍候…</span>';
    if(panelTab==='config'&&(data.case?.status==='completed'||!data.case&&['completed','not_applicable'].includes(data.task.status)))return `<div class="oa-action-hint">已完成事项需先说明原因重新打开，再调整配置。</div>${button('返回当前事项','work')}${button('重新打开','reopen')}`;
    if(panelTab==='config')return `<div class="oa-action-hint">${dirty?'有未保存的配置':'仅流程管理员可调整步骤、字段和审核人'}</div>${button('取消配置','work')}${button('保存配置版本','config-save',true)}`;
    if(panelTab==='history')return `<div class="oa-action-hint">历史记录保留原提交与审核快照</div>${button('返回当前事项','work')}`;
    const s=stageConfig(),rt=stageRuntime(selected)||{};
    if(!data.user||isDemo)return `<div class="oa-action-hint">${isDemo?'示例展示完整审批结构；实际提交需连接后端。':'登录后按分工参与申请、办理或审核。'}</div>${button('返回 SOP','close')}${!isDemo?button('登录处理审批','login',true):''}`;
    if(data.case?.status==='completed'||!data.case&&['completed','not_applicable'].includes(data.task.status))return `<div class="oa-action-hint">审批单已完成，验收时的内容和文件版本保留。</div>${data.permissions.configure?button('说明原因并重新打开','reopen'):''}${button('返回 SOP','close')}`;
    if(!isCurrent())return `<div class="oa-action-hint">此处为查看环节，当前处理位置为「${esc(data.config.stages.find(x=>x.key===currentKey())?.name||'申请准备')}」。</div>${button('回到当前环节','current',true)}`;
    if(s?.type==='approval'&&reviewer(s))return `<div class="oa-action-hint">${dirty?'有尚未提交的填写':'核对本环节内容与附件后处理'}</div>${button('退回补充 / 修订','reject')}${button('同意并流转','approve',true)}`;
    if(executor(s))return `<div class="oa-action-hint">${dirty?'有未保存的填写':'按当前环节要求整理资料并提交'}</div>${button('保存草稿','save')}${s.type==='application'?button('提交申请','submit',true,(data.blockers||[]).length?'disabled title="前置事项尚未完成"':''):s.type==='handling'&&data.case&&rt.status==='pending'?button('确认接单 / 接手','accept',true):s.type==='handling'?button('提交办理结果','complete',true):''}`;
    return `<div class="oa-action-hint">${s?.type==='approval'?'等待指定审核人员处理。':'等待指定申请人或办理人员处理。'}人员：${esc(names(s?.type==='approval'?s.reviewer_ids:s?.assignee_ids))}</div>${data.permissions.configure?button('分配人员与计划','configure',true):button('返回 SOP','close')}`;
  }
  function markDirty() {dirty=true;$a('.oa-action-hint')?.classList.add('unsaved');}
  function showError(message) {const area=$a('.oa-primary');if(area){area.querySelector('.oa-inline-error')?.remove();const element=document.createElement('div');element.className='oa-inline-error';element.setAttribute('role','alert');element.textContent=message;area.prepend(element);element.scrollIntoView({block:'nearest'});}notify(message);}
  async function sendAction(action) {
    if(busy||!data)return;
    let comment=$a('#oaOpinion')?.value||opinionDraft[selected]||'';
    if(action==='reject'&&!comment.trim())return showError('退回时请填写需要补充或修订的意见。');
    if(['submit','complete','approve'].includes(action)){
      if(!($a('#oaStageForm')?.reportValidity()??true))return;
      if(stageConfig()?.type==='application'&&!($a('#oaApplicationForm')?.reportValidity()??true))return;
    }
    if(action==='reopen'){comment=prompt('说明重新打开的原因。已完成记录保留，后续环节需要重新验收：')||'';if(!comment.trim())return;}
    const p=progressDraft[selected]||{};
    const selectedConfig=stageConfig();const actionAnswers=structuredClone(stageAnswers[selected]||{});
    if(selectedConfig?.type==='application')for(const f of selectedConfig.fields)if(Object.hasOwn(applicationAnswers,f.id))actionAnswers[f.id]=applicationAnswers[f.id];
    const body={expected_version:data.case?.version||0,stage_key:selected,action,answers:actionAnswers,application_answers:applicationAnswers,forecast_end:p.forecast_end||null,delay_reason:p.delay_reason||'',next_action:p.next_action||'',external_event_at:p.external_event_at?new Date(p.external_event_at+':00+08:00').toISOString():null,comment};
    busy=true;draw();
    try{await api(endpoint('/actions'),{method:'POST',body:JSON.stringify(body)});dirty=false;busy=false;await refresh();selected=currentKey();draw(false);notify({save:'草稿已保存',submit:'申请已提交并流转',accept:'已记录接手时间',complete:'办理结果已提交',approve:'审核意见已记录',reject:'已退回指定补充环节',reopen:'已重新打开审批单'}[action]);}
    catch(error){busy=false;draw();showError(error.message);}
  }
  async function saveConfig() {
    if(busy||!$a('#oaConfigForm')?.reportValidity())return;
    const body={expected_version:data.config.version||0,fields:configDraft.fields,stages:configDraft.stages.map(stage=>Object.fromEntries(['key','name','type','fields','assignee_ids','reviewer_ids','collaborator_ids','review_mode','planned_start','planned_end','return_to'].filter(key=>stage[key]!==undefined).map(key=>[key,stage[key]]))),recall_pending:!!$a('#oaRecallPending')?.checked,reason:$a('#oaConfigReason')?.value||'',dependencies:configDraft.dependencies};
    busy=true;draw();
    try{await api(endpoint('/config'),{method:'PUT',body:JSON.stringify(body)});busy=false;dirty=false;configDraft=null;panelTab='work';await refresh();notify('审批配置已保存，新版本已生效。');}
    catch(error){busy=false;draw();showError(error.message);}
  }
  async function uploadFile(element) {
    const file=element.files?.[0];if(!file||busy)return;
    if(file.size>10*1024*1024)return showError('附件不能超过 10 MB');
    const body=new FormData();body.append('file',file);body.append('stage_key',element.dataset.scope==='application'?data.config.stages[0].key:selected);
    busy=true;draw();
    try{const item=await api(endpoint('/attachments'),{method:'POST',body});const values=element.dataset.scope==='application'?applicationAnswers:stageAnswers[selected];values[element.dataset.oaUpload]=[...(values[element.dataset.oaUpload]||[]),item];dirty=true;data=await api(endpoint());normalize();busy=false;draw();notify('附件已上传，请保存草稿或提交当前环节。');}
    catch(error){busy=false;draw();showError(error.message);}
  }
  async function handle(element) {
    if(busy)return;const action=element.dataset.oa;
    if(action==='close')return close();
    if(action==='stage'){selected=element.dataset.key;panelTab='work';draw(false);}
    else if(action==='current'){selected=currentKey();panelTab='work';draw(false);}
    else if(action==='history'){if(panelTab==='config'&&dirty&&!confirm('放弃尚未保存的配置并查看历史？'))return;panelTab='history';draw(false);}
    else if(action==='work'){if(panelTab==='config'&&dirty&&!confirm('放弃尚未保存的配置？'))return;configDraft=null;dirty=false;panelTab='work';draw(false);}
    else if(action==='configure'){if(dirty&&!confirm('进入配置将放弃尚未保存的填写，确认继续？'))return;dirty=false;beginConfig();draw(false);}
    else if(action==='login'){
      if(window.processUI?.login){await window.processUI.login();const dialog=$a('#pwAuthDialog');dialog?.addEventListener('close',()=>refresh().catch(error=>showError(error.message)),{once:true});}
      else showError('请使用页面顶部的个人账户入口登录后重新打开本事项。');
    }
    else if(['save','submit','accept','complete','approve','reject','reopen'].includes(action))await sendAction(action);
    else if(action==='config-save')await saveConfig();
    else if(action==='config-tab'){configTab=element.dataset.key;draw(false);}
    else if(action==='field-add'){
      const target=configDraft.stages.find(s=>s.key===configTab)||configDraft;target.fields.push({id:'custom_'+crypto.randomUUID().replaceAll('-',''),label:'',type:'text',required:false,help:'',options:[]});markDirty();draw();all('[data-config-field-key="label"]').at(-1)?.focus();
    }
    else if(action==='field-remove'){const target=configDraft.stages.find(s=>s.key===configTab)||configDraft;target.fields.splice(Number(element.dataset.index),1);markDirty();draw();}
    else if(action==='stage-add'){const key='step_'+crypto.randomUUID().slice(0,8);configDraft.stages.push({key,name:'新增办理环节',type:'handling',fields:[],assignee_ids:[],reviewer_ids:[],collaborator_ids:[],review_mode:'all',planned_start:null,planned_end:null,return_to:null});configTab=key;markDirty();draw(false);}
    else if(action==='stage-remove'){if(configDraft.stages.length<=1)return showError('至少保留一个申请环节。');const index=configDraft.stages.findIndex(s=>s.key===configTab);configDraft.stages.splice(index,1);configTab='__application';markDirty();draw(false);}
    else if(action==='stage-up'||action==='stage-down'){const i=configDraft.stages.findIndex(s=>s.key===configTab),j=i+(action==='stage-up'?-1:1);if(j<0||j>=configDraft.stages.length)return;[configDraft.stages[i],configDraft.stages[j]]=[configDraft.stages[j],configDraft.stages[i]];markDirty();draw();}
    else if(action==='reviewer-up'){const s=configDraft.stages.find(x=>x.key===configTab),i=Number(element.dataset.index);[s.reviewer_ids[i-1],s.reviewer_ids[i]]=[s.reviewer_ids[i],s.reviewer_ids[i-1]];markDirty();draw();}
    else if(action==='remove-file'){const values=element.dataset.scope==='application'?applicationAnswers:stageAnswers[selected];values[element.dataset.field].splice(Number(element.dataset.index),1);markDirty();draw();}
    else if(action==='add-link'){
      const key=element.dataset.scope+':'+element.dataset.field,nameInput=$a(`[data-link-name="${CSS.escape(key)}"]`),urlInput=$a(`[data-link-url="${CSS.escape(key)}"]`),versionInput=$a(`[data-link-version="${CSS.escape(key)}"]`);
      const title=nameInput.value.trim(),url=urlInput.value.trim(),version=versionInput.value.trim()||'v1';
      if(!title||safeHref(url)==='#'||/\s/.test(url))return showError('请填写资料名称和有效的 HTTP(S) 文件链接。');
      const values=element.dataset.scope==='application'?applicationAnswers:stageAnswers[selected];values[element.dataset.field]=[...(values[element.dataset.field]||[]),{kind:'link',title,url,version}];markDirty();draw();
    }
  }
  document.addEventListener('click',event=>{const element=event.target.closest('[data-oa]');if(!element)return;event.preventDefault();handle(element).catch(error=>showError(error.message));});
  document.addEventListener('input',event=>{
    const element=event.target;
    if(element.dataset.answer){const values=element.dataset.scope==='application'?applicationAnswers:stageAnswers[selected];let value=element.value;if(element.dataset.type==='number')value=value===''?'':Number(value);if(element.dataset.type==='multiselect')value=Array.from(element.selectedOptions).map(o=>o.value);values[element.dataset.answer]=value;markDirty();}
    else if(element.id==='oaOpinion'){opinionDraft[selected]=element.value;markDirty();}
    else if(element.dataset.progress){progressDraft[selected][element.dataset.progress]=element.value;markDirty();}
    else if(element.dataset.configStageField&&configDraft){configDraft.stages.find(s=>s.key===configTab)[element.dataset.configStageField]=element.value||null;markDirty();}
    else if(element.dataset.configFieldIndex!==undefined&&configDraft){const target=configDraft.stages.find(s=>s.key===configTab)||configDraft,f=target.fields[Number(element.dataset.configFieldIndex)],key=element.dataset.configFieldKey;f[key]=key==='required'?element.checked:key==='options'?element.value.split('\n').map(v=>v.trim()).filter(Boolean):element.value;markDirty();}
    else if(element.id==='oaConfigReason'){configDraft.reason=element.value;markDirty();}
    else if(element.id==='oaRecallPending'){configDraft.recall_pending=element.checked;markDirty();}
  });
  document.addEventListener('change',event=>{
    const element=event.target;
    if(element.dataset.oaUpload)uploadFile(element);
    else if(element.dataset.configDependency&&configDraft){const id=element.dataset.configDependency;if(element.checked&&!configDraft.dependencies.includes(id))configDraft.dependencies.push(id);else if(!element.checked)configDraft.dependencies=configDraft.dependencies.filter(value=>value!==id);markDirty();}
    else if(element.dataset.configPerson&&configDraft){const s=configDraft.stages.find(x=>x.key===element.dataset.configStage),key=element.dataset.configPerson;if(element.checked&&!s[key].includes(element.value))s[key].push(element.value);else if(!element.checked)s[key]=s[key].filter(id=>id!==element.value);markDirty();if(s.review_mode==='sequential')draw();}
    else if(element.dataset.configStageField==='type'||element.dataset.configStageField==='review_mode'||element.dataset.configFieldKey==='type')draw();
  });
  document.addEventListener('submit',event=>{if(['oaApplicationForm','oaStageForm','oaProgressForm','oaConfigForm'].includes(event.target.id))event.preventDefault();});
  document.addEventListener('keydown',event=>{
    if(document.querySelector('dialog[open]'))return;
    const page=$a('.oa-page');if(!page)return;
    if(event.key==='Escape'){event.preventDefault();close();}
    if(event.key==='Tab'&&!document.querySelector('dialog[open]')){const focusable=Array.from(page.querySelectorAll('button:not(:disabled),input:not(:disabled):not([hidden]),select:not(:disabled),textarea:not(:disabled),a[href],summary')).filter(x=>x.getClientRects().length),first=focusable[0],last=focusable.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}
  });
  window.addEventListener('beforeunload',event=>{if(data&&dirty){event.preventDefault();event.returnValue='';}});
  return {open,close,refresh};
})();
