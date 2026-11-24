/* Local reconciliation of order lines against settlement evidence. */
(() => {
  'use strict';
  const base='/api/erp-refunds/lines';
  const states={unmarked:'待核对',marked:'人工标记',partial:'已登记部分流水',full:'流水已登记足额'};
  const money=(value,currency)=>new Intl.NumberFormat('zh-CN',{style:'currency',currency}).format(value);
  const action=(label,cmd,extra='')=>`<button type="button" class="btn" data-rfd="${cmd}" ${extra}>${e(label)}</button>`;
  const input=(label,name,attributes='',value='')=>`<label class="field">${e(label)}<input name="${name}" value="${e(value)}" ${attributes}></label>`;
  const option=(v,t,selected)=>`<option value="${e(v)}" ${selected===v?'selected':''}>${e(t)}</option>`;
  let dialog, panel, serial=0, scope, filter, offset, entriesOffset, marksOffset, detail, submitting=false;
  const user=()=>window.processUI?.currentUser();
  function notice(text) {panel.querySelector('[role="alert"]').textContent=text;}
  function show(content) {panel.innerHTML=content+'<p role="alert" class="rfd-error"></p>';}
  function active(){return Boolean(dialog?.open);}
  function open() {
    if(active())return;
    scope=productId;
    filter={order_ref:'',account_ref:'',state:''};
    offset=entriesOffset=marksOffset=0;detail=null;
    dialog=document.createElement('dialog');dialog.className='wb-dialog rfd-dialog';
    dialog.setAttribute('aria-labelledby','rfd-title');
    dialog.innerHTML='<header class="wb-dialog-head"><h2 id="rfd-title">退款核对台账</h2>'+action('关闭','close')+'</header><div class="wb-dialog-body"></div>';
    panel=dialog.querySelector('.wb-dialog-body');document.body.append(dialog);
    const opened=dialog;
    dialog.addEventListener('close',()=>{
      opened.remove();
      if(dialog===opened){serial++;dialog=null;}
    },{once:true});
    dialog.addEventListener('cancel',event=>{if(submitting)event.preventDefault();});
    dialog.addEventListener('click',handleClick);dialog.addEventListener('submit',handleSubmit);
    dialog.showModal();
    if(mode==='demo'){
      show('<h3>订单行核对示例</h3><p>示例订单行 SKU-100 有两笔退款，20.00 USD 和 5.00 USD，共 25.00 USD。撤销人工标记后，两笔结算流水仍可追溯。</p><p>请在已连接数据库的工作台登录后登记真实订单行。</p>');
    }else if(!user()){
      show('<p>登录协作账户后查看自己登记的订单行。流程管理员可以核对工作区的结算流水。</p>'+action('登录协作账户','login'));
    }else list();
  }
  function filterForm(){
    return `<form data-rfd-form="filter"><div class="rfd-fields">
      ${input('订单号','order_ref','maxlength="150"',filter.order_ref)}${input('店铺账号','account_ref','maxlength="150"',filter.account_ref)}
      <label class="field">核对状态<select name="state">${option('','全部',filter.state)}${Object.entries(states).map(([k,v])=>option(k,v,filter.state)).join('')}</select></label>
      </div><div class="rfd-actions"><button class="btn" type="submit">筛选</button>${action('登记订单行','new')}</div></form>`;
  }
  async function list() {
    const token=++serial;detail=null;
    const query=new URLSearchParams({offset:String(offset),limit:'25'});
    if(scope!=='all')query.set('product_id',scope);
    for(const [k,v] of Object.entries(filter))if(v)query.set(k,v);
    show(filterForm()+'<p>正在读取订单行…</p>');
    try{
      const result=await api(base+'?'+query);
      if(token!==serial||!active())return;
      const lines=result.items.map(x=>`<li><div><b>${e(x.order_ref)} · ${e(x.line_ref)}</b><small>${e(x.account_ref)} / ${e(x.site)} · ${e(x.sku)}</small><span>${e(states[x.state])} · ${x.entry_count} 笔 · ${e(money(x.recorded_amount,x.currency))}</span></div>${action('查看明细','item',`data-id="${e(x.id)}"`)}</li>`).join('');
      show(filterForm()+(lines?`<ul class="rfd-list">${lines}</ul>`:'<p>当前范围内暂无符合条件的订单行。</p>')+
        '<div class="rfd-actions">'+(offset?action('上一页','prev'):'')+(result.next_offset!==null?action('下一页','next'):'')+'</div>');
    }catch(err){if(token===serial)notice(err.message);}
  }
  function lineForm(){
    ++serial;
    const products=scope==='all'?ws.products:ws.products.filter(p=>p.id===scope);
    if(!products.length){show('<p>请先建立订单商品的产品主档。</p>'+action('返回','list'));return;}
    show(`<h3>登记订单行</h3><form data-rfd-form="line"><div class="rfd-fields">
      <label class="field">产品与站点<select name="product_id" required>${products.map(p=>option(p.id,p.model+' · '+p.marketplace,'')).join('')}</select></label>
      ${input('店铺账号标识','account_ref','required maxlength="150"',products[0].data_account_key||'')}
      ${input('脱敏订单号','order_ref','required maxlength="150"')}${input('订单行号','line_ref','required maxlength="150"')}
      ${input('订单商品 SKU','sku','required maxlength="150"',products[0].sku||'')}${input('币种','currency','required readonly',products[0].currency)}
      ${input('订单行可退金额','refundable_amount','type="number" min="0.0001" step="any" required')}
      </div><div class="rfd-actions">${action('返回','list')}<button class="btn primary">保存订单行</button></div></form>`);
    panel.querySelector('[name="product_id"]').addEventListener('change',event=>{
      const p=products.find(x=>x.id===event.target.value);const fields=event.target.form.elements;
      fields.currency.value=p.currency;fields.account_ref.value=p.data_account_key||'';fields.sku.value=p.sku||'';
    });
  }
  async function loadItem(id){
    const token=++serial;
    try{
      const result=await api(base+'/'+id+'?limit=10&offset='+entriesOffset+'&mark_offset='+marksOffset);
      if(token===serial&&active()){detail=result;drawItem();}
    }catch(err){if(token===serial)notice(err.message);}
  }
  function drawItem(){
    const x=detail, rows=x.entries.items.map(r=>`<li><div><b>${e(money(r.amount,r.currency))} · ${e(r.posted_on)}</b><span>流水行 ${e(r.entry_ref)} · 结算单 ${e(r.statement_ref)}</span><small>登记人 ${e(r.recorded_by_name)} · ${e(new Date(r.recorded_at).toLocaleString())}</small></div></li>`).join('');
    const marks=x.marks.items.map(m=>`<li><div><b>${m.marked?'添加':'撤销'}人工标记 · ${e(m.actor_name)}</b><span>${e(m.reason)}</span><small>${e(new Date(m.created_at).toLocaleString())}</small></div></li>`).join('');
    show(action('返回订单行','list')+`<h3>${e(x.order_ref)} · 行 ${e(x.line_ref)}</h3><p>${e(x.account_ref)} / ${e(x.site)} · ${e(x.sku)}</p>
      <p><b>${e(states[x.state])}</b> · 可退 ${e(money(x.refundable_amount,x.currency))} · 已登记 ${e(money(x.recorded_amount,x.currency))} · 尚可登记 ${e(money(x.remaining_amount,x.currency))}</p>
      ${action('退货包裹与实收','packages')}<h3>外部结算流水 · ${x.entry_count} 笔</h3><p>此处记录经办方核对过的外部凭证。实际资金处理在原平台完成。</p>${rows?`<ul class="rfd-list">${rows}</ul>`:'<p>暂无结算流水。</p>'}
      <div class="rfd-actions">${entriesOffset?action('上一页流水','entries-prev'):''}${x.entries.next_offset!==null?action('下一页流水','entries-next'):''}</div>
      ${x.can_record_entry&&Number(x.remaining_amount)>0?`<details><summary>登记结算流水</summary><form data-rfd-form="entry"><div class="rfd-fields">
      ${input('流水行唯一引用','entry_ref','required maxlength="150"')}${input('结算单或报表凭证编号','statement_ref','required maxlength="150"')}
      ${input('结算日期','posted_on','type="date" required')}${input('金额（'+x.currency+'）','amount','type="number" min="0.0001" step="any" required')}
      </div><div class="rfd-actions"><button class="btn primary">登记该笔流水</button></div></form></details>`:!x.can_record_entry?'<p>请由流程管理员核对原结算单并登记流水。</p>':''}
      <h3>人工标记 · ${x.marked?'已标记':'未标记'}</h3><form data-rfd-form="mark">
      ${input(x.marked?'撤销原因':'标记依据','reason','required maxlength="500"')}<div class="rfd-actions"><button class="btn">${x.marked?'撤销人工标记':'添加人工标记'}</button></div></form>
      ${marks?`<ul class="rfd-list">${marks}</ul>`:''}<div class="rfd-actions">${marksOffset?action('上一页标记','marks-prev'):''}${x.marks.next_offset!==null?action('下一页标记','marks-next'):''}</div>`);
  }
  const channels={local:'本地仓',third_party:'三方仓',fba:'FBA'};
  const sources={local:'local_record',third_party:'warehouse_report',fba:'fba_report'};
  const evidenceNames={local:'本地签收或质检凭证',third_party:'三方仓报告',fba:'FBA 退货报告'};
  let packages, pack, packageOffset=0, eventOffset=0;
  const returnsPath=()=>'/api/erp-returns/lines/'+detail.id+'/packages';
  function countField(label,name,maximum,initial=''){
    return input(label,name,`type="number" min="0" max="${Number(maximum)}" step="1"`,initial);
  }
  async function loadPackages(){
    const token=++serial;
    try{
      const result=await api(returnsPath()+'?limit=20&offset='+packageOffset);
      if(token!==serial||!active())return;
      packages=result;
      const items=result.items.map(p=>`<li><div><b>${e(p.package_ref)} · ${e(channels[p.channel])}</b>
        <span>预计 ${p.expected_units} · 已收 ${p.received} · 待处置 ${p.unclassified}</span></div>
        ${action('包裹明细','package',`data-id="${e(p.id)}"`)}</li>`).join('');
      show(action('返回退款明细','back-line')+`<h3>退货包裹 · ${e(detail.order_ref)} / ${e(detail.line_ref)}</h3>
        <p>${e(detail.sku)} · ${e(detail.account_ref)} / ${e(detail.site)}</p>
        <p>${result.order_quantity?'原单 '+result.order_quantity.ordered_units+' 件，已登记包裹 '+result.expected_total+' 件。原单凭证 '+e(result.order_quantity.order_evidence)+'。':'首次登记包裹时，请核对原订单行的件数和凭证。'}</p>
        ${!result.order_quantity||result.expected_total<result.order_quantity.ordered_units?action('登记退货包裹','new-package'):''}
        ${items?'<ul class="rfd-list">'+items+'</ul>':'<p>本订单行暂无包裹。</p>'}
        <div class="rfd-actions">${packageOffset?action('上一页包裹','pack-prev'):''}${result.next_offset!==null?action('下一页包裹','pack-next'):''}</div>`);
    }catch(err){if(token===serial)notice(err.message);}
  }
  function newPackage(){
    const previous=packages.order_quantity;
    show(action('返回包裹列表','packages')+`<h3>登记退货包裹</h3><p>原订单行的数量和凭证将用于校验后续包裹的累计件数。</p>
      <form data-rfd-form="package"><div class="rfd-fields">
      ${input('原订单行件数','ordered_units','type="number" min="1" step="1" max="10000000" required '+(previous?'readonly':''),previous?.ordered_units??'')}
      ${input('原订单行凭证','order_evidence','maxlength="150" required '+(previous?'readonly':''),previous?.order_evidence??'')}
      ${input('退货包裹或物流引用','package_ref','maxlength="150" required')}
      ${input('该包裹预计退回件数','expected_units','type="number" min="1" step="1" max="10000000" required')}
      <label class="field">收货渠道<select name="channel">${Object.entries(channels).map(([k,v])=>option(k,v,'')).join('')}</select></label>
      </div><div class="rfd-actions"><button class="btn primary">保存包裹</button></div></form>`);
  }
  async function loadPackage(id){
    const token=++serial;
    try{
      const result=await api(returnsPath()+'/'+id+'?limit=25&offset='+eventOffset);
      if(token===serial&&active()){pack=result;drawPackage();}
    }catch(err){if(token===serial)notice(err.message);}
  }
  function eventForm(kind,p){
    const receive=kind==='receive', label=receive?'登记实收':'登记处置';
    const fields=receive?input('本次实收件数','received',`type="number" min="1" max="${p.outstanding}" step="1" required`):
      countField('良品件数','good',p.unclassified,0)+countField('次品件数','defective',p.unclassified,0)+countField('销毁件数','destroyed',p.unclassified,0);
    return `<details><summary>${label}</summary><form data-rfd-form="${kind}"><div class="rfd-fields">
      ${input('本笔记录唯一引用','event_ref','maxlength="150" required')}${input(evidenceNames[p.channel],'evidence_ref','maxlength="150" required')}
      ${input('凭证业务日期','occurred_on','type="date" required')}${fields}</div>
      <div class="rfd-actions"><button class="btn primary">保存${receive?'实收':'处置'}流水</button></div></form></details>`;
  }
  function stockAction(record,p){
    if(p.channel!=='local'||record.kind!=='dispose')return '';
    if(record.stock_posted)return `<p>已入本地库存${record.stock_warehouse?' · '+e(record.stock_warehouse):''} · 良品 ${record.good} / 次品 ${record.defective}</p>`;
    if(!record.good&&!record.defective)return '<p>本次全部销毁，无入库件数。</p>';
    if(!p.stock_scopes?.length)return '<p>等待有本店铺、本站点仓库权限的经办人入库。</p>';
    return `<form data-rfd-form="stock" data-stock-event="${e(record.id)}">
      <input type="hidden" name="event_id" value="${e(record.id)}">
      <label>实际收货仓库<select name="scope_id" required><option value="">选择实际仓库</option>
      ${p.stock_scopes.map(scope=>`<option value="${e(scope.id)}">${e(scope.warehouse)} · ${e(scope.site)}</option>`).join('')}</select></label>
      <button class="btn">入库良品 ${record.good}、次品 ${record.defective}</button></form>`;
  }
  function drawPackage(){
    const p=pack;
    const events=p.events.items.map(r=>`<li><div><b>${e(r.occurred_on)} · ${r.kind==='receive'?'实收 '+r.received+' 件':'处置 · 良 '+r.good+' / 次 '+r.defective+' / 销毁 '+r.destroyed}</b>
       <span>${e(r.event_ref)} · 凭证 ${e(r.evidence_ref)}</span><small>${e(r.recorded_by_name)} · ${e(new Date(r.recorded_at).toLocaleString())}</small>${stockAction(r,p)}</div></li>`).join('');
    show(action('返回包裹列表','packages')+`<h3>${e(p.package_ref)} · ${e(channels[p.channel])}</h3><p>${e(detail.order_ref)} / ${e(detail.line_ref)} · ${e(detail.sku)}</p>
      <p>预计 ${p.expected_units} 件，实收 ${p.received} 件，待收 ${p.outstanding} 件，待处置 ${p.unclassified} 件。</p>
      <p>良品 ${p.good} · 次品 ${p.defective} · 销毁 ${p.destroyed}</p>
      <p>${p.channel==='local'?'按处置凭证将良品和次品入实际仓库，销毁件数单独留存。首笔入库确定本包裹的收货仓。':'按'+e(evidenceNames[p.channel])+'登记外部仓实收和处置。'}退款金额以独立结算流水核对。</p>
      ${p.can_record?(p.outstanding?eventForm('receive',p):'')+(p.unclassified?eventForm('dispose',p):''):'<p>请交由流程管理员核对凭证并登记实收或处置。</p>'}
      <h3>包裹流水</h3>${events?'<ul class="rfd-list">'+events+'</ul>':'<p>暂无实收或处置流水。</p>'}
      <div class="rfd-actions">${eventOffset?action('上一页流水','event-prev'):''}${p.events.next_offset!==null?action('下一页流水','event-next'):''}</div>`);
  }
  async function handleClick(event){
    const b=event.target.closest('[data-rfd]');if(!b||submitting)return;
    const cmd=b.dataset.rfd;
    if(cmd==='packages'){packageOffset=0;loadPackages();}
    else if(cmd==='pack-prev'||cmd==='pack-next'){packageOffset+=cmd==='pack-next'?20:-20;loadPackages();}
    else if(cmd==='back-line'){entriesOffset=marksOffset=0;loadItem(detail.id);}
    else if(cmd==='new-package')newPackage();
    else if(cmd==='package'){eventOffset=0;loadPackage(b.dataset.id);}
    else if(cmd==='event-prev'||cmd==='event-next'){eventOffset+=cmd==='event-next'?25:-25;loadPackage(pack.id);}
    else if(cmd==='close')dialog.close();
    else if(cmd==='login'){dialog.close();window.processUI.login();}
    else if(cmd==='list')list();
    else if(cmd==='new')lineForm();
    else if(cmd==='next'||cmd==='prev'){offset+=cmd==='next'?25:-25;list();}
    else if(cmd==='item'){entriesOffset=marksOffset=0;loadItem(b.dataset.id);}
    else if(cmd.startsWith('entries-')||cmd.startsWith('marks-')){
      const delta=cmd.endsWith('next')?10:-10;
      if(cmd.startsWith('entries-'))entriesOffset+=delta;else marksOffset+=delta;
      loadItem(detail.id);
    }
  }
  async function handleSubmit(event){
    event.preventDefault();if(submitting)return;
    const f=event.target,body=Object.fromEntries(new FormData(f)),kind=f.dataset.rfdForm;
    if(kind==='filter'){filter=body;offset=0;return list();}
    let url=base,method='POST';
    const packageWrite=['package','receive','dispose','stock'].includes(kind);
    if(kind==='stock'){url='/api/erp-returns/packages/'+pack.id+'/post-stock';body.request_id=f.dataset.requestId||(f.dataset.requestId=crypto.randomUUID());}
    if(kind==='package'){url=returnsPath();body.expected_units=Number(body.expected_units);body.ordered_units=Number(body.ordered_units);}
    if(kind==='receive'||kind==='dispose'){
      url=returnsPath()+'/'+pack.id+'/events';body.kind=kind;body.evidence_type=sources[pack.channel];
      for(const key of ['received','good','defective','destroyed'])if(key in body)body[key]=Number(body[key]);
    }
    if(kind==='mark'){url+='/'+detail.id+'/mark';method='PUT';body.marked=!detail.marked;body.expected_version=detail.version;}
    if(kind==='entry'){url+='/'+detail.id+'/entries';body.currency=detail.currency;}
    const token=++serial;submitting=true;const button=event.submitter;if(button)button.disabled=true;
    try{
      const result=await api(url,{method,body:JSON.stringify(body)});
      if(token===serial&&active()){
        if(packageWrite){eventOffset=0;pack=result;drawPackage();}
        else{entriesOffset=marksOffset=0;detail=result;drawItem();}
      }
    }catch(err){
      if(token===serial){notice(err.message);if(err.status===409&&kind==='mark')panel.querySelector('[role="alert"]').insertAdjacentHTML('beforeend',' '+action('重读订单行','item','data-id="'+e(detail.id)+'"'));}
    }finally{submitting=false;if(button)button.disabled=false;}
  }
  document.addEventListener('click',event=>{if(event.target.closest('[data-erp-refunds-open]'))open();});
})();
