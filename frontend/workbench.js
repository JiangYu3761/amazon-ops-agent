/* Page-specific operating records. Static previews and authenticated server data have separate stores. */
window.workbenchUI = (() => {
  'use strict';
  const definitions = new Map();
  const activeWrites=new Map();
  let openSerial=0;
  const panes = new Map();
  const labels = {draft:'草稿',pending_review:'待审核',approved:'待执行',executed:'待复核',closed:'已归档'};
  const actions = {submit:'提交审核',approve:'批准',return:'退回修订',execute:'登记执行',verify:'完成复核'};
  const stages = {submit:'pending_review',approve:'approved',return:'draft',execute:'executed',verify:'closed'};
  const legacyNames = {overview:'产品矩阵',deliverables:'交付目录',process:'流程图',workflow:'全流程 SOP',team:'成员与审批',calendar:'活动月历',ranking:'排名曲线',agent:'经营诊断与对话',imports:'CSV 导入',settings:'连接与系统配置'};
  const demoStore = 'homa.workbench.records.1';
  const sampleStore = 'homa.workbench.demo.samples.1';
  let sampleRequest, samplesLoaded=false, sampleRows=[], sampleIssue='';
  let demoActor = '', configRequest, configError='', queueKey='', rows=[], signals=[], busy=false, issue='', nextOffset=null;
  let generation=0, editor=null, detailRequest=0, previousQueue=null, truncated=false;
  const q = selector => document.querySelector(selector);
  const escape = value => e(value);
  const control = (text,action,primary=false,attrs='') => `<button type="button" class="btn ${primary?'primary':''}" data-wb="${action}" ${attrs}>${escape(text)}</button>`;
  const personName = id => ws.people.find(p=>p.id===id)?.display_name || '未分配';
  const shopScope=form=>form.page_id==='shop' && (form.record_scope==='shop'||(!form.product_id&&Boolean(form.account_ref)));
  const scopeName=item=>shopScope(item)?'店铺 '+item.account_ref:modelName(item.product_id);
  const modelName = id => ws.products.find(p=>p.id===id)?.model || '未知型号';
  const dt = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '';
  const currentUser = () => mode==='demo' ? ws.people.find(p=>p.id===demoActor) || ws.people[0] : window.processUI?.currentUser();
  const admin = () => mode==='demo' ? currentUser()?.id===ws.people[0]?.id : Boolean(currentUser()?.is_admin);
  function pane(pageId=view){
    if(!panes.has(pageId)){
      const resume=pageId==='ads'&&mode==='server'&&window.adsDecisionReview?.savedRoute()?.product===productId;
      panes.set(pageId,{section:resume?'verification':'',status:'',mine:pageId==='overview'?'mine':'',legacy:false});
    }
    return panes.get(pageId);
  }
  function getStored(key, fallback) {
    const raw = localStorage.getItem(key);
    return raw===null ? fallback : JSON.parse(raw);
  }
  function demoItems(){
    if(mode!=='demo')return [];
    const saved=getStored(demoStore,[]), ids=new Set(saved.map(i=>i.id));
    return [...saved,...sampleRows.filter(sample=>!ids.has(sample.id))];
  }
  async function loadDemoSamples(){
    if(mode!=='demo'||samplesLoaded||!ws.products.length||ws.people.length<2)return;
    if(!sampleRequest)sampleRequest=(async()=>{
      let examples;
      try{examples=getStored(sampleStore,null);}catch{}
      if(!Array.isArray(examples)||!examples.length){
        const result=await fetch('workbench-samples.json');
        if(!result.ok)throw new Error('示例业务单未加载');
        examples=await result.json();
        if(mode!=='demo')return;
      }
      sampleRows=examples.filter(sample=>!sample.product_id||ws.products.some(p=>p.id===sample.product_id)).map(sample=>{
        const product=ws.products.find(p=>p.id===sample.product_id)||ws.products[0];
        const page=definitions.get(sample.page_id),section=page?.sections.find(s=>s.id===sample.section_id);
        if(!section)throw new Error('示例业务环节与模板不一致');
        const owner=ws.people.find(p=>p.id===sample.owner_id)||ws.people[sample.owner_role==='member'?1:0];
        const reviewer=ws.people.find(p=>p.id===sample.reviewer_id&&p.id!==owner.id)||ws.people.find(p=>p.id!==owner.id);
        const id='demo-sample:'+page.id;
        return {id,request_id:id,sample:true,version:Math.max(1,sample.history?.length||0),status:sample.status||'draft',product_id:product.id,
          page_id:page.id,section_id:section.id,title:sample.title,site:sample.site||product.marketplace,
          object_ref:sample.object_ref||'',values:sample.values||{},owner_id:owner.id,reviewer_id:reviewer.id,
          task_id:null,history:sample.history||[],definition:section,source_note:sample.source_note||sample.note||''};
      });
      try{localStorage.setItem(sampleStore,JSON.stringify(examples));}catch{}
      sampleIssue='';samplesLoaded=true;
    })().catch(error=>{sampleIssue=error.message;}).finally(()=>{sampleRequest=null;});
    await sampleRequest;
  }
  async function loadDefinitions(){
    if(!configRequest) configRequest=Promise.all(['growth','operations','workspace'].map(async group=>{
      const response=await fetch('workbench-'+group+'.json');
      if(!response.ok) throw new Error('办理模板加载失败');
      return response.json();
    })).then(groups=>{
      const next=new Map();
      for(const page of groups.flat()) {
        if(next.has(page.id) || !page.sections?.length) throw new Error('办理模板配置无效');
        next.set(page.id,page);
      }
      for(const [id,page] of next) definitions.set(id,page);
      configError='';
    }).catch(err=>{configError=err.message;}).finally(()=>{configRequest=null;mount();});
    return configRequest;
  }
  function key(){return [mode,currentUser()?.id||'',view,productId,pane().section,pane().shopOnly].join('|');}
  function clearQueue(){++generation;queueKey='';rows=[];signals=[];truncated=false;nextOffset=null;issue='';busy=false;}
  function changeScope(){
    if(editor && editor.scope !== [mode,currentUser()?.id,view,productId].join('|')) closeEditor();
    ++detailRequest;
    clearQueue();
  }
  function mount(){
    if(!q('#workbenchRoot') || !['demo','server'].includes(mode)) return;
    if(!definitions.size){
      q('#workbenchRoot').innerHTML=configError ? '<p role="alert">'+escape(configError)+' '+control('重试','definitions')+'</p>' : '<p>正在读取办理模板…</p>';
      if(!configError&&!configRequest)loadDefinitions();
      return;
    }
    if(!definitions.has(view)) return;
    if(queueKey!==key()){changeScope();loadQueue();}
    else draw();
  }
  async function loadQueue(more=false){
    const request=++generation, contextKey=key();
    queueKey=contextKey;
    if(!more){rows=[];signals=[];truncated=false;nextOffset=null;}
    busy=true;issue='';draw();
    try {
      const state=pane();
      const globalLane = ['overview','team','agent'].includes(view) && !state.section;
      const params=new URLSearchParams();
      if(view==='shop'&&state.shopOnly)params.set('scope','shop');
      else if(productId!=='all')params.set('product_id',productId);
      if(!globalLane) params.set('page_id',view);
      if(state.section) params.set('section_id',state.section);
      let list=[], feed=[];
      let offset=null,hasExtra=false;
      if(mode==='demo'){
        await loadDemoSamples();
        list=demoItems().filter(item=>(view==='shop'&&state.shopOnly?shopScope(item):productId==='all'||item.product_id===productId) && (globalLane||item.page_id===view) && (!state.section||item.section_id===state.section));
      }else if(currentUser()){
        if((view==='overview'||view==='agent') && !state.section && !more){
          if(state.mine)params.set('mine','true');
          const inbox=await api('/api/workbench/inbox?'+params);
          list=inbox.items||[];feed=inbox.signals||[];hasExtra=Boolean(inbox.truncated);
        }else{
          params.set('limit','100');params.set('offset',String(more ? nextOffset||0 : 0));
          const result=await api('/api/workbench/items?'+params);
          list=result.items;offset=result.has_more?result.next_offset:null;
        }
      }
      if(request!==generation || contextKey!==key())return;
      rows=more?[...rows,...list]:list;signals=feed;nextOffset=offset;truncated=hasExtra;
    }catch(error){if(request===generation)issue=error.message;}
    finally{if(request===generation){busy=false;draw();}}
  }
  function draw(){
    if(!definitions.has(view) || !q('#workbenchRoot'))return;
    const page=definitions.get(view), p=pane(), chosen=page.sections.find(s=>s.id===p.section);
    const who=currentUser(), canWrite=Boolean(who);
    const receiving=view==='inventory' && p.receiving;
    const comparison=view==='overview' && p.comparison;
    const searchTerms=view==='ads' && p.searchTerms;
    const adReview=view==='ads' && p.section==='verification' && !p.legacy && !searchTerms;
    q('#viewRoot').hidden=!p.legacy;
    q('#pageActions').hidden=!p.legacy;
    q('#pageTitle').textContent=page.title;
    q('#pageDescription').textContent=p.legacy?'':page.purpose;
    const nextActor=item=>['pending_review','executed'].includes(item.status)?item.reviewer_id:item.status==='closed'?null:item.owner_id;
    const entries=rows.filter(item=>(!p.status||item.status===p.status) &&
      (!p.mine || (p.mine==='review' ? item.reviewer_id===who?.id&&['pending_review','executed'].includes(item.status) : nextActor(item)===who?.id)) &&
      (!query||[item.title,item.site,item.object_ref,scopeName(item),personName(item.owner_id)].some(v=>String(v||'').toLowerCase().includes(query.toLowerCase()))));
    const selectOptions=Object.entries(labels).map(([v,l])=>`<option value="${v}" ${v===p.status?'selected':''}>${l}</option>`).join('');
    const count = status => rows.filter(item=>item.status===status).length;
    const root=q('#workbenchRoot');
    let content=root.querySelector('.wb-content');
    if(!content){content=document.createElement('div');content.className='wb-content';root.replaceChildren(content);}
    content.innerHTML=`${view==='overview'&&!p.legacy&&!p.section&&!comparison?renderTodayDashboard():''}${previousQueue ? control('返回来源队列','back'):''}<div class="wb-nav">
      <button class="wb-tab ${!p.legacy&&!p.section&&!receiving&&!comparison&&!searchTerms?'selected':''}" data-wb-section="">${['overview','team','agent'].includes(view)?'跨模块队列':'办理队列'}</button>
      ${view==='overview'?'<button class="wb-tab" data-erp-queue-open>ERP 待办</button><button class="wb-tab" data-erp-shop-sales-open>多店经营</button>':''}
      ${view==='inventory'?'<button class="wb-tab" data-erp-procurement-open>采购申请</button>':''}
      ${view==='finance'?'<button class="wb-tab" data-erp-payables-open>供应商应付</button><button class="wb-tab" data-erp-settlements-open>店铺结算对账</button>':''}
      ${view==='service'?'<button class="wb-tab" data-erp-fulfillment-open>自发货履约</button><button class="wb-tab" data-erp-refunds-open>退款核对台账</button>':''}
      ${view==='overview'?`<button class="wb-tab ${comparison?'selected':''}" data-wb="comparison">经营环比</button>`:''}
      ${view==='ads'?`<button class="wb-tab ${searchTerms?'selected':''}" data-wb="searchTerms">搜索词汇总</button>`:''}
      ${view==='inventory'?`<button class="wb-tab" data-erp-stock-open>库存余额与调拨</button><button class="wb-tab ${receiving?'selected':''}" data-wb="receiving">仓库收货</button>`:''}
      ${page.sections.map(s=>`<button class="wb-tab ${!p.legacy&&!receiving&&!comparison&&!searchTerms&&p.section===s.id?'selected':''}" data-wb-section="${escape(s.id)}">${escape(s.title)}</button>`).join('')}
      <button class="wb-tab ${p.legacy?'selected':''}" data-wb="legacy">${escape(legacyNames[view]||'指标与 SOP')}</button></div>
      ${p.legacy||receiving||comparison||searchTerms?'':`<section class="wb-queue panel">
      <header><div><h2>${escape(chosen?.title||(view==='overview'?'跨模块办理队列':view==='team'?'今日工作':'业务办理队列'))}</h2>
      <p>${chosen?escape(chosen.purpose):`待审核 ${count('pending_review')}　待执行 ${count('approved')}　待复核 ${count('executed')}`}</p></div>
      ${control(chosen?.action||'新建业务单','new',true,canWrite?'':'disabled')}</header>
      <div class="wb-toolbar">${view==='shop'?`<label>单据范围<select data-wb-shop-filter><option value="">当前产品范围</option><option value="shop" ${p.shopOnly?'selected':''}>仅店铺级记录</option></select></label>`:''}<label>进度<select data-wb-filter="status"><option value="">全部进度</option>${selectOptions}</select></label>
      <label>经办范围<select data-wb-filter="mine"><option value="">工作空间</option><option value="mine" ${p.mine==='mine'?'selected':''}>待我办理</option><option value="review" ${p.mine==='review'?'selected':''}>待我审核／复核</option></select></label>
      ${mode==='demo'?`<label>演示身份<select data-wb-actor>${ws.people.map(person=>`<option value="${escape(person.id)}" ${person.id===who?.id?'selected':''}>${escape(person.display_name)}${person.id===ws.people[0].id?'（管理员）':''}</option>`).join('')}</select></label>`:''}
      ${control('刷新队列','refresh',false,busy?'disabled':'')}</div>
      ${mode==='demo'&&sampleIssue?`<p class="wb-empty">${escape(sampleIssue)}，可刷新重试。</p>`:''}
      ${issue?`<p role="alert" class="wb-empty">${escape(issue)} ${control('重试','refresh')}</p>`:!who?`<div class="wb-empty">登录后查看与办理工作。 ${control('登录','login')}</div>`:busy&&!rows.length?'<div class="wb-empty">正在读取工作队列…</div>':
      entries.length?`<div class="wb-table-wrap"><table class="wb-table"><thead><tr><th>事项／范围</th><th>办理环节</th><th>负责人</th><th>状态</th><th>处理依据</th></tr></thead><tbody>${entries.map(item=>{
        const source=definitions.get(item.page_id),sec=source?.sections.find(s=>s.id===item.section_id);
        return `<tr data-wb-status="${escape(item.status)}"><td><button class="wb-link" data-wb-id="${escape(item.id)}">${escape(item.title)}</button><small>${escape([item.sample?'本机示例':'',scopeName(item),item.site,item.object_ref].filter(Boolean).join(' / '))}</small></td>
        <td>${escape(sec?.title||source?.title||'—')}</td><td>${escape(personName(item.owner_id))}</td><td><span class="wb-state">${escape(labels[item.status]||item.status)}</span></td><td>${escape(item.reason||item.due_date||'—')}</td></tr>`;
      }).join('')}</tbody></table></div>`:
      `<p class="wb-empty">${p.status||p.mine||query?'当前筛选下没有记录。':mode==='demo'?'演示队列尚无办理记录，可新建业务单试办。':'已查询当前范围，尚无办理记录。'}</p>`}
      ${truncated?'<p class="wb-empty">当前按优先级显示前 100 项。进入团队与审批的跨模块队列可分页查看全部。 '+control('查看全部工作单','all-records')+'</p>':''}
      ${nextOffset!==null?control('加载更多','more',false,busy?'disabled':''):''}
      </section>
      ${signals.length?`<section class="panel wb-queue"><header><h2>待核实的经营线索</h2></header>${signals.map((signal,i)=>`<button class="wb-signal" data-wb-signal="${i}"><b>${escape(signal.title)}</b><span>${escape(signal.reason)}</span></button>`).join('')}</section>`:''}
      <details class="wb-guide"><summary>办理口径与岗位分工</summary><p>${escape(page.roles.join(' / '))}　${escape(page.scope)}</p>${(page.rules||[]).map(rule=>`<p><b>${escape(rule.name)}</b>　${escape(rule.logic)}　${escape(rule.missing)}</p>`).join('')}</details>`}`;
    let receivingRoot=root.querySelector('#erpReceivingRoot');
    if(receiving){
      if(!receivingRoot){receivingRoot=document.createElement('div');receivingRoot.id='erpReceivingRoot';root.append(receivingRoot);}
      window.erpReceiving?.mount(receivingRoot,{mode,user:who,api,login:()=>window.processUI?.login()});
    }else if(receivingRoot){
      window.erpReceiving?.unmount();receivingRoot.remove();
    }
    let comparisonRoot=root.querySelector('#erpComparisonRoot');
    if(comparison){
      if(!comparisonRoot){comparisonRoot=document.createElement('div');comparisonRoot.id='erpComparisonRoot';root.append(comparisonRoot);}
      window.erpComparison?.mount(comparisonRoot,{mode,product:ws.products.find(x=>x.id===productId),user:who,api,login:()=>window.processUI?.login()});
    }else if(comparisonRoot){
      window.erpComparison?.unmount();comparisonRoot.remove();
    }
    let searchRoot=root.querySelector('#erpSearchTermsRoot');
    if(searchTerms){
      if(!searchRoot){searchRoot=document.createElement('div');searchRoot.id='erpSearchTermsRoot';root.append(searchRoot);}
      window.erpSearchTerms?.mount(searchRoot,{mode,product:ws.products.find(x=>x.id===productId),user:who,api,login:()=>window.processUI?.login()});
    }else if(searchRoot){
      window.erpSearchTerms?.unmount();searchRoot.remove();
    }
    let reviewRoot=root.querySelector('#adsReviewRoot');
    if(adReview){
      if(!reviewRoot){reviewRoot=document.createElement('div');reviewRoot.id='adsReviewRoot';root.append(reviewRoot);}
      window.adsDecisionReview?.mount(reviewRoot,{mode,product:ws.products.find(x=>x.id===productId),user:who,api,
        personName,login:()=>window.processUI?.login(),openItem:openRow,createChild:openSignal});
    }else if(reviewRoot){
      window.adsDecisionReview?.unmount(true);reviewRoot.remove();
    }
    const triageScope=[mode,currentUser()?.id,productId].join('|');
    let triage=root.querySelector('#wb-routing');
    if(!p.legacy && !comparison && !p.section && ['overview','agent'].includes(view)){
      if(!triage || triage.dataset.scope!==triageScope){
        triage?.remove();triage=document.createElement('div');triage.id='wb-routing';triage.dataset.scope=triageScope;root.append(triage);
      }
      window.workbenchTriage?.mount();
    }else triage?.remove();
  }
  function blank(section,seed={}){
    const product=productId==='all'?'':ws.products.find(p=>p.id===productId);
    return {id:null,request_id:uuid(),version:0,status:'draft',page_id:view,section_id:section.id,
      product_id:product?.id||'',site:product?.marketplace||'',object_ref:'',title:'',values:{},
      owner_id:currentUser()?.id,reviewer_id:null,task_id:null,history:[],...seed};
  }
  function draftKey(item) {
    return ['homa.workbench.draft.1',mode,currentUser()?.id,item.page_id,item.section_id,item.id||'new',item.id?'':productId].join('/');
  }
  function openEditor(item){
    closeEditor();const serial=++openSerial;
    const source=[item.product_id,item.parent_id,item.signal_key,item.object_ref,item.source_note];
    const routed=!item.id && (item.parent_id || item.signal_key || item.source_note);
    const localKey=draftKey(item)+(routed?'/'+JSON.stringify(source):'');
    const writing=activeWrites.get(localKey);
    if(writing?.saving){
      const scope=key();
      writing.saving.catch(()=>{}).then(()=>{if(serial===openSerial&&scope===key())openEditor(writing.item.id?writing.item:item);});
      return;
    }
    let saved,archive,warning='';
    try{saved=getStored(localKey,null);archive=getStored(localKey+'/prior',null);}catch{warning='本机草稿读取失败。';}
    const owned=currentUser()?.id===item.owner_id;
    const editable=item.status==='draft' && (owned||admin());
    const compatible=saved?.version===item.version && (item.id?saved.id===item.id:true);
    if(saved&&!compatible)warning='服务器已有新版本。旧草稿保留在本机，本次显示最新记录。';
    editor={item,form:compatible?{...structuredClone(item),...saved.form}:structuredClone(item),
      key:localKey,scope:[mode,currentUser()?.id,view,productId].join('|'),editable,owned,working:false,warning,
      shopScopes:null,note:compatible?saved.note||'':'',evidence:compatible?saved.evidence||'':'',result:null,
      conflict:saved&&!compatible?saved:null,archive,memberList:null,timer:null,saving:null,
      pending:compatible?saved.pending:null,actionReplay:compatible?saved.actionReplay:null,actor:currentUser()?.id,isAdmin:admin(),
      prefix:['homa.workbench.draft.1',mode,currentUser()?.id,item.page_id,item.section_id,''].join('/')};
    if(!item.id&&compatible)editor.item.request_id=editor.form.request_id;
    const dialog=document.createElement('dialog');dialog.id='wbDialog';dialog.className='wb-dialog';dialog.setAttribute('aria-labelledby','wbDialogTitle');
    document.body.append(dialog);
    dialog.addEventListener('close',()=>{if(q('#wbDialog')===dialog){closeEditor();}});
    dialog.addEventListener('cancel',event=>{if(editor?.working)event.preventDefault();});
    drawEditor();dialog.showModal();
    if(compatible && serverReady(editor))schedule(editor);
    else if(mode==='server'&&item.id&&!editor.conflict&&!warning)saveState(editor,'saved','业务草稿已存服务器 · 版本 '+item.version);
    if(mode==='server' && editable && admin())loadReviewers(editor);
    if(mode==='server' && !item.id && item.page_id==='shop' && !item.parent_id)loadShopScopes(editor);
  }
  async function loadShopScopes(tx){
    try{
      const result=await api('/api/workbench/shop-scopes');
      if(editor!==tx)return;
      tx.shopScopes=result.scopes;
      const select=q('#wbItemForm [name=shop_connection]');
      if(select && !tx.working)select.replaceChildren(...shopOptions(tx));
    }catch{if(editor===tx)message('未能读取已有店铺，可手工填写账号范围。');}
  }
  function shopOptions(tx){
    const options=[new Option('手工填写店铺范围','')];
    for(const [n,scope] of (tx.shopScopes||[]).entries()){
      const selected=scope.account_ref===tx.form.account_ref && scope.site===tx.form.site && scope.business_timezone===tx.form.business_timezone;
      options.push(new Option(scope.account_ref+' / '+scope.site,String(n),false,selected));
    }
    return options;
  }
  async function loadReviewers(tx){
    try {
      const people=await api('/api/workflow-auth/members');
      if(editor!==tx)return;
      tx.memberList=people.filter(p=>p.id!==tx.item.owner_id);
      const select=q('#wbItemForm [name=reviewer_id]');
      const selected=tx.form.reviewer_id;
      select.replaceChildren(new Option('待管理员分配',''),...tx.memberList.map(p=>new Option(p.display_name,p.id)));
      select.value=selected||'';
      select.disabled=tx.working||Boolean(tx.conflict);
    }catch(err){if(editor===tx)message('可用审核人读取失败。'+err.message);}
  }
  function closeEditor(){
    ++openSerial;
    const tx=editor,dialog=q('#wbDialog');editor=null;
    if(dialog){if(dialog.open)dialog.close();dialog.remove();}
    if(tx?.timer && !tx.working){flushDraft(tx).catch(()=>{});}

  }
  function businessField(spec,item,disabled){
    const attr=`name="${escape(spec.key)}" data-wb-field="${escape(spec.key)}" ${spec.required?'aria-required="true"':''} ${disabled?'disabled':''}`;
    const value=item.values[spec.key]??'';
    const options=(spec.options||[]).map(v=>`<option ${v===value?'selected':''}>${escape(v)}</option>`).join('');
    let input;
    if(spec.type==='textarea')input=`<textarea ${attr}>${escape(value)}</textarea>`;
    else if(spec.type==='select')input=`<select ${attr}><option value="">请选择</option>${options}</select>`;
    else input=`<input ${attr} type="${escape(spec.type)}" value="${escape(value)}" ${spec.type==='number'?'step="any"':''} ${spec.min!=null?'min="'+Number(spec.min)+'"':''} ${spec.max!=null?'max="'+Number(spec.max)+'"':''}>`;
    return `<label class="field">${escape(spec.label)}${spec.required?' *':''}${input}<small>${escape(spec.help||'')}</small></label>`;
  }
  function allowed(action,record){
    const user=currentUser()?.id, owner=record.owner_id===user, reviewer=record.reviewer_id===user && !owner;
    return (action==='submit' && record.status==='draft'&&owner) ||
      (['approve','return'].includes(action)&&record.status==='pending_review'&&reviewer) ||
      (action==='execute'&&record.status==='approved'&&owner) ||
      (action==='verify'&&record.status==='executed'&&reviewer);
  }
  function drawEditor(){
    if(!editor)return;
    const {item,form,editable,owned,warning,note,evidence}=editor;
    const page=definitions.get(item.page_id);
    const section=item.definition || page.sections.find(s=>s.id===item.section_id);
    const fieldLock=!editable||!owned;
    const shop=shopScope(form), scopeLock=Boolean(item.id||form.parent_id||form.signal_key||form.source_note);
    const opt=(value,label,selected)=>`<option value="${escape(value)}" ${value===selected?'selected':''}>${escape(label)}</option>`;
    const people=editor.memberList || (mode==='demo'?ws.people.filter(person=>person.id!==item.owner_id):ws.people.filter(person=>person.id===form.reviewer_id));
    const permitted=Object.keys(actions).filter(action=>allowed(action,item));
    const dlg=q('#wbDialog');
    dlg.dataset.wbItemId=item.id||'';dlg.dataset.wbVersion=item.version;
    const refs=[item.parent_ref,...(item.child_refs||[])].filter(Boolean);
    const jumpLabel = ref => (ref.id===item.parent_id?'来源单':'后续单')+' · '+ref.title;
    const followOptions=[...definitions.values()].filter(destination=>!shop||destination.id==='shop').map(destination=>opt(destination.id,destination.title,item.page_id)).join('');
    dlg.innerHTML=`<header class="wb-dialog-head"><div><small>${escape(page.title)} / ${escape(section.title)}</small>
      <h2 id="wbDialogTitle">${escape(item.id?item.title:section.action)}</h2></div>${control('关闭','close')}</header>
      <form id="wbItemForm"><div class="wb-dialog-body">
      <div class="wb-stamp">${item.sample?'<span>本机示例</span>':''}<span class="wb-state">${escape(labels[item.status])}</span><span>经办人　${escape(personName(item.owner_id))}</span><span data-wb-version-label>${item.version?'版本 '+item.version:'新单据'}</span></div>
      ${editor.conflict?`<section class="wb-conflict"><strong>检测到本机旧版本草稿</strong><p>核对旧稿后可在最新版本继续办理。</p>
      <textarea aria-label="本机旧版本草稿" readonly>${escape(JSON.stringify(editor.conflict,null,2))}</textarea>
      ${control('复制旧稿','copy-old')} ${control('保留旧稿并使用最新版本','use-latest')}</section>`:''}
      ${editor.archive&&!editor.conflict?`<details class="wb-conflict"><summary>查看上次保留的本机旧稿</summary>
      <textarea readonly aria-label="上次保留的本机旧稿">${escape(JSON.stringify(editor.archive,null,2))}</textarea>${control('复制旧稿','copy-old')}</details>`:''}
      ${form.source_note?`<details class="wb-source" open><summary>来源原文</summary><p data-wb-source>${escape(form.source_note)}</p></details>`:''}
      <div class="wb-form-grid">
      ${page.id==='shop'?`<label class="field">办理范围<select name="record_scope" ${scopeLock?'disabled':''}>${opt('product','按产品办理',shop?'shop':'product')}${opt('shop','按店铺办理',shop?'shop':'product')}</select></label>`:''}
      ${shop?`<label class="field">已有店铺连接<select name="shop_connection" ${scopeLock?'disabled':''}></select></label>
      <label class="field">店铺／Seller ID *<input name="account_ref" maxlength="120" value="${escape(form.account_ref||'')}" ${scopeLock?'disabled':''}></label>
      <label class="field">经营时区 *<input name="business_timezone" placeholder="America/Los_Angeles" value="${escape(form.business_timezone||'')}" ${scopeLock?'disabled':''}></label>`:
      `<label class="field">所属型号 *<select name="product_id" ${item.id||productId!=='all'?'disabled':''}><option value="">请选择产品型号</option>${ws.products.map(p=>opt(p.id,p.model,form.product_id)).join('')}</select></label>`}
      <label class="field">业务环节<select name="section_id" ${item.id||form.signal_key||form.source_note||form.parent_id?'disabled':''}>${page.sections.map(s=>opt(s.id,s.title,section.id)).join('')}</select></label>
      <label class="field wb-wide">事项标题 *<input name="title" maxlength="240" value="${escape(form.title)}" ${fieldLock?'disabled':''}></label>
      <label class="field">站点 *<input name="site" value="${escape(form.site)}" ${fieldLock||(shop&&scopeLock)?'disabled':''}></label>
      <label class="field">业务对象 *<input name="object_ref" placeholder="SKU、批次或业务编号" value="${escape(form.object_ref)}" ${fieldLock||form.signal_key?'disabled':''}></label>
      <label class="field">指定审核人<select name="reviewer_id" ${editable&&admin()&&(mode==='demo'||editor.memberList)?'':'disabled'}><option value="">待管理员分配</option>${people.map(p=>opt(p.id,p.display_name,form.reviewer_id)).join('')}</select></label>
      ${shop?'':`<label class="field">关联现有 SOP<select name="task_id" ${fieldLock?'disabled':''}><option value="">无</option>${ws.tasks.filter(t=>t.product_id===form.product_id).map(t=>opt(t.id,t.title,form.task_id)).join('')}</select></label>`}
      </div><h3>${escape(section.title)}</h3><div class="wb-form-grid">${section.fields.map(f=>businessField(f,form,fieldLock)).join('')}</div>
      <p class="wb-context">${escape(section.output)}</p>
      ${item.task_id?control('查看关联 SOP','task'):''}
      ${item.id || permitted.length ? `<div class="wb-form-grid"><label class="field">办理意见／复核结论<textarea name="note">${escape(note)}</textarea></label>
      <label class="field">执行回执／可复核凭证（登记执行时入库）<textarea name="${section.fields.some(f=>f.key==='evidence')?'execution_evidence':'evidence'}" data-wb-action-evidence>${escape(evidence)}</textarea></label></div>`:''}
      ${refs.length ? `<div class="wb-links">${refs.map(ref=>control(jumpLabel(ref),'linked',false,'data-ref="'+escape(ref.id)+'"')).join('')}</div>` : ''}
      ${item.parent_id&&!item.id?'<p>保存后，此单将关联来源单据。</p>':''}
      ${item.id ? `<details class="wb-follow"><summary>安排后续工作</summary><div class="wb-form-grid"><label class="field">目标工作台<select name="follow_page">${followOptions}</select></label>
        <label class="field">后续环节<select name="follow_section">${page.sections.map(s=>opt(s.id,s.title,'')).join('')}</select></label>
        <label class="field">后续办理对象<input name="follow_object" value="${escape(item.object_ref)}"></label></div>${control('建立关联草稿','follow')}</details>` : ''}
      <section class="wb-assist">${control('核算与办理建议','assist')} ${mode==='server'?control('请 AI 起草','model'):''}<div id="wbAssistResult" aria-live="polite"></div></section>
      ${item.history?.length?`<details class="wb-history"><summary>办理记录（${item.history.length}）</summary>${item.history.map(h=>`<p><b>${escape(actions[h.action]||({create:'新建',created:'新建',update:'修改',updated:'修改'}[h.action])||h.action)}</b>　${escape(h.actor_name||personName(h.actor_id))}　${escape(dt(h.created_at))}<br>${escape([h.note,h.evidence].filter(Boolean).join(' / '))}</p>`).join('')}</details>`:''}
      </div><footer><span id="wbMessage" role="status">${escape(warning||'')}</span>
      ${control('读取最新版本','latest',false,editor.refreshRequired?'':'hidden')}
      ${editable?control('保存业务草稿','save',!item.id):''}
      ${permitted.map(action=>control(actions[action],action,action!=='return')).join('')}
      </footer></form>`;
    if(shop)dlg.querySelector('[name=shop_connection]').replaceChildren(...shopOptions(editor));
    if(editor.conflict)dlg.querySelectorAll('input,select,textarea:not([readonly]),button[data-wb]:not([data-wb=close]):not([data-wb=copy-old]):not([data-wb=use-latest])').forEach(el=>el.disabled=true);
    if(editor.saveState&&!editor.warning)saveState(editor,editor.saveState,editor.saveText);
    showAssist();
    dlg.querySelector('form').onsubmit=ev=>ev.preventDefault();
  }
  function collect(){
    if(!editor)return;
    const form=q('#wbItemForm');if(!form)return;
    const draft=editor.form;
    for(const key of ['product_id','title','site','object_ref','reviewer_id','task_id','account_ref','business_timezone']){
      const el=form.elements.namedItem(key);
      if(el)draft[key]=el.value||(['task_id','reviewer_id'].includes(key)?null:'');
    }
    for(const el of form.querySelectorAll('[data-wb-field]')){
      if(el.value==='')delete draft.values[el.dataset.wbField];
      else draft.values[el.dataset.wbField]=el.type==='number'?Number(el.value):el.value;
    }
    editor.note=form.elements.namedItem('note')?.value||'';
    editor.evidence=form.querySelector('[data-wb-action-evidence]')?.value||'';
  }
  function message(text){const place=q('#wbMessage');if(place)place.textContent=text;}
  function saveState(tx,state,text){
    tx.saveState=state;tx.saveText=text;
    if(editor!==tx)return;
    const label=q('#wbMessage');
    if(label){label.dataset.saveState=state;label.textContent=text;}
    const reload=q('#wbDialog [data-wb=latest]');if(reload)reload.hidden=!tx.refreshRequired;
  }
  function protect(tx){
    try{
      localStorage.setItem(tx.key,JSON.stringify({id:tx.item.id,version:tx.item.version,form:tx.form,
        note:tx.note,evidence:tx.evidence,pending:tx.pending,actionReplay:tx.actionReplay}));
      tx.localOK=true;return true;
    }catch{tx.localOK=false;return false;}
  }
  function serverReady(tx){
    if(mode!=='server'||currentUser()?.id!==tx.actor||!tx.editable||!tx.owned||tx.conflict||tx.refreshRequired)return false;
    if(tx.pending)return true;
    try{validate(tx.form);}catch{return false;}
    return Boolean(tx.item.id || (tx.form.object_ref?.trim() && tx.form.site?.trim()));
  }
  function schedule(tx){
    clearTimeout(tx.timer);
    if(serverReady(tx) && !tx.working){
      saveState(tx,'pending',tx.localOK===false?'本机存储不可用，等待写入服务器':'本机草稿已保护，等待写入服务器');
      tx.timer=setTimeout(()=>{tx.timer=null;flushDraft(tx).catch(()=>{});},900);
    }
  }
  function autosave(){
    if(!editor || editor.working || editor.conflict)return;
    collect();const tx=editor;
    const local=protect(tx);
    if(tx.refreshRequired)return;
    if(tx.saving)return;
    if(!local)saveState(tx,'error','本机草稿未能保存，请保留窗口。');
    else saveState(tx,'local','草稿已保存在此设备');
    schedule(tx);
  }
  function captureOperation(tx){
    const item=tx.item, form=structuredClone(tx.form);
    const body=item.id?editPayload(form,item):createPayload(form);
    if(item.id && !Object.keys(body).length)return null;
    if(item.id)Object.assign(body,{expected_version:item.version,request_id:uuid()});
    return {url:'/api/workbench/items'+(item.id?'/'+item.id:''),method:item.id?'PATCH':'POST',body,form};
  }
  function lockRecordScope(tx){
    if(editor!==tx)return;
    const dlg=q('#wbDialog');if(!dlg)return;
    for(const name of ['product_id','section_id','record_scope','account_ref','business_timezone','shop_connection',...(shopScope(tx.form)?['site']:[])]){
      const el=dlg.querySelector('[name="'+name+'"]');if(el)el.disabled=true;
    }
  }
  function updateRecord(tx,saved,op){
    const oldKey=tx.key;
    const latest=tx.form;
    tx.item=saved;tx.pending=null;
    tx.form={...structuredClone(saved),request_id:latest.request_id,record_scope:latest.record_scope};
    for(const k of ['title','object_ref','site','values','reviewer_id','task_id']){
      if(JSON.stringify(latest[k])!==JSON.stringify(op.form[k]))tx.form[k]=latest[k];
    }
    tx.owned=tx.actor===saved.owner_id;
    tx.editable=saved.status==='draft' && (tx.owned||tx.isAdmin);
    tx.key=tx.prefix+saved.id+'/';
    if(protect(tx) && oldKey!==tx.key){
      try{const old=getStored(oldKey,null);if(old?.form?.request_id===op.form.request_id)localStorage.removeItem(oldKey);}catch{}
    }
    if(editor===tx){
      const dlg=q('#wbDialog');
      dlg.dataset.wbItemId=saved.id;dlg.dataset.wbVersion=saved.version;
      q('#wbDialogTitle').textContent=saved.title;
      q('#wbDialog [data-wb-version-label]').textContent='版本 '+saved.version;
      lockRecordScope(tx);
    }
  }
  async function flushDraft(tx,manual=false){
    clearTimeout(tx.timer);tx.timer=null;
    if(tx.saving){await tx.saving;if(manual)return flushDraft(tx,true);return tx.item;}
    if(tx.refreshRequired)throw new Error('请先核对服务器最新版本。');
    if(mode!=='server'||currentUser()?.id!==tx.actor||!tx.editable||(!manual&&!serverReady(tx)))return tx.item;
    if(manual)validate(tx.form);
    const op=tx.pending||captureOperation(tx);
    if(!op){
      if(tx.item.id)saveState(tx,'saved','业务草稿已存服务器 · 版本 '+tx.item.version);
      return tx.item;
    }
    tx.pending=op;protect(tx);lockRecordScope(tx);
    saveState(tx,'saving','正在保存到服务器…');
    const writingKey=tx.key;
    activeWrites.set(writingKey,tx);
    tx.saving=(async()=>{
      try{
        const saved=await api(op.url,{method:op.method,body:JSON.stringify(op.body)});
        const othersChanged=saved.status!=='draft'||saved.owner_id!==op.form.owner_id||
          (op.method==='PATCH' && saved.version!==op.body.expected_version+1)||
          (op.method==='POST' && (saved.version>1 || Object.keys(editPayload(op.form,saved)).length>0));
        if(othersChanged){
          tx.pending=null;tx.refreshRequired=true;
          if(!tx.item.id){tx.item={...tx.item,id:saved.id};const before=tx.key;tx.key=tx.prefix+saved.id+'/';if(protect(tx))localStorage.removeItem(before);}
          const conflict=new Error('相同来源或服务器已有新版本。请核对双方草稿。');conflict.status=409;throw conflict;
        }
        updateRecord(tx,saved,op);
        saveState(tx,'saved','业务草稿已存服务器 · 版本 '+saved.version);
        if(mode==='server' && currentUser()?.id===tx.actor)loadQueue();
        return saved;
      }catch(error){
        if(error.status && error.status<500 && error.status!==408 && error.status!==429)tx.pending=null;
        if(error.status===409)tx.refreshRequired=true;
        const local=protect(tx);
        saveState(tx,tx.refreshRequired?'conflict':'error',(local?'本机草稿已保留。':'本机保存失败。')+error.message);
        throw error;
      }finally{
        activeWrites.delete(writingKey);tx.saving=null;
      }
    })();
    const saved=await tx.saving;
    if(serverReady(tx)&&Object.keys(editPayload(tx.form,saved)).length){
      if(manual||editor!==tx)return flushDraft(tx,manual);
      schedule(tx);
    }
    return saved;
  }
  function templateChecks(section,values){
    const findings=[];
    for(const check of section.checks||[]){
      const [left,right]=check.keys.map(key=>values[key]);
      let blocked=false;
      if(check.op==='required_if')blocked=left===check.when && (right==null||String(right).trim()==='');
      else if(check.op==='block_if')blocked=left===check.when;
      else if(left!=null && right!=null && left!=='' && right!==''){
        if(check.op==='lte')blocked=left>right;
        if(check.op==='gte')blocked=left<right;
        if(check.op==='eq')blocked=left!==right;
        if(check.op==='neq')blocked=left===right;
      }
      if(blocked)findings.push({label:check.label,detail:check.message});
    }
    return findings;
  }
  function validate(form,submit=false){
    if(shopScope(form)){
      if(form.site && !/^[a-z]{2,4}$/i.test(form.site.trim()))throw new Error('店铺站点需填写 2 至 4 位站点代码。');
      if(!form.account_ref?.trim()||!form.site?.trim()||!form.business_timezone?.trim())throw new Error('请填写店铺、站点与经营时区。');
      try{new Intl.DateTimeFormat('en',{timeZone:form.business_timezone});}catch{throw new Error('请填写有效的 IANA 经营时区。');}
    }else if(!form.product_id)throw new Error('请选择所属型号。');
    if(!form.title.trim())throw new Error('请填写事项标题。');
    if(!submit)return;
    if(!form.site.trim()||!form.object_ref.trim()||!form.reviewer_id)throw new Error('提交需要站点、业务对象及由管理员指定的独立审核人。');
    const section=form.definition || definitions.get(form.page_id).sections.find(s=>s.id===form.section_id);
    for(const field of section.fields){
      const value=form.values[field.key];
      if(field.required && (value==null||!String(value).trim()))throw new Error('请填写'+field.label+'。');
      if(value!=null&&field.type==='number' && (!Number.isFinite(value)||field.min!=null&&value<field.min||field.max!=null&&value>field.max))throw new Error(field.label+'超出范围。');
      if(value!=null && field.type==='url'){
        let valid=false;try{valid=['http:','https:'].includes(new URL(value).protocol);}catch{}
        if(!valid)throw new Error(field.label+'需要有效的网址。');
      }
    }
    const blocker=templateChecks(section,form.values)[0];
    if(blocker)throw new Error(blocker.detail);
  }
  function localWrite(item,form,action,note,evidence){
    const list=demoItems(),index=list.findIndex(i=>item.id?i.id===item.id:i.request_id===item.request_id);
    let record=list[index];
    if(record && item.id && record.version!==item.version)throw new Error('此业务单已更新，请关闭详情并重新打开。');
    if(!item.id&&record)return record;
    if(!record && (form.signal_key||form.parent_id)){
      const found=list.find(other=>other.product_id===form.product_id && other.status!=='closed' && other.account_ref===form.account_ref && other.site===form.site &&
        other.page_id===form.page_id && other.section_id===form.section_id && other.object_ref===form.object_ref && (form.signal_key?other.signal_key===form.signal_key:other.parent_id===form.parent_id));
      if(found)return found;
    }
    if(!record)record={...structuredClone(form),id:uuid(),version:0,history:[],definition:structuredClone(definitions.get(form.page_id).sections.find(s=>s.id===form.section_id))};
    if(action==='save')Object.assign(record,form,{id:record.id});
    else {
      if(!allowed(action,record))throw new Error('该身份不能办理当前环节。');
      if(action==='submit')validate(record,true);
      if((action==='return'||action==='verify')&&!note.trim())throw new Error('请填写退回原因或复核结论。');
      if(action==='execute'&&!evidence.trim())throw new Error('请登记执行回执。');
      record.status=stages[action];
    }
    record.version++;record.history=[...(record.history||[]),{action:action==='save'?'update':action,actor_id:currentUser().id,note,evidence,created_at:new Date().toISOString()}];
    const saved=getStored(demoStore,[]),savedIndex=saved.findIndex(other=>other.id===record.id);
    if(savedIndex===-1)saved.unshift(record);else saved[savedIndex]=record;
    localStorage.setItem(demoStore,JSON.stringify(saved));return record;
  }
  function editPayload(form,item){
    if(item.owner_id!==currentUser()?.id)return admin() && form.reviewer_id!==item.reviewer_id?{reviewer_id:form.reviewer_id}:{};
    const body={};
    for(const k of ['title','site','object_ref','values','task_id']){
      const value=typeof form[k]==='string'?(k==='site'?form[k].trim().toUpperCase():form[k].trim()):form[k];
      const previous=item[k];
      const equal=k==='values'?Object.keys(value).length===Object.keys(previous||{}).length &&
        Object.entries(value).every(([key,entry])=>entry===previous?.[key]):value===previous;
      if(!equal)body[k]=value;
    }
    if(admin() && form.reviewer_id!==item.reviewer_id)body.reviewer_id=form.reviewer_id;
    return body;
  }
  function createPayload(form){
    const body={};
    for(const k of ['product_id','page_id','section_id','title','site','object_ref','values','task_id','request_id'])body[k]=form[k];
    if(admin() && form.reviewer_id)body.reviewer_id=form.reviewer_id;
    if(form.signal_key)body.signal_key=form.signal_key;
    if(form.parent_id)body.parent_id=form.parent_id;
    if(form.source_note)body.source_note=form.source_note;
    if(shopScope(form)){body.product_id=null;body.account_ref=form.account_ref;body.business_timezone=form.business_timezone;
      if(form.connection_id)body.connection_id=form.connection_id;}
    return body;
  }
  async function act(action){
    if(!editor || editor.working || editor.conflict)return;
    collect();const tx=editor;const {form,note,evidence}=tx;
    tx.working=true;clearTimeout(tx.timer);tx.timer=null;let item=tx.item;
    q('#wbDialog').querySelectorAll('button,input,select,textarea').forEach(el=>el.disabled=true);
    try{
      if(action==='submit')validate(form,true);
      if(action==='save'||action==='submit'){
        validate(form);
        const persisted=mode==='demo'?localWrite(item,form,'save','',''):await flushDraft(tx,true);
        if(editor!==tx)return;
        item=persisted;tx.item=item;tx.form={...structuredClone(item),request_id:form.request_id};
        if(action==='submit'){
          localStorage.removeItem(tx.key);
          tx.key=draftKey(item);
        }
      }
      if(action!=='save'){
        if(!allowed(action,item))throw new Error('请使用当前环节指定人员的身份操作。');
        if(mode==='demo')item=localWrite(item,form,action,note,evidence);
        else{
          if(tx.actionReplay&&tx.actionReplay.action!==action)throw new Error('上一次办理结果尚未确认，请先重试原操作或重新读取服务器单据。');
          tx.actionReplay=tx.actionReplay||{expected_version:item.version,action,note,evidence,request_id:uuid()};
          protect(tx);
          try{
            item=await api('/api/workbench/items/'+item.id+'/actions',{method:'POST',body:JSON.stringify(tx.actionReplay)});
            tx.actionReplay=null;
          }catch(error){
            if(error.status&&error.status<500&&error.status!==408&&error.status!==429){tx.actionReplay=null;protect(tx);}
            throw error;
          }
        }
      }
      if(editor!==tx)return;
      localStorage.removeItem(tx.key);
      if(view==='shop' && shopScope(item))pane().shopOnly=true;
      openEditor(item);
      if(action==='save'&&mode==='server')saveState(editor,'saved','业务草稿已写入服务器 · 版本 '+item.version);
      else message(action==='save'?'已保存演示业务单':'已记录'+actions[action]);
      loadQueue();
    }catch(err){
      if(editor===tx){tx.working=false;tx.refreshRequired=tx.refreshRequired||err.status===409;tx.warning=err.message;drawEditor();if(tx.refreshRequired)saveState(tx,'conflict','本机草稿已保留。'+err.message);}
    }
  }
  function showAssist(){
    const output=q('#wbAssistResult');if(!output||!editor?.result)return;
    const r=editor.result;
    output.innerHTML=`<b>${escape(r.summary)}</b><p>${escape(r.suggestion)}</p>
      ${(r.findings||[]).map(f=>`<p><strong>${escape(f.label)}</strong>　${escape(f.detail)}</p>`).join('')}
      ${r.missing?.length?`<p>待补充：${escape(r.missing.join('、'))}</p>`:''}
      ${r.metrics?.length?`<p>${escape(r.metric_scope)}　${escape(r.period?.start_date)} 至 ${escape(r.period?.end_date)}</p><ul>${r.metrics.map(m=>`<li>${escape(m.label)}　${escape(m.value??'缺少数据')}　${escape(m.unit)}　${escape(m.reason||m.formula)}</li>`).join('')}</ul>`:''}
      ${r.model_draft?`<p><b>模型草稿</b><br>${escape(r.model_draft)}</p>`:r.model_status==='not_configured'?'<p>外部模型未配置，以上为规则结果。</p>':r.model_status==='unavailable'?'<p>模型暂时不可用，以上为规则结果。</p>':''}
      <small>${escape(r.source)} · 需人工确认</small>`;
  }
  async function assist(useModel=false){
    if(!editor || editor.working)return;collect();const tx=editor,{form}=tx;
    const shop=shopScope(form);
    if(!form.product_id && !shop)return message('请先选择具体型号。');
    if(shop && (!form.account_ref?.trim()||!form.business_timezone?.trim()||!form.site?.trim()))return message('请先填写店铺、站点和经营时区。');
    const section=form.definition || definitions.get(form.page_id).sections.find(s=>s.id===form.section_id);
    const sameProduct=form.product_id===productId;
    const period=sameProduct&&q('#viewRoot .an-container')?window.analyticsUI?.getPeriod()||{}:{};
    const snapshot=JSON.stringify(form), serial=tx.assistSerial=(tx.assistSerial||0)+1;
    q('#wbAssistResult').textContent='正在读取办理依据…';
    try{
      const result=mode==='demo' ? {summary:'本机演示规则检查',suggestion:section.output,
        findings:[{label:'办理依据',detail:section.source},...templateChecks(section,form.values)],missing:section.fields.filter(f=>f.required&&(form.values[f.key]==null||form.values[f.key]==='')).map(f=>f.label),source:'本机表单校验。业务专用算法、报表指标和外部模型需连接服务器'} :
        await api('/api/workbench/assist',{method:'POST',body:JSON.stringify({product_id:form.product_id||null,...(shop?{account_ref:form.account_ref,business_timezone:form.business_timezone}:{}),page_id:form.page_id,section_id:form.section_id,values:form.values,site:form.site,object_ref:form.object_ref,...period,use_model:useModel})});
      if(editor!==tx||tx.assistSerial!==serial)return;
      collect();
      if(JSON.stringify(tx.form)!==snapshot){q('#wbAssistResult').textContent='表单已更改，请重新生成建议。';return;}
      tx.result=result;showAssist();
    }catch(error){if(editor===tx&&tx.assistSerial===serial)q('#wbAssistResult').textContent=error.message;}
  }
  function rememberOrigin(){
    if(!previousQueue)previousQueue={page:view,product:productId,state:{...pane()},scroll:window.scrollY};
  }
  async function openRow(id,reference=null){
    const item=reference||rows.find(r=>r.id===id);
    if(!item)return;
    const serial=++detailRequest,context=key();
    try{
      let detail;
      if(mode==='demo'){
        const all=demoItems(),saved=all.find(i=>i.id===id);if(!saved)return;
        const ref=i=>({id:i.id,page_id:i.page_id,section_id:i.section_id,title:i.title,status:i.status,product_id:i.product_id});
        const parent=all.find(i=>i.id===saved.parent_id);
        detail={...saved,parent_ref:parent?ref(parent):null,child_refs:all.filter(i=>i.parent_id===id).map(ref)};
      }else detail=await api('/api/workbench/items/'+encodeURIComponent(id));
      if(serial!==detailRequest||context!==key())return;
      if(view!==detail.page_id || pane().section!==detail.section_id || productId!==detail.product_id && !(shopScope(detail)&&productId==='all')){
        rememberOrigin();productId=detail.product_id||'all';navigate(detail.page_id);
        Object.assign(pane(),{section:detail.section_id,legacy:false,receiving:false,comparison:false,searchTerms:false,shopOnly:shopScope(detail)});mount();
      }
      openEditor(detail);
    }catch(error){if(serial===detailRequest)toast(error.message);}
  }
  function openSignal(signal){
    if(!signal||!definitions.has(signal.page_id)) return;
    if(view!==signal.page_id || pane().section!==signal.section_id)rememberOrigin();
    if(signal.product_id)productId=signal.product_id;else if(signal.account_ref)productId='all';
    navigate(signal.page_id);
    Object.assign(pane(),{section:signal.section_id,legacy:false,receiving:false,comparison:false,searchTerms:false});mount();
    handleNew({title:signal.title,site:signal.site||'',object_ref:signal.object_ref||'',signal_key:signal.signal_key,parent_id:signal.parent_id,source_note:signal.source_note||signal.reason||'',...(signal.account_ref?{record_scope:'shop',product_id:null,account_ref:signal.account_ref,business_timezone:signal.business_timezone,connection_id:signal.connection_id}:{}),values:{...signal.values}});
  }
  function handleNew(seed={}){
    if(!currentUser())return window.processUI?.login();
    const page=definitions.get(view);
    const section=page.sections.find(s=>s.id===(seed.section_id||pane().section))||page.sections[0];
    const shopDefault=view==='shop' && pane().shopOnly && !seed.signal_key && !seed.source_note && !seed.parent_id?{record_scope:'shop',product_id:null,site:''}:{};
    openEditor(blank(section,{...shopDefault,...seed}));
  }
  document.addEventListener('click',async event=>{
    const shortcut=event.target.closest('[data-wb-desk]');
    if(shortcut && view==='overview'){
      const pageId=shortcut.dataset.wbDesk;
      if(!['service','inventory','ads'].includes(pageId))return;
      Object.assign(pane(pageId),{section:pageId==='ads'?'verification':'',legacy:false,receiving:pageId==='inventory',comparison:false,searchTerms:false});
      navigate(pageId);
      if(pageId==='service'){
        window.erpFulfillment?.openOrder();
        const dialog=q('#erpFbmDesk');
        if(dialog){
          const leave=()=>{if(location.hash!=='#service'&&dialog.open)dialog.close();};
          window.addEventListener('hashchange',leave);
          dialog.addEventListener('close',()=>window.removeEventListener('hashchange',leave),{once:true});
        }
        return;
      }
      requestAnimationFrame(()=>{
        if(view!==pageId)return;
        const needsProduct=pageId==='ads'&&mode==='server'&&currentUser()&&productId==='all';
        const target=q(pageId==='inventory'?'#erpReceivingRoot':needsProduct?'#productSelect':'#adsReviewRoot');
        if(!target)return;
        if(target.tagName!=='SELECT')target.tabIndex=-1;
        target.focus({preventScroll:true});target.scrollIntoView({block:needsProduct?'center':'start'});
      });
      return;
    }
    const el=event.target.closest('[data-wb],[data-wb-section],[data-wb-id],[data-wb-signal]');
    if(!el||el.disabled)return;
    if(el.hasAttribute('data-wb-section')){pane().section=el.dataset.wbSection;pane().legacy=false;pane().receiving=false;pane().comparison=false;pane().searchTerms=false;clearQueue();mount();return;}
    if(el.hasAttribute('data-wb-id'))return openRow(el.dataset.wbId);
    if(el.hasAttribute('data-wb-signal')){
      openSignal(signals[Number(el.dataset.wbSignal)]);return;
    }
    switch(el.dataset.wb){
      case 'definitions':configError='';loadDefinitions();break;
      case 'searchTerms':pane().searchTerms=true;pane().legacy=false;draw();break;
      case 'comparison':pane().comparison=true;pane().legacy=false;draw();break;
      case 'receiving':pane().receiving=true;pane().legacy=false;draw();break;
      case 'legacy':pane().searchTerms=false;pane().comparison=false;pane().receiving=false;pane().legacy=true;draw();if(view==='ranking')requestAnimationFrame(drawRanking);break;
      case 'new':handleNew();break;
      case 'login':window.processUI?.login();break;
      case 'close':closeEditor();break;
      case 'copy-old':{
        const field=q('.wb-conflict textarea');field.select();
        try{await navigator.clipboard.writeText(field.value);message('已复制本机旧版本草稿。');}catch{message('旧稿已选中，可用系统复制。');}
        break;
      }
      case 'use-latest':{
        try{localStorage.setItem(editor.key+'/prior',JSON.stringify(editor.conflict));
          localStorage.removeItem(editor.key);editor.archive=editor.conflict;editor.conflict=null;editor.warning='旧版本草稿已另存。';drawEditor();}
        catch{message('无法另存旧稿，请先复制到其他位置。');}
        break;
      }
      case 'latest':if(editor?.item.id)openRow(editor.item.id,editor.item);break;
      case 'back':{
        const origin=previousQueue; previousQueue=null;
        if(origin){productId=origin.product;panes.set(origin.page,origin.state);navigate(origin.page);requestAnimationFrame(()=>window.scrollTo(0,origin.scroll));}break;
      }
      case 'linked':{
        const ref=[editor.item.parent_ref,...(editor.item.child_refs||[])].find(r=>r?.id===el.dataset.ref);
        if(ref)openRow(ref.id,ref);break;
      }
      case 'follow':{
        const form=q('#wbItemForm'),page=form.elements.follow_page.value,section=form.elements.follow_section.value;
        const object_ref=form.elements.follow_object.value.trim();
        if(!object_ref){message('请填写后续办理对象。');break;}
        const ref=editor.item, title=definitions.get(page).sections.find(s=>s.id===section).title+' · '+object_ref;
        openSignal({page_id:page,section_id:section,object_ref,title,site:ref.site,parent_id:ref.id,product_id:ref.product_id,account_ref:ref.account_ref,business_timezone:ref.business_timezone,connection_id:ref.connection_id});break;
      }
      case 'all-records':navigate('team');Object.assign(pane(),{section:'',status:'',mine:'',legacy:false});clearQueue();mount();break;
      case 'refresh':loadQueue();break;
      case 'more':loadQueue(true);break;
      case 'assist':assist();break;
      case 'model':assist(true);break;
      case 'task':{const id=editor?.item.task_id;closeEditor();if(id)openTask(id);break;}
      default:if(el.dataset.wb==='save'||Object.hasOwn(actions,el.dataset.wb))act(el.dataset.wb);
    }
  });
  document.addEventListener('input',ev=>{if(ev.target.closest('#wbItemForm'))autosave();});
  document.addEventListener('change',ev=>{
    const el=ev.target;
    if(el.hasAttribute('data-wb-shop-filter')){pane().shopOnly=el.value==='shop';clearQueue();mount();return;}
    if(el.hasAttribute('data-wb-actor')){demoActor=el.value;changeScope();mount();return;}
    if(el.hasAttribute('data-wb-filter')){pane()[el.dataset.wbFilter]=el.value;
      if(el.dataset.wbFilter==='mine' && ['overview','agent'].includes(view) && !pane().section)loadQueue();else draw();return;}
    if(!editor || !el.closest('#wbItemForm'))return;
    if(el.name==='record_scope'){
      collect();const v=editor.form;v.record_scope=el.value;
      if(el.value==='shop'){v.product_id=null;v.task_id=null;v.site='';}
      else{v.product_id=productId==='all'?'':productId;v.site=ws.products.find(p=>p.id===v.product_id)?.marketplace||'';delete v.account_ref;delete v.business_timezone;delete v.connection_id;}
      drawEditor();autosave();return;
    }
    if(el.name==='shop_connection'){
      const chosen=el.value===''?null:editor.shopScopes?.[Number(el.value)];
      collect();Object.assign(editor.form,chosen||{connection_id:null,account_ref:'',site:'',business_timezone:''});drawEditor();autosave();return;
    }
    if(['account_ref','business_timezone','site'].includes(el.name)&&shopScope(editor.form)){
      const connection=q('#wbItemForm [name=shop_connection]');if(connection)connection.value='';
      editor.form.connection_id=null;
    }
    if(el.name==='follow_page'){
      const list=definitions.get(el.value).sections;
      q('#wbItemForm').elements.follow_section.replaceChildren(...list.map(section=>new Option(section.title,section.id)));
      return;
    }
    if(el.name==='section_id'&&!editor.item.id){
      const section=definitions.get(view).sections.find(s=>s.id===el.value);
      if(section){
        const current=editor.form;
        openEditor(blank(section,{product_id:current.product_id,site:current.site,...(shopScope(current)?{record_scope:'shop',account_ref:current.account_ref,business_timezone:current.business_timezone,connection_id:current.connection_id}:{})}));
      }
    }else if(el.name==='product_id'){
      collect();editor.form.site=ws.products.find(p=>p.id===el.value)?.marketplace||'';
      editor.form.task_id=null;drawEditor();autosave();
    }else autosave();
  });
  document.addEventListener('visibilitychange',()=>{
    if(document.visibilityState==='hidden' && editor?.timer && !editor.working)flushDraft(editor).catch(()=>{});
  });
  window.addEventListener('online',()=>{if(editor&&serverReady(editor))schedule(editor);});
  window.addEventListener('homa-session',()=>{changeScope();mount();});
  window.addEventListener('storage',ev=>{if(mode==='demo' && ev.key===demoStore)loadQueue();});
  function openReceivingOrder(scopeId,orderId,noticeId=''){
    if(view!=='inventory')navigate('inventory');
    pane().legacy=false;pane().receiving=true;draw();
    return window.erpReceiving?.openOrder(scopeId,orderId,noticeId);
  }
  return {mount,invalidate:clearQueue,openSignal,openReceivingOrder};
})();
