/* Process collaboration: the diagram and SOP list share the same task IDs. */
window.processUI = (() => {
  let auth = {user:null, initialized:false}, authReady=false, authBusy=false, authAttempted=false;
  let data=null, loadedProduct=null, loadingProduct=null, generation=0, loadError='', errorProduct=null;
  let display='graph', scope='all', zoom=1, detail=null, tab='form', answers={}, configDraft=null, dirty=false, busy=false, returnFocus=null;
  const types={text:'单行文字',textarea:'多行说明',number:'数字',date:'日期',select:'单选',multiselect:'多选',person:'人员',location:'地点',attachment:'附件 / 文件链接'};
  const actionNames={configured:'更新填写项目与审核配置',draft_saved:'保存填写草稿',submitted:'提交验收',approved:'审核通过',rejected:'退回修改',reopened:'重新打开，要求重新验收',attachment_uploaded:'上传附件'};
  const stageNames=['准备与定义','内容与交付','上线与投放','跟踪与复盘'];
  const stageOf=n=>n.phase==='上线后'?3:n.sop_key==='supply-lead'||n.title==='确认生产、头程及入仓周期'?0:({shop:0,compliance:0,finance:0,listing:1,inventory:1,calendar:2,ads:2,service:3,review:3}[n.module_code]??3);
  const name=id=>data?.people.find(p=>p.id===id)?.display_name || ws?.people.find(p=>p.id===id)?.display_name || '待分配';
  const memberOptions=id=>`<option value="">请选择人员</option>${(data?.people||[]).map(p=>`<option value="${e(p.id)}" ${id===p.id?'selected':''}>${e(p.display_name)} · ${e(p.department)}</option>`).join('')}`;
  const dt=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'尚未发生';
  const btn=(label,action,primary=false,attrs='')=>`<button type="button" class="btn ${primary?'primary':''}" data-pw="${action}" ${attrs}>${label}</button>`;
  const badge=n=>`<span class="pw-state ${e(n.status)}">${e(statuses[n.status]||n.status)}</span>`;
  function portal(){let root=$('#pwPortal');if(!root){root=document.createElement('div');root.id='pwPortal';document.body.append(root)}return root;}
  function invalidate(){data=null;loadedProduct=null;loadingProduct=null;loadError='';generation++;}
  async function session(){
    if(authBusy||mode!=='server')return;
    authBusy=true;authAttempted=true;
    try{auth=await api('/api/workflow-auth/session');authReady=true;}
    catch(err){authReady=false;toast('流程登录服务暂不可用：'+err.message)}
    finally{authBusy=false;identity();window.dispatchEvent(new Event('homa-session'));if(view==='process'&&loadedProduct||view==='workflow')render();}
  }
  function identity(){
    const el=$('#workflowIdentity');if(!el)return;
    el.innerHTML=mode==='server'?(auth.user?`<button class="pw-identity" data-pw="account"><span class="pw-avatar">${e(auth.user.display_name.slice(0,1))}</span><span>${e(auth.user.display_name)}<small>${auth.user.is_admin?'流程管理员':'协作成员'}</small></span></button>`:btn('登录协作账户','login')):'';
  }
  function decorate(){
    if(mode==='server'&&!authAttempted&&!authBusy)session();
    identity();
    if(mode==='server'&&!auth.user?.is_admin){
      $$('#pageActions [data-action="new-task"], #pageActions [data-action="new-person"], #pageActions [data-action="new-product"]').forEach(el=>el.remove());
    }
    if(view==='process')requestAnimationFrame(drawEdges);
  }
  const $$=s=>Array.from(document.querySelectorAll(s));
  async function loadGraph(force=false){
    if(mode!=='server')return;
    const selected=productId==='all'?null:productId;
    if(!selected)return;
    if(!force&&loadingProduct===selected)return;
    const run=++generation;loadingProduct=selected;loadError='';
    try{const result=await api(`/api/process/products/${selected}`);if(run!==generation)return;data=result;loadedProduct=selected;}
    catch(err){if(run!==generation)return;loadError=err.message;errorProduct=selected;data=null;}
    finally{if(run===generation){loadingProduct=null;if(view==='process')render();}}
  }
  function visibleNodes(){
    return (data?.nodes||[]).filter(n=>(!query||(n.title+' '+name(n.owner_id)+' '+n.config.location).toLowerCase().includes(query.toLowerCase()))&&
      (scope==='all'||scope==='mine'&&(n.owner_id===auth.user?.id||n.config.collaborator_ids.includes(auth.user?.id))||scope==='review'&&n.status==='pending_approval'&&n.config.reviewer_ids.includes(auth.user?.id)||scope==='overdue'&&overdue(n)));
  }
  function renderPage(){
    const selected=productId==='all'?null:productId;
    if(!selected&&ws.products.length)return empty('请选择具体产品型号','选择上方工作范围后，可查看和处理此产品的流程节点。');
    if(!selected)return empty('先建立产品档案','建立产品后，可在这里安排流程、填写项目与审核人员。',auth.user?.is_admin?button('建立产品','new-product',true):'');
    if(errorProduct!==selected)loadError='';
    if(mode==='demo'){
      data={product:current()||{},people:ws.people,nodes:scopedTasks().map(n=>({...n,configured:false,state_version:0,config:{version:0,fields:[],reviewer_ids:[],collaborator_ids:[],location:'示例工作地点'},dependencies:ws.dependencies.filter(d=>d.task_id===n.id).map(d=>d.depends_on_task_id),blockers:blockers(n),permissions:{}}))};
    }else if(loadedProduct!==selected&&!loadError){loadGraph();return '<div class="loading-state">正在读取流程、人员与版本记录…</div>';}
    if(loadError)return `<div class="pw-error">${icon('flag')}<h3>流程暂未读取成功</h3><p>${e(loadError)}</p>${btn('重新读取','reload')}</div>`;
    if(!data?.nodes.length)return empty('还没有流程节点','由流程管理员先为产品创建 SOP 任务，再启用协作流程。');
    const nodes=visibleNodes(),all=data.nodes,complete=all.filter(n=>done(n)).length,ready=all.filter(n=>!done(n)&&!n.blockers.length&&n.status!=='pending_approval').length,pending=all.filter(n=>n.status==='pending_approval').length,pct=progress(all);
    return `<div class="pw-page">
      <section class="pw-project"><div><div class="pw-project-kicker"><span class="pw-live-dot"></span>${e(data.product.model)} · ${e(data.product.marketplace||current()?.marketplace||'US')} ${data.product.data_source==='sample'?'<span class="label">示例产品</span>':''}</div><h2>从准备，到每一次交付。</h2><p>时间、地点、责任人与资料，在同一个流程里协作。</p><div class="pw-project-meta"><span>${icon('calendar')}目标上线 ${e(data.product.target_launch_date||'待安排')}</span><span>${icon('people')}${data.people.length} 位可登录成员</span></div></div><div class="pw-progress-block"><div class="pw-ring" style="--progress:${pct}%"><strong>${pct}<small>%</small></strong></div><div><strong>${complete} / ${all.length} 个节点已完成</strong><p>按任务权重计算进度</p><span>${ready} 项可推进 · ${pending} 项待审核</span></div></div></section>
      <div class="pw-access-bar"><div>${icon(auth.user?.is_admin?'settings':'people')}<span>${auth.user?`<b>${e(auth.user.display_name)}</b> · ${auth.user.is_admin?'你可以配置填写项目、人员与审核规则':'你可以处理分配给自己的工作；流程配置由管理员维护'}`:'当前为只读浏览 · 登录后填写分配给你的事项或完成审核'}</span></div><div>${mode==='demo'?'<span class="label">本机示例 · 只读</span>':auth.user?.is_admin?btn('成员与账户','members'):!auth.user?btn(auth.initialized?'登录处理工作':'首次设置管理员','login',true):''}</div></div>
      <section class="pw-board"><div class="pw-board-toolbar"><div class="pw-view-tabs" role="group" aria-label="流程视图">${[['graph','流程图'],['list','节点清单'],['schedule','时间排期']].map(([id,label])=>`<button data-pw="view" data-value="${id}" class="${display===id?'active':''}">${label}</button>`).join('')}</div><div class="pw-board-tools"><span>${nodes.length} 个节点</span>${display==='graph'?`<label>缩放 <select id="pwZoom" aria-label="流程图缩放">${[.75,1,1.25].map(x=>`<option value="${x}" ${x===zoom?'selected':''}>${x*100}%</option>`).join('')}</select></label>`:''}${btn('刷新','reload')}</div></div>
      <div class="pw-filter-bar">${[['all','全部节点'],['mine','我负责 / 协作'],['review','待我审核'],['overdue','已逾期']].map(([id,label])=>`<button data-pw="scope" data-value="${id}" class="${scope===id?'active':''}">${label}${id==='review'&&auth.user?` <b>${all.filter(n=>n.status==='pending_approval'&&n.config.reviewer_ids.includes(auth.user.id)).length}</b>`:''}</button>`).join('')}<span class="pw-filter-help">${display==='graph'?'点击节点，查看事项与交付要求':'与流程图使用同一份任务数据'}</span></div>
      ${!nodes.length?empty('当前筛选下没有节点','可以切回全部节点，或调整搜索条件。'):display==='graph'?graphMarkup(nodes):display==='schedule'?scheduleMarkup(nodes):listMarkup(nodes)}
      <div class="pw-board-footer"><div class="pw-legend"><span><i></i>未开始</span><span><i class="active"></i>进行中</span><span><i class="pending"></i>待审核</span><span><i class="complete"></i>已完成</span></div><span>连线表示前置关系 · 阶段内可并行推进 · ${mode==='server'?'服务器实时读取':'示例数据'}</span></div></section>
      <div class="pw-bottom-note">${icon('shield')}填写项目与审核人员仅由流程管理员配置。已提交版本和操作记录会保留；文件链接沿用原平台访问权限。</div>
    </div>`;
  }
  function graphMarkup(nodes){
    return `<div class="pw-canvas-scroll" tabindex="0" aria-label="可滚动流程画布"><div class="pw-canvas" style="zoom:${zoom}"><svg class="pw-edges" aria-hidden="true"><defs><marker id="pwArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10" fill="none" stroke="currentColor" stroke-width="1.7"/></marker></defs><g id="pwPaths"></g></svg>${stageNames.map((stage,i)=>`<section class="pw-stage"><div class="pw-stage-head"><span>0${i+1}</span><h3>${stage}</h3><small>${nodes.filter(n=>stageOf(n)===i).length}</small></div><div class="pw-stage-nodes">${nodes.filter(n=>stageOf(n)===i).map(nodeCard).join('')||'<div class="pw-empty-stage">当前阶段暂无节点</div>'}</div></section>`).join('')}</div></div>`;
  }
  function nodeCard(n){const disabled=n.blockers.length&&!done(n);return `<button class="pw-node ${e(n.status)} ${disabled?'waiting':''}" data-pw="node" data-id="${e(n.id)}" aria-label="${e(n.title)}，${e(statuses[n.status])}"><div class="pw-node-top"><span>${icon(catalog.modules.find(m=>m.id===n.module_code)?.icon||'file')}${e(catalog.modules.find(m=>m.id===n.module_code)?.name||n.stage_name)}</span>${badge(n)}</div><h4>${e(n.title)}</h4><div class="pw-node-place">${icon('link')}${e(n.config.location||'地点 / 协作空间待配置')}</div><div class="pw-node-bottom"><span><i class="pw-avatar small">${e(name(n.owner_id).slice(0,1))}</i>${e(name(n.owner_id))}</span><span class="${overdue(n)?'red':''}">${icon('calendar')}${e(shortDate(n.due_date))}${overdue(n)?' · 逾期':''}</span></div><div class="pw-node-foot"><span>${e(n.approval_summary?.current_stage||'申请准备')} · 审批单</span><span>${disabled?'等待 '+n.blockers.length+' 项前置':n.configured?'v'+n.config.version+' →':'查看 →'}</span></div></button>`;}
  function drawEdges(){
    const canvas=$('.pw-canvas'),svg=$('.pw-edges'),paths=$('#pwPaths');if(!canvas||!svg||!paths||!data)return;
    const base=canvas.getBoundingClientRect(),scale=base.width/canvas.offsetWidth;
    svg.setAttribute('width',canvas.scrollWidth);svg.setAttribute('height',canvas.scrollHeight);
    const edges=[];
    visibleNodes().forEach(n=>n.dependencies.forEach(id=>{const from=canvas.querySelector(`[data-id="${CSS.escape(id)}"]`),to=canvas.querySelector(`[data-id="${CSS.escape(n.id)}"]`);if(!from||!to)return;
      const a=from.getBoundingClientRect(),b=to.getBoundingClientRect();let x1=(a.right-base.left)/scale,y1=(a.top+a.height/2-base.top)/scale,x2=(b.left-base.left)/scale,y2=(b.top+b.height/2-base.top)/scale,d;
      if(Math.abs(a.left-b.left)<5){x2=(b.right-base.left)/scale;const bend=x1+17;d=`M${x1},${y1} C${bend},${y1} ${bend},${y1} ${bend},${(y1+y2)/2} S${bend},${y2} ${x2+2},${y2}`;}
      else if(x2>x1){const mid=(x1+x2)/2;d=`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2-3},${y2}`;}
      else{const bend=x1+18;d=`M${x1},${y1} C${bend},${y1} ${bend},${y2-60} ${x2-14},${y2-60} S${x2-14},${y2} ${x2-2},${y2}`;}
      edges.push(`<path d="${d}" class="${done(data.nodes.find(x=>x.id===id)||{})?'complete':''}" marker-end="url(#pwArrow)"/>`);
    }));paths.innerHTML=edges.join('');
  }
  function listMarkup(nodes){return `<div class="table-wrap"><table class="task-table pw-node-table"><thead><tr><th>事项 / 工作节点</th><th>负责人</th><th>计划时间</th><th>地点</th><th>审核人员</th><th>状态</th></tr></thead><tbody>${nodes.map(n=>`<tr><td><button class="task-name" data-pw="node" data-id="${e(n.id)}">${e(n.title)}</button><small class="task-sub">${n.configured?n.config.fields.length+' 项填写 · 配置 v'+n.config.version:'待配置填写项目与审核人员'}</small></td><td>${e(name(n.owner_id))}</td><td>${e(n.config.planned_start||'开始待定')}<br>${e(n.due_date||'截止待定')}</td><td>${e(n.config.location||'待安排')}</td><td>${e(n.config.reviewer_ids.map(name).join('、')||'待指定')}</td><td>${badge(n)}</td></tr>`).join('')}</tbody></table></div>`;}
  function scheduleMarkup(nodes){
    return `<div class="pw-schedule">${[...nodes].sort((a,b)=>(a.due_date||'9999').localeCompare(b.due_date||'9999')).map(n=>`<button class="pw-schedule-row" data-pw="node" data-id="${e(n.id)}"><div class="pw-schedule-date ${overdue(n)?'red':''}"><b>${e(shortDate(n.due_date))}</b><small>${n.due_date?'截止日期':'待安排'}</small></div><div class="pw-schedule-line"><i class="${done(n)?'complete':''}"></i></div><div><strong>${e(n.title)}</strong><small>${e(n.config.planned_start||'开始待定')} → ${e(n.due_date||'截止待定')} · ${e(name(n.owner_id))} · ${e(n.config.location||'地点待安排')}</small></div>${badge(n)}</button>`).join('')}</div>`;
  }
  function close(force=false){if(dirty&&!force&&!confirm('有尚未保存的填写或配置，确认离开？'))return false;portal().innerHTML='';dirty=false;detail=null;configDraft=null;returnFocus?.focus();return true;}
  async function openNode(id){
    if(window.approvalsUI){await window.approvalsUI.open(id);return;}
    if(detail?.node.id!==id&&dirty&&!confirm('有尚未保存的内容，确认打开其他节点？'))return;
    if(mode!=='server')return toast('本机示例仅展示流程图，协作操作需要连接后端。');
    try{const result=await api(`/api/process/nodes/${id}`);if(view!=='process')return;if(data?.product.id!==result.node.product_id)await loadGraph(true);returnFocus=document.activeElement;detail=result;answers=structuredClone(result.answers);dirty=false;tab='form';configDraft=null;drawDrawer(false);}
    catch(err){toast(err.message)}
  }
  function editable(){return detail?.node.permissions.fill&&!['completed','pending_approval','not_applicable'].includes(detail.node.status);}
  function drawDrawer(keepScroll=true){
    if(!detail)return;const scroll=keepScroll?($('.pw-drawer-body')?.scrollTop||0):0;const n=detail.node;
    portal().innerHTML=`<div class="pw-backdrop" data-pw="close"></div><aside class="pw-drawer" role="dialog" aria-modal="true" aria-labelledby="pwDrawerTitle"><header class="pw-drawer-head"><div><span class="eyebrow">${e(data?.product.model||'')} / ${e(n.stage_name)}</span><h2 id="pwDrawerTitle">${e(n.title)}</h2><div>${badge(n)}<span>配置 ${n.configured?'v'+n.config.version:'尚未启用'} · ${n.config.reviewer_ids.length} 位审核人</span></div></div><button class="close-btn" data-pw="close" aria-label="关闭节点详情">×</button></header><div class="pw-drawer-tabs">${[['form','填写与交付'],['people','人员与审核'],['history','版本与记录'],...(n.permissions.configure?[['config','配置节点']]:[])].map(([id,label])=>`<button data-pw="tab" data-value="${id}" class="${tab===id?'active':''}">${label}</button>`).join('')}</div><div class="pw-drawer-body">${tab==='config'?configMarkup():tab==='people'?peopleMarkup():tab==='history'?historyMarkup():formMarkup()}</div><div class="pw-drawer-actions">${footerMarkup()}</div></aside>`;
    if($('.pw-drawer-body'))$('.pw-drawer-body').scrollTop=scroll;
    const first=$('.pw-drawer .close-btn');if(first&&!document.activeElement?.closest('.pw-drawer'))first.focus();
  }
  function formMarkup(){const n=detail.node,can=editable()&&!busy;
    return `<div class="pw-node-facts"><div><span>负责人</span><strong>${e(name(n.owner_id))}</strong></div><div><span>计划开始 → 截止</span><strong>${e(n.config.planned_start||'待安排')} → ${e(n.due_date||'待安排')}</strong></div><div><span>工作地点 / 协作空间</span><strong>${e(n.config.location||'待管理员配置')}</strong></div><div><span>实际开始 → 完成</span><strong>${dt(n.started_at)} → ${n.completed_at?dt(n.completed_at):'尚未完成'}</strong></div></div>
      ${n.blockers.length?`<div class="pw-callout amber">${icon('clock')}<div><b>等待 ${n.blockers.length} 项前置完成</b><p>${e(n.blockers.map(b=>b.title).join('、'))}</p><small>可以提前保存资料草稿，前置完成后才能提交验收。</small></div></div>`:''}
      ${!n.configured?`<div class="pw-callout">${icon('settings')}<div><b>该节点尚未配置</b><p>管理员可指定填写人员、审核人，添加需要收集的项目。</p></div></div>`:!auth.user?`<div class="pw-callout">${icon('people')}<div><b>登录后查看填写记录并参与协作</b><p>当前展示事项和要求，已填写内容仅向登录成员展示。</p></div></div>`:n.status==='pending_approval'?'<div class="pw-callout amber"><div><b>当前提交正在审核</b><p>此版本已锁定。修改配置需要管理员明确撤回，历史提交会保留。</p></div></div>':!can?'<div class="pw-callout"><div>当前为只读查看。只有负责人、协作人和管理员可填写；已完成节点保留验收时的记录。</div></div>':''}
      ${n.acceptance_criteria?`<div class="pw-acceptance"><span>验收标准</span><p>${e(n.acceptance_criteria)}</p></div>`:''}
      <form id="pwAnswersForm">${n.config.fields.map(f=>fieldMarkup(f,can)).join('')}</form>
      ${n.status==='in_progress'&&detail.submissions[0]?.decisions.some(d=>d.decision==='rejected')?`<div class="pw-callout amber"><div><b>需要补充修改</b><p>${e(detail.submissions[0].decisions.find(d=>d.decision==='rejected').comment)}</p></div></div>`:''}${reviewMarkup()}`;
  }
  function fieldMarkup(f,can){
    const value=answers[f.id]??'',attrs=`data-answer="${e(f.id)}" data-type="${e(f.type)}" ${can?'':'disabled'} ${f.required?'required':''}`;
    let input;
    if(f.type==='textarea')input=`<textarea rows="4" maxlength="10000" ${attrs} placeholder="${e(f.help||'填写处理结论与需要说明的事项')}">${e(value)}</textarea>`;
    else if(f.type==='select')input=`<select ${attrs}><option value="">请选择</option>${f.options.map(x=>`<option ${value===x?'selected':''}>${e(x)}</option>`).join('')}</select>`;
    else if(f.type==='multiselect')input=`<select multiple ${attrs}>${f.options.map(x=>`<option ${(Array.isArray(value)?value:[]).includes(x)?'selected':''}>${e(x)}</option>`).join('')}</select>`;
    else if(f.type==='person')input=`<select ${attrs}>${memberOptions(value)}</select>`;
    else if(f.type==='attachment')input=attachmentMarkup(f,can);
    else input=`<input type="${f.type==='number'?'number':f.type==='date'?'date':'text'}" ${f.type==='number'?'step="any"':''} ${attrs} value="${e(value)}" maxlength="10000" placeholder="${e(f.help||'请填写')}">`;
    return `<div class="pw-form-field"><label class="pw-field-label" ${f.type==='attachment'?'':`for="pwField_${e(f.id)}"`}>${e(f.label)}${f.required?'<span>*</span>':'<small>选填</small>'}</label>${f.type==='attachment'?input:input.replace(/(<input |<textarea |<select )/,`$1id="pwField_${e(f.id)}" aria-label="${e(f.label)}" `)}${f.help?`<small class="pw-field-help">${e(f.help)}</small>`:''}</div>`;
  }
  function attachmentMarkup(f,can){const items=Array.isArray(answers[f.id])?answers[f.id]:[];
    return `<div class="pw-attachments">${items.map((a,i)=>`<div class="pw-attachment">${icon('file')}<div>${a.kind==='file'?`<a href="/api/process/attachments/${e(a.id)}" target="_blank" rel="noopener noreferrer">${e(a.title)}</a>`:`<a href="${e(a.url)}" target="_blank" rel="noopener noreferrer">${e(a.title)}</a>`}<small>${e(a.version||'v1')} · ${a.kind==='file'?'已上传文件':'外部资料链接'}</small></div>${can?`<button type="button" class="text-btn" data-pw="remove-attachment" data-field="${e(f.id)}" data-index="${i}" aria-label="移除${e(a.title)}">移除</button>`:''}</div>`).join('')||'<div class="pw-no-attachment">尚未登记交付附件</div>'}${can?`<div class="pw-upload-row"><label class="btn">${icon('upload')}上传文件<input type="file" data-upload="${e(f.id)}" aria-label="上传${e(f.label)}" hidden></label><span>单文件最多 10 MB</span></div><div class="pw-link-form"><input data-link-title="${e(f.id)}" placeholder="资料名称" aria-label="附件名称"><input data-link-url="${e(f.id)}" type="url" placeholder="https:// 飞书 / 网盘文件链接" aria-label="附件链接"><input data-link-version="${e(f.id)}" placeholder="版本，例如 v1" aria-label="附件版本"><button type="button" class="btn" data-pw="add-link" data-field="${e(f.id)}">添加链接</button></div>`:''}</div>`;
  }
  function currentSubmission(){const n=detail.node;return detail.submissions.find(s=>s.task_revision===n.revision&&s.config_version===n.config.version);}
  function reviewMarkup(){
    const n=detail.node,s=currentSubmission();if(!auth.user||!s)return '';
    const already=s.decisions.some(d=>d.reviewer_id===auth.user.id),reviewers=n.config.reviewer_ids,doneIds=s.decisions.filter(d=>d.decision==='approved').map(d=>d.reviewer_id),next=reviewers.find(id=>!doneIds.includes(id));
    const can=n.status==='pending_approval'&&n.permissions.review&&!already&&(n.config.review_mode!=='sequential'||next===auth.user.id);
    return `<section class="pw-review-box"><h3>${icon('shield')}本次验收 <small>${doneIds.length} / ${reviewers.length} 人已通过</small></h3><p>${{all:'所有指定审核人均通过后完成',any:'任一指定审核人通过后完成',sequential:'按配置顺序依次审核'}[n.config.review_mode]}</p><div class="pw-review-people">${reviewers.map((id,i)=>`<span class="${doneIds.includes(id)?'approved':''}">${n.config.review_mode==='sequential'?i+1+'. ':''}${e(name(id))}${doneIds.includes(id)?' ✓':''}</span>`).join('')}</div>${s.decisions.map(d=>`<div class="pw-review-comment"><b>${e(d.reviewer_name)} · ${d.decision==='approved'?'通过':'退回'}</b><p>${e(d.comment||'已核对本次交付')}</p></div>`).join('')}${can?`<label class="field">审核意见<textarea id="pwReviewComment" maxlength="2000" placeholder="退回时请明确需要补充或修改的内容"></textarea></label><div class="filter-row">${btn('退回修改','reject')}${btn('通过本次验收','approve',true)}</div>`:n.status==='pending_approval'?`<small>${already?'你已完成本次审核。':n.config.review_mode==='sequential'?'当前等待 '+e(name(next))+' 审核。':'等待指定审核人处理。'}</small>`:''}</section>`;
  }
  function peopleMarkup(){const n=detail.node;return `<h3 class="pw-section-title">参与这项工作的人</h3><div class="pw-person-card"><i class="pw-avatar">${e(name(n.owner_id).slice(0,1))}</i><div><strong>${e(name(n.owner_id))}</strong><small>负责人 · 填写事项、整理资料并提交验收</small></div></div>${n.config.collaborator_ids.map(id=>`<div class="pw-person-card"><i class="pw-avatar">${e(name(id).slice(0,1))}</i><div><strong>${e(name(id))}</strong><small>协作人 · 补充填写内容与附件</small></div></div>`).join('')}<h3 class="pw-section-title">审核人员</h3><p class="muted">${{all:'会签：全部通过',any:'或签：任一通过',sequential:'按顺序依次审核'}[n.config.review_mode]}</p>${n.config.reviewer_ids.map((id,i)=>`<div class="pw-person-card"><span class="pw-step-number">${i+1}</span><div><strong>${e(name(id))}</strong><small>审核交付内容，可通过或退回</small></div></div>`).join('')||'<p class="muted">待管理员指定审核人员</p>'}<div class="pw-callout"><div>负责人和审核人身份不包含配置权。填写项目、人员分配和审核规则仅由流程管理员维护。</div></div>${reviewMarkup()}`;}
  function historyMarkup(){
    if(!auth.user)return '<div class="pw-callout">登录后查看提交版本、审核意见和操作记录。</div>';
    return `<h3 class="pw-section-title">交付与审核版本 <small>${detail.submissions.length}</small></h3>${detail.submissions.map((s,i)=>`<details class="pw-history-version" ${i===0?'open':''}><summary><span>第 ${detail.submissions.length-i} 次提交 · 配置 v${s.config_version}</span><small>${dt(s.created_at)}</small></summary><div class="pw-history-body"><p class="muted">提交人：${e(s.submitted_name)} · 审核：${e(s.snapshot.reviewer_ids.map(name).join('、'))}</p>${s.snapshot.fields.map(f=>`<div class="pw-snapshot-item"><b>${e(f.label)}</b><div>${snapshotValue(f,s.answers[f.id])}</div></div>`).join('')}${s.decisions.map(d=>`<div class="pw-review-comment"><b>${e(d.reviewer_name)} · ${d.decision==='approved'?'通过':'退回'}</b><p>${e(d.comment||'已审核')}</p><small>${dt(d.created_at)}</small></div>`).join('')||'<p class="muted">本次提交暂无审核记录，可能已被配置变更撤回。</p>'}</div></details>`).join('')||'<p class="pw-hint">尚无已提交版本；保存草稿不会生成验收版本。</p>'}<h3 class="pw-section-title">配置版本</h3>${detail.config_history.map(h=>`<details class="pw-history-version"><summary><span>配置 v${h.version} · ${h.snapshot.fields.length} 项填写</span><small>${dt(h.created_at)}</small></summary><div class="pw-history-body"><p>负责人：${e(name(h.snapshot.owner_id))}</p><p>审核人：${e(h.snapshot.reviewer_ids.map(name).join('、'))}</p><p>地点：${e(h.snapshot.location||'待安排')}</p><p>填写项目：${e(h.snapshot.fields.map(f=>f.label+(f.required?'（必填）':'')).join('、'))}</p></div></details>`).join('')||'<p class="pw-hint">尚未启用配置。</p>'}<h3 class="pw-section-title">操作记录</h3><div class="pw-audit">${detail.activity.map(a=>`<div><i></i><section><b>${e(a.display_name||'系统')} · ${e(actionNames[a.action]||a.action)}</b>${a.after_data?.comment||a.after_data?.reason?`<p>${e(a.after_data.comment||a.after_data.reason)}</p>`:''}<small>${dt(a.created_at)}</small></section></div>`).join('')||'<p class="muted">暂无操作记录</p>'}</div>`;
  }
  function snapshotValue(f,value){if(value===undefined||value===null||value==='')return '<span class="muted">未填写</span>';if(f.type==='attachment'&&Array.isArray(value))return value.map(x=>`<a href="${x.kind==='file'?'/api/process/attachments/'+e(x.id):e(x.url)}" target="_blank" rel="noopener noreferrer">${e(x.title)} · ${e(x.version||'v1')}</a>`).join('<br>');if(f.type==='person')return e(name(value));return e(Array.isArray(value)?value.join('、'):value);}
  function startConfig(){const n=detail.node;configDraft={...structuredClone(n.config),owner_id:n.owner_id||auth.user.id,due_date:n.due_date||'',planned_start:n.config.planned_start||'',reviewer_ids:n.config.reviewer_ids.length?[...n.config.reviewer_ids]:[auth.user.id],dependencies:[...n.dependencies],recall_pending:false};}
  function personChecks(key,selected){return `<div class="pw-person-checks">${(data?.people||[]).map(p=>`<label><input type="checkbox" data-config-people="${key}" value="${e(p.id)}" ${selected.includes(p.id)?'checked':''}><span>${e(p.display_name)}<small>${e(p.department)}</small></span></label>`).join('')||'<p>请先在“成员与账户”中创建登录成员。</p>'}</div>`;}
  function configMarkup(){
    const n=detail.node;if(!n.permissions.configure)return '<div class="pw-callout">仅流程管理员可配置。</div>';if(!configDraft)startConfig();
    if(done(n))return '<div class="pw-callout amber"><div><b>已完成的节点保留验收快照</b><p>请使用底部“重新打开”操作，再修改填写项目与审核人员。</p></div></div>';
    const c=configDraft;
    return `<div class="pw-callout"><div><b>配置仅应用于当前产品的这个节点</b><p>保存后生成新版本。其他节点、其他产品的填写项目与审核人员保持各自配置。</p></div></div><form id="pwConfigForm"><h3 class="pw-section-title">01 · 时间、地点与分工</h3><label class="field">负责人<select data-config="owner_id" required>${memberOptions(c.owner_id)}</select></label><div class="form-row"><label class="field">计划开始<input type="date" data-config="planned_start" value="${e(c.planned_start||'')}"></label><label class="field">截止日期<input type="date" data-config="due_date" value="${e(c.due_date||'')}"></label></div><label class="field">地点 / 协作空间<input data-config="location" value="${e(c.location)}" maxlength="300" placeholder="例如：深圳工厂 / FBA 仓库 / 线上设计协作"></label><label class="pw-field-label">协作人员</label>${personChecks('collaborator_ids',c.collaborator_ids)}
      <div class="pw-section-heading"><h3>02 · 需要填写的项目</h3><span>${c.fields.length} / 30 项</span></div><div class="pw-field-editors">${c.fields.map((f,i)=>`<section class="pw-field-editor"><div class="pw-field-editor-title"><span>${String(i+1).padStart(2,'0')}</span><strong>${e(types[f.type])}</strong><button type="button" class="text-btn red" data-pw="remove-field" data-index="${i}" aria-label="删除填写项目${e(f.label)}">移除</button></div><div class="form-row"><label class="field">项目名称<input data-field-index="${i}" data-field-key="label" required maxlength="100" value="${e(f.label)}" placeholder="填写项目名称"></label><label class="field">填写类型<select data-field-index="${i}" data-field-key="type">${Object.entries(types).map(([id,label])=>`<option value="${id}" ${f.type===id?'selected':''}>${label}</option>`).join('')}</select></label></div><label class="field">填写提示<input data-field-index="${i}" data-field-key="help" maxlength="500" value="${e(f.help)}" placeholder="说明需要提供什么，以及如何验收"></label>${['select','multiselect'].includes(f.type)?`<label class="field">选项（每行一个）<textarea data-field-index="${i}" data-field-key="options" required>${e(f.options.join('\n'))}</textarea></label>`:''}<label class="pw-checkbox"><input type="checkbox" data-field-index="${i}" data-field-key="required" ${f.required?'checked':''}>提交验收前必须填写</label></section>`).join('')}</div>${btn(icon('plus')+'添加填写项目','add-field',false,c.fields.length>=30?'disabled':'')}
      <h3 class="pw-section-title">03 · 指定审核人员</h3>${personChecks('reviewer_ids',c.reviewer_ids)}<label class="field">审核方式<select data-config="review_mode">${[['all','全部通过（会签）'],['any','任一通过（或签）'],['sequential','依次通过（顺序审核）']].map(([id,label])=>`<option value="${id}" ${c.review_mode===id?'selected':''}>${label}</option>`).join('')}</select></label>${c.review_mode==='sequential'?`<div class="pw-review-order">${c.reviewer_ids.map((id,i)=>`<div><span>${i+1}. ${e(name(id))}</span>${i?btn('上移','reviewer-up',false,`data-index="${i}"`):''}</div>`).join('')}</div>`:''}
      <h3 class="pw-section-title">04 · 前置节点</h3><div class="pw-dependency-checks">${(data?.nodes||[]).filter(x=>x.id!==n.id).map(x=>`<label><input type="checkbox" data-dependency="${e(x.id)}" ${c.dependencies.includes(x.id)?'checked':''}><span>${e(x.title)}</span></label>`).join('')}</div><small class="pw-field-help">保存时会检查跨产品依赖与循环关系。</small>
      ${n.status==='pending_approval'?`<div class="pw-callout amber"><label class="pw-checkbox"><input type="checkbox" data-config="recall_pending" ${c.recall_pending?'checked':''} required>确认撤回当前审核，让填写人员按新配置重新提交。旧提交和审核记录保留。</label></div>`:''}</form>`;
  }
  function footerMarkup(){const n=detail.node;if(busy)return '<span class="muted">正在保存，请稍候…</span>';
    if(tab==='config'&&!done(n))return `<span class="pw-save-note">${dirty?'有未保存的配置':'仅管理员可更改配置'}</span>${btn('关闭','close')}${btn('保存配置版本','save-config',true)}`;
    if(tab==='form'&&editable())return `<span class="pw-save-note" id="pwDirty">${dirty?'有未保存的填写':'填写后先保存，也可直接提交'}</span>${btn('保存草稿','save-draft')}${btn('提交验收','submit',true,n.blockers.length?'disabled title="前置节点尚未完成"':'')}`;
    return `${auth.user?.is_admin&&(n.configured||done(n))?btn('重新打开 / 撤回','reopen'):''}<span class="pw-save-note"></span>${!auth.user?btn('登录参与协作','login',true):btn('关闭','close')}`;
  }
  function markDirty(){dirty=true;const note=$('#pwDirty');if(note)note.textContent='有未保存的填写';}
  async function changed(id){dirty=false;await refreshWorkspace(false);await loadGraph(true);await openNode(id);}
  async function work(action){
    if(busy||!detail)return;const id=detail.node.id;
    if(action==='submit'&&!$('#pwAnswersForm')?.reportValidity())return;
    busy=true;drawDrawer();
    try{await api(`/api/process/nodes/${id}/work`,{method:'POST',body:JSON.stringify({action,expected_version:detail.node.state_version,answers})});busy=false;await changed(id);toast(action==='submit'?'已提交，等待指定审核人验收。':'草稿已保存。');}
    catch(err){busy=false;drawDrawer();showPanelError(err.message);}
  }
  function showPanelError(message){const body=$('.pw-drawer-body');if(body){body.querySelector('.pw-inline-error')?.remove();const div=document.createElement('div');div.className='pw-inline-error';div.setAttribute('role','alert');div.textContent=message;body.prepend(div);body.scrollTop=0;}toast(message);}
  async function saveConfig(){
    if(busy||!$('#pwConfigForm').reportValidity())return;
    const n=detail.node,c=configDraft;
    if(!c.reviewer_ids.length)return showPanelError('请至少指定一位审核人员。');
    if(!c.fields.length)return showPanelError('请至少保留一个填写项目。');
    const body={expected_version:n.state_version,owner_id:c.owner_id,fields:c.fields,collaborator_ids:c.collaborator_ids,reviewer_ids:c.reviewer_ids,review_mode:c.review_mode,planned_start:c.planned_start||null,due_date:c.due_date||null,location:c.location,dependencies:c.dependencies,recall_pending:c.recall_pending};
    busy=true;drawDrawer();
    try{await api(`/api/process/nodes/${n.id}/config`,{method:'PUT',body:JSON.stringify(body)});busy=false;await changed(n.id);toast('配置已保存为新版本，填写与审核权限已生效。');}
    catch(err){busy=false;drawDrawer();showPanelError(err.message);}
  }
  async function decide(decision){
    if(busy)return;const comment=$('#pwReviewComment')?.value||'';if(decision==='rejected'&&!comment.trim())return showPanelError('退回时请说明需要补充或修改的内容。');
    const n=detail.node,s=currentSubmission();if(!s)return;
    busy=true;
    try{await api(`/api/process/nodes/${n.id}/review`,{method:'POST',body:JSON.stringify({expected_version:n.state_version,submission_id:s.id,decision,comment})});busy=false;await changed(n.id);toast(decision==='approved'?'审核意见已记录。':'已退回，填写人员可以修改并重新提交。');}
    catch(err){busy=false;showPanelError(err.message);}
  }
  function authDialog(){
    if(dirty&&!confirm('登录会关闭当前编辑面板，确认离开未保存内容？'))return;
    close(true);const setup=authReady&&!auth.initialized;
    const dialog=document.createElement('dialog');dialog.id='pwAuthDialog';dialog.className='pw-auth-dialog';
    dialog.innerHTML=`<form id="pwAuthForm"><div class="pw-auth-mark">H</div><span class="eyebrow">HOMA · PROCESS WORKSPACE</span><h2>${setup?'设置你的流程管理员账户':'登录流程协作'}</h2><p>${setup?'只有持有部署端设置码的人可以创建首位管理员。':'使用管理员为你开通的个人账户，处理自己的填写与审核。'}</p>${setup?'<label class="field">管理员设置码<input name="setup_code" type="password" required autocomplete="off"><small>在受信任的部署终端运行：python -m app.workflow_auth 获取。</small></label><label class="field">你的姓名<input name="display_name" required maxlength="100" autocomplete="name"></label>':''}<label class="field">登录邮箱<input name="email" type="email" required autocomplete="username"></label><label class="field">密码<input name="password" type="password" minlength="12" maxlength="200" required autocomplete="${setup?'new-password':'current-password'}"><small>至少 12 个字符。</small></label><div id="pwAuthError" class="pw-auth-error" role="alert"></div><div class="pw-auth-actions">${btn('取消','close-auth')}<button class="btn primary" type="submit">${setup?'创建管理员并登录':'登录'}</button></div></form>`;
    document.body.append(dialog);dialog.showModal();
    dialog.addEventListener('close',()=>dialog.remove());
    dialog.querySelector('form').addEventListener('submit',async ev=>{ev.preventDefault();const submit=dialog.querySelector('[type="submit"]');submit.disabled=true;try{const b=Object.fromEntries(new FormData(ev.target));await api('/api/workflow-auth/'+(setup?'setup':'login'),{method:'POST',body:JSON.stringify(b)});await session();dialog.close();invalidate();await refreshWorkspace(false);render();toast('已登录，操作权限按当前账户生效。');}catch(err){$('#pwAuthError').textContent=err.message;submit.disabled=false;}});
  }
  async function membersDialog(){
    if(!auth.user?.is_admin)return;const people=await api('/api/workflow-auth/members');
    const dialog=document.createElement('dialog');dialog.id='pwMembersDialog';dialog.className='pw-members-dialog';
    dialog.innerHTML=`<div class="pw-members-head"><div><span class="eyebrow">WORKSPACE MEMBERS</span><h2>成员与登录账户</h2><p>新增成员可被指定为填写人员或审核人，不会获得流程配置权。</p></div><button class="close-btn" data-pw="close-members" aria-label="关闭成员面板">×</button></div><div class="pw-members-list">${people.map(p=>`<div><span class="pw-avatar">${e(p.display_name.slice(0,1))}</span><div><b>${e(p.display_name)}</b><small>${e(p.email)} · ${e(p.department)}</small></div><span class="label">${p.is_admin?'流程管理员':'协作成员'}</span></div>`).join('')}</div><form id="pwMemberForm"><h3>新增登录成员</h3><div class="form-row"><label class="field">姓名<input name="display_name" required maxlength="100"></label><label class="field">部门<input name="department" required maxlength="100" placeholder="运营 / 设计 / 供应链"></label></div><label class="field">登录邮箱<input type="email" name="email" required autocomplete="off"></label><label class="field">初始密码<input type="password" name="password" minlength="12" maxlength="200" required autocomplete="new-password"><small>至少 12 个字符。通过安全渠道告知本人。</small></label><div id="pwMemberError" role="alert" class="pw-auth-error"></div><div class="pw-auth-actions">${btn('关闭','close-members')}<button class="btn primary" type="submit">创建协作账户</button></div></form>`;
    document.body.append(dialog);dialog.showModal();dialog.addEventListener('close',()=>dialog.remove());
    dialog.querySelector('form').addEventListener('submit',async ev=>{ev.preventDefault();const submit=ev.target.querySelector('[type="submit"]');submit.disabled=true;try{await api('/api/workflow-auth/members',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(ev.target)))});dialog.close();await refreshWorkspace(false);await loadGraph(true);await membersDialog();toast('成员账户已创建，配置节点时可以选择该人员。');}catch(err){$('#pwMemberError').textContent=err.message;submit.disabled=false;}});
  }
  async function handle(el){const action=el.dataset.pw;if(busy)return;
    if(action==='login'){if(!authReady)await session();authDialog();}
    else if(action==='account'){if(confirm('当前登录：'+auth.user.display_name+'。是否退出该账户？')){await api('/api/workflow-auth/logout',{method:'POST'});await session();close(true);invalidate();render();}}
    else if(action==='close-auth')$('#pwAuthDialog')?.close();
    else if(action==='close-members')$('#pwMembersDialog')?.close();
    else if(action==='members')await membersDialog();
    else if(action==='reload'){if(dirty&&!confirm('刷新会放弃未保存内容，是否继续？'))return;close(true);await refreshWorkspace(false);await loadGraph(true);}
    else if(action==='view'){display=el.dataset.value;render();}
    else if(action==='scope'){if(['mine','review'].includes(el.dataset.value)&&!auth.user)return authDialog();scope=el.dataset.value;render();}
    else if(action==='node')await openNode(el.dataset.id);
    else if(action==='close'){if(!busy)close();}
    else if(action==='tab'){if(dirty&&tab==='config'&&el.dataset.value!=='config'&&!confirm('尚未保存节点配置，确认放弃？'))return;if(tab==='config'&&el.dataset.value!=='config'){configDraft=null;dirty=false;}if(el.dataset.value==='config'&&dirty&&tab!=='config'){if(!confirm('还有未保存的填写内容，确认进入配置？'))return;answers=structuredClone(detail.answers);dirty=false;}tab=el.dataset.value;drawDrawer();}
    else if(action==='add-field'){if(configDraft.fields.length>=30)return;configDraft.fields.push({id:'custom_'+uuid().replaceAll('-',''),label:'',type:'text',required:false,help:'',options:[]});markDirty();drawDrawer();const fields=$$('.pw-field-editor input[data-field-key="label"]');fields.at(-1)?.focus();}
    else if(action==='remove-field'){configDraft.fields.splice(Number(el.dataset.index),1);markDirty();drawDrawer();}
    else if(action==='reviewer-up'){const i=Number(el.dataset.index);[configDraft.reviewer_ids[i-1],configDraft.reviewer_ids[i]]=[configDraft.reviewer_ids[i],configDraft.reviewer_ids[i-1]];markDirty();drawDrawer();}
    else if(action==='save-config')await saveConfig();
    else if(action==='save-draft')await work('save');
    else if(action==='submit')await work('submit');
    else if(action==='approve')await decide('approved');
    else if(action==='reject')await decide('rejected');
    else if(action==='reopen'){
      const n=detail.node,reason=prompt('重新打开会撤回此节点，以及已开始的后续节点的验收状态，保留全部历史。请填写原因：');if(!reason?.trim())return;
      const result=await api(`/api/process/nodes/${n.id}/work`,{method:'POST',body:JSON.stringify({action:'reopen',expected_version:n.state_version,reason})});await changed(n.id);toast(`已重新打开 ${result.affected} 个节点，相关内容需要重新验收。`);
    }
    else if(action==='add-link'){
      const id=el.dataset.field,title=$(`[data-link-title="${CSS.escape(id)}"]`).value.trim(),url=$(`[data-link-url="${CSS.escape(id)}"]`).value.trim(),version=$(`[data-link-version="${CSS.escape(id)}"]`).value.trim()||'v1';
      try{const parsed=new URL(url);if(!['http:','https:'].includes(parsed.protocol)||parsed.username||parsed.password||/\s/.test(url))throw new Error();}catch{return showPanelError('请填写有效的 HTTP(S) 资料链接。');}
      if(!title)return showPanelError('请填写资料名称。');answers[id]=[...(answers[id]||[]),{kind:'link',title,url,version}];markDirty();drawDrawer();
    }
    else if(action==='remove-attachment'){answers[el.dataset.field].splice(Number(el.dataset.index),1);markDirty();drawDrawer();}
    else if(action==='enable'){
      if(!confirm('将为尚未配置的节点建立填写项目和审核要求，默认由你负责并审核；你可逐项重新分配人员。旧版示例任务还会建立上线前置关系。是否继续？'))return;
      const result=await api(`/api/process/products/${data.product.id}/enable`,{method:'POST'});await refreshWorkspace(false);await loadGraph(true);toast(`已启用 ${result.enabled} 个节点，可逐项配置人员与填写要求。`);
    }
  }
  document.addEventListener('click',ev=>{const el=ev.target.closest('[data-pw]');if(!el)return;ev.preventDefault();handle(el).catch(err=>{if(detail)showPanelError(err.message);else toast(err.message)});});
  document.addEventListener('input',ev=>{
    const el=ev.target;
    if(el.dataset.answer){let value=el.value;if(el.dataset.type==='number')value=value===''?'':Number(value);if(el.dataset.type==='multiselect')value=Array.from(el.selectedOptions).map(o=>o.value);answers[el.dataset.answer]=value;markDirty();}
    else if(el.dataset.config&&configDraft){configDraft[el.dataset.config]=el.type==='checkbox'?el.checked:el.value;markDirty();}
    else if(el.dataset.fieldIndex!==undefined&&configDraft){const f=configDraft.fields[Number(el.dataset.fieldIndex)],key=el.dataset.fieldKey;f[key]=key==='required'?el.checked:key==='options'?el.value.split('\n').map(x=>x.trim()).filter(Boolean):el.value;markDirty();}
  });
  document.addEventListener('change',async ev=>{
    const el=ev.target;
    if(el.id==='pwZoom'){zoom=Number(el.value);render();}
    else if(el.dataset.configPeople&&configDraft){const ids=configDraft[el.dataset.configPeople];if(el.checked&&!ids.includes(el.value))ids.push(el.value);else if(!el.checked)configDraft[el.dataset.configPeople]=ids.filter(x=>x!==el.value);markDirty();if(configDraft.review_mode==='sequential')drawDrawer();}
    else if(el.dataset.dependency&&configDraft){if(el.checked)configDraft.dependencies.push(el.dataset.dependency);else configDraft.dependencies=configDraft.dependencies.filter(x=>x!==el.dataset.dependency);markDirty();}
    else if(el.dataset.fieldKey==='type'||el.dataset.config==='review_mode')drawDrawer();
    else if(el.dataset.upload){
      const file=el.files[0];if(!file)return;if(file.size>10*1024*1024)return showPanelError('附件不能超过 10 MB。');
      const form=new FormData();form.append('file',file);busy=true;
      try{const uploaded=await api(`/api/process/nodes/${detail.node.id}/attachments`,{method:'POST',body:form});answers[el.dataset.upload]=[...(answers[el.dataset.upload]||[]),uploaded];markDirty();toast('文件已上传，保存草稿或提交后关联到填写项目。');}
      catch(err){showPanelError(err.message)}finally{busy=false;drawDrawer();}
    }
  });
  document.addEventListener('submit',ev=>{if(['pwAnswersForm','pwConfigForm'].includes(ev.target.id))ev.preventDefault();});
  document.addEventListener('keydown',ev=>{
    const drawer=$('.pw-drawer');if(!drawer)return;
    if(ev.key==='Escape'){ev.preventDefault();if(!busy)close();}
    if(ev.key==='Tab'){const nodes=Array.from(drawer.querySelectorAll('button:not(:disabled),input:not(:disabled):not([hidden]),select:not(:disabled),textarea:not(:disabled),a[href]')).filter(x=>x.getClientRects().length);if(!nodes.length)return;const first=nodes[0],last=nodes.at(-1);if(ev.shiftKey&&document.activeElement===first){ev.preventDefault();last.focus();}else if(!ev.shiftKey&&document.activeElement===last){ev.preventDefault();first.focus();}}
  });
  window.addEventListener('beforeunload',ev=>{if(dirty){ev.preventDefault();ev.returnValue='';}});
  window.addEventListener('resize',()=>requestAnimationFrame(drawEdges));
  return {render:renderPage,decorate,openNode,invalidate,currentUser:()=>auth.user,isAdmin:()=>!!auth.user?.is_admin,login:async()=>{if(!authReady)await session();authDialog();}};
})();
