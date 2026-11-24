/* Warehouse receiving posts to the authenticated ERP API. The workbench sample has no stock ledger. */
window.erpReceiving = (() => {
  'use strict';
  const base = '/api/erp-receiving';
  const tabs = {purchase:'采购与通知', scan:'扫码收货', receiving:'收货与质检', ledger:'库存流水', access:'仓库与授权'};
  const statusText = {waiting:'待收货',received:'已收货',cancelled:'已取消'};
  const actionText = {receive_normal:'普通收货',receive_quick:'快捷收货',inspect:'质检',waive:'免检',reverse:'冲销',cancel:'取消通知'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const selected = (a,b) => a===b ? ' selected' : '';
  const option = (value,title,active) => `<option value="${esc(value)}"${selected(value,active)}>${esc(title)}</option>`;
  const button = (text,action,extra='') => `<button type="button" class="btn" data-rx="${action}" ${extra}>${esc(text)}</button>`;
  const submit = text => `<button class="btn primary" type="submit">${esc(text)}</button>`;
  const input = (title,name,attrs='') => `<label>${esc(title)}<input name="${name}" ${attrs}></label>`;
  const table = (headers,body) => `<div class="rx-table-wrap"><table class="rx-table"><thead><tr>${headers.map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>`;
  const section = (title,body) => `<section class="rx-section"><h3>${esc(title)}</h3>${body}</section>`;
  const num = (name,max,label) => `<input type="number" min="0" max="${esc(max)}" step="1" name="${name}" value="0" aria-label="${esc(label)}">`;
  const fmtTime = (value,zone) => value ? new Date(value).toLocaleString('zh-CN',{timeZone:zone,hour12:false}) : '—';
  let current=null;
  const fallbackTokens=new Map();

  function noticeIn(s){return s.order?.notices.find(n=>n.id===s.noticeId);}
  function tell(s,message,error=false){
    s.message=message;s.error=error;
    const box=s.root.querySelector('[data-rx-feedback]');
    if(box){box.hidden=!message;box.textContent=message;box.dataset.error=String(error);box.setAttribute('role',error?'alert':'status');}
  }
  function ordersOf(s){return s.orders || [];}
  function activeScope(s){return s.scopes.find(w=>w.id===s.scopeId);}
  function scopeCaption(w){return [w.warehouse,w.account_ref,w.site].filter(Boolean).join(' · ');}
  function qtyRows(lines,key,render){return lines.filter(line=>line[key]>0).map(render).join('');}
  function purchaseEditor(){
    return section('登记采购基线',`<form data-rx-form="order"><div class="rx-selectors">
      ${input('采购单号','number','required maxlength="100"')}${input('供应商','supplier','required maxlength="120"')}
      </div>${table(['SKU','采购数量',''],orderRow())}<div class="rx-actions">${button('增加 SKU','add-line')}${submit('建立采购单')}${button('返回单据','close-order')}</div></form>`);
  }
  function orderRow(){return `<tr data-rx-order-row><td><input name="sku" required maxlength="120" aria-label="SKU"></td><td><input name="qty" type="number" min="1" step="1" required aria-label="采购数量"></td><td>${button('移除','remove-line')}</td></tr>`;}
  function noticeTable(s){
    return (s.order.notices||[]).length ? table(['通知单','状态','通知件数','实收件数',''],s.order.notices.map(n=>
      `<tr><td>${esc(n.number)}</td><td>${esc(statusText[n.status]||n.status)}</td><td>${n.lines.reduce((sum,l)=>sum+l.qty,0)}</td><td>${n.lines.reduce((sum,l)=>sum+l.received,0)}</td><td>${button('查看明细','open-notice',`data-id="${esc(n.id)}"`)}</td></tr>`).join('')) : '<p class="rx-tip">尚未创建到货通知。</p>';
  }
  function purchase(s){
    if(s.createOrder)return purchaseEditor();
    if(!s.order)return section('采购单','<p class="rx-tip">本仓库尚无采购单。</p>');
    const o=s.order;
    const lines=o.lines.map(l=>`<tr data-line="${esc(l.id)}"><td>${esc(l.sku)}</td><td>${l.qty}</td><td>${l.received}</td><td>${l.notified}</td><td>${l.unnotified}</td><td>${l.unnotified>0?num('qty',l.unnotified,l.sku+' 本次通知数量'):'—'}</td></tr>`).join('');
    return section(o.number+' · '+o.supplier,`<form data-rx-form="notice">${table(['SKU','采购','已收','待收通知','可再通知','本次通知'],lines)}
      ${o.lines.some(l=>l.unnotified>0)?`<div class="rx-actions">${submit('创建到货通知')}</div>`:''}</form>`)+section('到货通知',noticeTable(s));
  }

  function receiveTable(n,quick,matched=[]){
    const rows=n.lines.map(l=>`<tr data-line="${esc(l.line_id)}" ${matched.includes(l.line_id)?'data-rx-scanned-line tabindex="-1"':''}><td>${esc(l.sku)}${matched.includes(l.line_id)?'<small class="rx-match-label">扫码命中</small>':''}</td><td>${l.qty}</td><td>${num('qty',l.qty,l.sku+' 实收数量')}</td>${quick?`<td>${num('good',l.qty,l.sku+' 良品')}</td><td>${num('bad',l.qty,l.sku+' 次品')}</td>`:''}</tr>`).join('');
    return table(['SKU','通知数量','实收数量',...(quick?['良品','次品']:[])],rows);
  }
  function scanView(s){
    const search=s.scan;
    const form=`<form data-rx-form="scan"><label>条码或单号<input name="code" required maxlength="140" autocomplete="off" spellcheck="false" value="${esc(search.code)}" placeholder="采购单号、通知号、SKU 或 FNSKU"></label>
      <p class="rx-tip">扫描后按回车，或输入编码查询。打开待收通知后核对实收数量，再确认入账。</p><div>${submit('查询待收通知')}</div></form>`;
    let matches='';
    if(search.done){
      const by={order_number:'采购单号',notice_number:'通知号',sku:'SKU',fnsku:'FNSKU'};
      const rows=search.items.map((item,index)=>{
        const matched=item.lines.filter(l=>l.matched),lines=matched.length?matched:item.lines;
        const skus=lines.slice(0,3).map(l=>l.sku+' × '+l.qty).join('、')+(lines.length>3?' 等 '+lines.length+' 行':'');
        return `<tr><td>${esc(item.notice_number)}</td><td>${esc(item.order_number)}</td><td>${esc(item.supplier)}</td>
          <td>${esc(skus)}</td><td>${esc(item.matched_by.map(k=>by[k]||k).join(' / '))}</td><td>${button('选此通知','scan-pick',`data-index="${index}" aria-label="选择通知 ${esc(item.notice_number)}"`)}</td></tr>`;
      }).join('');
      matches=section('待收候选 · '+search.code,rows?`<p class="rx-tip">请核对采购单、通知与供应商后选单。</p>${table(['通知号','采购单号','供应商','SKU 与通知量','匹配','操作'],rows)}${search.next!=null?button('更多匹配通知','scan-more'):''}`:
        `<p class="rx-tip">${search.mapped?'已关联 SKU '+esc(search.mapped)+'。':''}当前仓库没有对应的待收通知。</p>`);
    }
    return section('扫码收货',form)+matches+(s.user.is_admin?barcodeManager(s):'');
  }
  function barcodeManager(s){
    const b=s.barcode;
    const editor=b.checked?`<form data-rx-form="barcode-save">
      <p class="rx-tip">${b.binding?'当前对照 · '+esc(b.code)+' → '+esc(b.binding.sku):'本仓尚未登记 '+esc(b.code)+'。'}</p>
      <label>本仓采购 SKU<input name="sku" maxlength="120" required value="${esc(b.binding?.sku||'')}" autocomplete="off"></label>
      <div>${submit(b.binding?'保存改配':'建立对照')}</div></form>`:'';
    return `<section class="rx-section"><details ${b.open?'open':''}><summary>维护本仓 FNSKU 对照</summary>
      <p class="rx-tip">先查询 FNSKU，再按当前仓库采购单上的 SKU 建立或修改对照。</p>
      <form data-rx-form="barcode-lookup">${input('FNSKU','fnsku',`required maxlength="120" autocomplete="off" value="${esc(b.code)}"`)}<div>${submit('查找对照')}</div></form>
      ${editor}</details></section>`;
  }
  async function barcodeAction(s,form){
    if(s.loading||s.writing||!s.user.is_admin||!s.scopeId)return;
    const search=form.dataset.rxForm==='barcode-lookup';
    const code=(search?form.elements.fnsku.value:s.barcode.code).trim();
    if(!code){tell(s,'请输入完整 FNSKU。',true);return;}
    if(!search&&s.root.querySelector('[data-rx-form=barcode-lookup] input')?.value.trim()!==code){
      tell(s,'请先查找当前 FNSKU，再保存对应 SKU。',true);return;
    }
    const sku=search?'':form.elements.sku.value.trim();
    if(!search&&!sku){tell(s,'请填写本仓已有采购行的 SKU。',true);return;}
    const revision=++s.revision,live=()=>current===s&&revision===s.revision;
    s.writing=true;s.barcode.open=true;tell(s,'');lock(s,true);
    try{
      if(search){
        const p=new URLSearchParams({scope_id:s.scopeId,fnsku:code});
        const found=await s.api(base+'/barcodes?'+p);if(!live())return;
        s.barcode={open:true,checked:true,code,binding:found.items[0]||null};
        draw(s);tell(s,found.items.length?'已读取本仓现有对照。':'当前 FNSKU 尚无对照，可登记本仓采购 SKU。');
      }else{
        const body={scope_id:s.scopeId,fnsku:code,sku};
        if(s.barcode.binding)body.expected_sku=s.barcode.binding.sku;
        const bound=await s.api(base+'/barcodes',{method:'POST',body:JSON.stringify(body)});if(!live())return;
        s.barcode={open:true,checked:true,code,binding:bound};
        s.scan={code:'',items:[],done:false};draw(s);tell(s,'对照已保存 · '+code+' → '+bound.sku);
      }
    }catch(err){if(live())tell(s,err.status===409?err.message+' 请重新查找并核对当前映射。':err.message,true);}
    finally{if(live()){s.writing=false;lock(s,false);}}
  }
  async function scanCode(s,code,more=false){
    if(s.loading||s.writing||!s.scopeId)return;
    code=String(code||'').trim();if(!code){tell(s,'请扫描或输入单据号、SKU 或 FNSKU。',true);return;}
    const revision=++s.revision,live=()=>current===s&&revision===s.revision;
    const prior=more?s.scan.items:[];let one;
    s.loading=true;tell(s,'');draw(s);
    try{
      const params=new URLSearchParams({scope_id:s.scopeId,code,offset:String(more?s.scan.next:0)});
      const result=await s.api(base+'/scan?'+params);if(!live())return;
      if(result.scope_id!==s.scopeId)throw new Error('扫码结果与当前仓库不符。');
      const known=new Set(prior.map(n=>n.notice_id));
      s.scan={code,items:[...prior,...result.items.filter(n=>!known.has(n.notice_id))],mapped:result.mapped_sku,done:true,next:result.next_offset};
      if(!more&&!result.has_more&&result.items.length===1)one=result.items[0];
    }catch(err){if(live()){s.scan={code,items:[],done:false};tell(s,err.message,true);}}
    finally{if(live()){s.loading=false;draw(s);}}
    if(!live())return;
    if(one)await openScanned(s,one);
    else{s.root.querySelector('[data-rx-form="scan"] input')?.select();}
  }
  async function openScanned(s,item){
    if(!item||item.scope_id!==s.scopeId||s.loading||s.writing)return;
    resetForms(s);s.tab='receiving';s.noticeId=item.notice_id;
    s.scanHit={noticeId:item.notice_id,lines:item.lines.filter(l=>l.matched).map(l=>l.line_id)};
    const done=load(s,item.scope_id,item.order_id,item.notice_id);const revision=s.revision;
    await done;if(s!==current||s.revision!==revision||s.tab!=='receiving'||!s.ready)return;
    const notice=noticeIn(s);if(!notice||notice.status!=='waiting'){
      tell(s,'该通知已处理，请重新扫描并核对待收状态。',true);return;
    }
    tell(s,'已定位待收通知 '+notice.number+'，请核对实收数量。');
    s.root.querySelector('[data-rx-scanned-line]')?.focus();
  }
  function qualityTable(n,waive){
    const rows=qtyRows(n.lines,'pending',l=>`<tr data-line="${esc(l.line_id)}"><td>${esc(l.sku)}</td><td>${l.pending}</td><td>${num('good',l.pending,l.sku+' 本次良品')}</td>${waive?'':`<td>${num('bad',l.pending,l.sku+' 本次次品')}</td>`}</tr>`);
    return table(['SKU','待检数量',...(waive?['免检入良品']:['本次良品','本次次品'])],rows);
  }
  function receiving(s){
    if(!s.order?.notices.length)return section('收货与质检','<p class="rx-tip">先在采购单中创建到货通知。</p>');
    const n=noticeIn(s) || s.order.notices[0];s.noticeId=n.id;
    let out=section('收货明细',`<label>到货通知<select data-rx-select="notice">${s.order.notices.map(x=>option(x.id,x.number+' · '+(statusText[x.status]||x.status),n.id)).join('')}</select></label>
      ${table(['SKU','通知','实收','待检','良品','次品'],n.lines.map(l=>`<tr><td>${esc(l.sku)}</td><td>${l.qty}</td><td>${l.received}</td><td>${l.pending}</td><td>${l.good}</td><td>${l.bad}</td></tr>`).join(''))}`);
    if(n.status==='waiting'){
      const quick=s.receiveMode==='quick';
      out+=section('办理收货',`<form data-rx-form="receive"><label>收货方式<select data-rx-select="receive-mode">${option('normal','普通收货 · 进入待检',s.receiveMode)}${option('quick','快捷收货 · 直接判定良次品',s.receiveMode)}</select></label>
        <p class="rx-tip">按实际到货填写，短收数量将释放回采购单。快捷收货的良品与次品之和须等于实收。</p>${receiveTable(n,quick,s.scanHit?.noticeId===n.id?s.scanHit.lines:[])}<div>${submit('确认收货')}</div></form>`);
      out+=section('取消待收通知',`<details><summary>填写取消原因</summary><form data-rx-form="cancel">${input('取消原因','reason','required maxlength="500"')}${submit('确认取消通知')}</form></details>`);
    }
    if(n.lines.some(l=>l.pending>0)){
      const waive=s.qualityMode==='waive';
      out+=section('处理待检库存',`<form data-rx-form="quality"><label>处理方式<select data-rx-select="quality-mode">${option('inspect','质检',s.qualityMode)}${option('waive','免检入库',s.qualityMode)}</select></label>${qualityTable(n,waive)}${input('办理备注','note','maxlength="500"')}<div>${submit(waive?'确认免检入库':'登记质检结果')}</div></form>`);
    }
    if(n.events?.length){
      out+=section('通知操作记录',table(['操作','仓库当地时间','经办人','说明',''],n.events.map(ev=>{
        const reverse=s.user.is_admin&&!ev.reversed&&['receive_normal','receive_quick','inspect','waive'].includes(ev.action);
        const member=s.people.find(person=>person.id===ev.actor_id);
        return `<tr><td>${esc(actionText[ev.action]||ev.action)}${ev.reversed?' · 已冲销':''}</td><td>${esc(fmtTime(ev.created_at,s.order.business_timezone))}</td><td>${esc(member?.display_name||ev.actor_id)}</td><td>${esc(ev.note||'—')}</td><td>${reverse?button('冲销','reverse',`data-id="${esc(ev.id)}"`):''}</td></tr>`;
      }).join('')));
      if(s.reverseId)out+=section('冲销操作',`<form data-rx-form="reverse"><p class="rx-tip">确认后将按相反方向登记库存流水。收货已有后续质检时，须先冲销后续操作。</p>${input('冲销原因','reason','required maxlength="500"')}<div class="rx-actions">${submit('确认冲销')}${button('返回','close-reverse')}</div></form>`);
    }
    return out;
  }

  function ledger(s){
    if(!s.order)return section('采购单库存流水','<p class="rx-tip">请选择采购单。</p>');
    const sums=new Map(s.order.lines.map(line=>[line.sku,{pending:0,good:0,bad:0}]));
    for(const entry of s.order.ledger||[]){
      const b=sums.get(entry.sku);if(b){b.pending+=entry.pending_delta;b.good+=entry.good_delta;b.bad+=entry.bad_delta;}
    }
    const headers=['SKU','待检','良品','次品'];
    const totals=Array.from(sums,([sku,b])=>`<tr><td>${esc(sku)}</td><td>${b.pending}</td><td>${b.good}</td><td>${b.bad}</td></tr>`).join('');
    const lines=(s.order.ledger||[]).map(l=>{
      const n=s.order.notices.find(n=>n.id===l.notice_id), ev=n?.events.find(ev=>ev.id===l.event_id);
      const signed=v=>v>0?'+'+v:String(v);
      return `<tr><td>${esc(fmtTime(l.created_at,s.order.business_timezone))}</td><td>${esc(n?.number||'—')}</td><td>${esc(actionText[ev?.action]||ev?.action||'—')}</td><td>${esc(l.sku)}</td><td>${signed(l.pending_delta)}</td><td>${signed(l.good_delta)}</td><td>${signed(l.bad_delta)}</td></tr>`;
    }).join('');
    return section('本采购单库存变动净额',`<p class="rx-tip">${esc(s.order.number)} · 按 SKU 汇总本采购单收货、质检和冲销产生的流水。</p>${table(headers,totals)}`)+section('库存流水',lines?table(['入账时间（'+esc(s.order.business_timezone)+'）','通知单','操作','SKU','待检变化','良品变化','次品变化'],lines):'<p class="rx-tip">本采购单尚无库存流水。</p>');
  }
  function access(s){
    const w=s.newScope?null:activeScope(s);
    if(!s.user.is_admin)return section('我的收货范围',s.scopes.length?table(['仓库','店铺账号','站点','经营时区'],s.scopes.map(w=>`<tr><td>${esc(w.warehouse)}</td><td>${esc(w.account_ref)}</td><td>${esc(w.site)}</td><td>${esc(w.business_timezone)}</td></tr>`).join('')):'<p class="rx-tip">尚未获得仓库权限，请联系流程管理员。</p>');
    const field=(title,name,value)=>input(title,name,`required value="${esc(value||'')}" ${w?'readonly':''}`);
    const users=s.people.filter(person=>!person.is_admin);
    return section(w?'仓库授权 · '+w.warehouse:'新建仓库范围',`<form data-rx-form="scope"><div class="rx-selectors">
      ${field('店铺账号','account_ref',w?.account_ref)}${field('站点代码','site',w?.site)}
      ${field('仓库名称','warehouse',w?.warehouse)}${field('经营时区（IANA）','business_timezone',w?.business_timezone)}
      </div><fieldset class="rx-people"><legend>允许办理的成员</legend>${users.length?users.map(person=>`<label><input type="checkbox" name="member" value="${esc(person.id)}" ${w?.member_ids?.includes(person.id)?'checked':''}>${esc(person.display_name)} · ${esc(person.department)}</label>`).join(''):'<span class="rx-tip">尚无其他流程成员。管理员可在团队管理中开通成员。</span>'}</fieldset>
      <div class="rx-actions">${submit(w?'保存仓库授权':'建立仓库范围')}${w?button('新增仓库','new-scope'):s.scopeId?button('返回已有仓库','close-scope'):''}</div></form>`);
  }
  function draw(s){
    if(current!==s)return;
    if(s.mode!=='server'||!s.user){
      s.root.innerHTML=section('仓库收货',s.mode==='server'?`<p class="rx-tip">登录后可查看获授权的仓库和采购单。</p>${button('登录工作台','login')}`:'<p class="rx-tip">仓库收货需要连接服务器，并使用获授权的流程账号登录。离线示例提供库存办理表单。</p>');
      return;
    }
    const hasOrders=ordersOf(s).length>0;
    let body='';
    if(s.loading)body=section('正在读取','<p class="rx-tip">正在加载仓库单据…</p>');
    else if(!s.ready)body=section('仓库数据待加载',button('重新加载','refresh'));
    else if(s.tab==='access'||!s.scopes.length)body=access(s);
    else if(s.tab==='scan')body=scanView(s);
    else if(s.tab==='ledger')body=ledger(s);
    else if(s.tab==='receiving')body=receiving(s);
    else body=purchase(s);
    s.root.innerHTML=`<div class="rx-top"><div class="rx-head"><h2>仓库收货</h2>${button('刷新单据','refresh')}</div>
      <p class="rx-tip">按店铺和仓库办理，一张采购单可包含多个 SKU。</p>
      <div class="rx-selectors"><label>收货仓库<select data-rx-select="scope" ${s.scopes.length?'':'disabled'}>${s.scopes.length?s.scopes.map(w=>option(w.id,scopeCaption(w),s.scopeId)).join(''):option('','暂无已授权仓库','')}</select></label>
      <label>采购单<select data-rx-select="order" ${hasOrders?'':'disabled'}>${hasOrders?s.orders.map(o=>option(o.id,o.number+' · '+o.supplier,s.order?.id)).join(''):option('','暂无采购单','')}</select></label></div>
      ${s.nextOffset!=null?button('加载更多采购单','more-orders'):''}</div>
      <nav class="rx-nav" aria-label="仓库收货环节">${Object.entries(tabs).map(([id,title])=>`<button type="button" data-rx="tab" data-tab="${id}" aria-pressed="${s.tab===id}">${title}</button>`).join('')}</nav>
      <div data-rx-feedback class="rx-feedback" hidden></div>
      ${s.tab==='purchase'&&s.scopeId&&s.user.is_admin&&!s.createOrder&&s.ready?'<div class="rx-actions" style="margin-bottom:10px">'+button('新建采购单','new-order')+'</div>':''}${body}`;
    tell(s,s.message,s.error);
    if(s.loading||s.writing)lock(s,true);
  }
  function lock(s,disabled){
    s.root.querySelectorAll('button,select,input,textarea').forEach(el=>{
      if(disabled&&!el.disabled){el.dataset.rxLocked='1';el.disabled=true;}
      else if(!disabled&&el.dataset.rxLocked){delete el.dataset.rxLocked;el.disabled=false;}
    });
  }
  async function load(s,scopeId=s.scopeId,orderId=s.order?.id,noticeId=''){
    const revision=++s.revision;
    const live=()=>s===current&&revision===s.revision;
    s.loading=true;s.ready=false;s.order=null;s.orders=[];s.nextOffset=null;draw(s);
    try{
      const [scopes,people]=await Promise.all([s.api(base+'/scopes'),s.api('/api/workflow-auth/members')]);
      if(!live())return;
      s.scopes=scopes.scopes;s.people=people;
      if(scopeId&&orderId&&!s.scopes.some(w=>w.id===scopeId))throw new Error('当前账户无权查看指定仓库。');
      s.scopeId=s.scopes.some(w=>w.id===scopeId)?scopeId:s.scopes[0]?.id||'';
      if(s.scopeId){
        const result=await s.api(base+'/orders?scope_id='+encodeURIComponent(s.scopeId));if(!live())return;
        s.orders=result.items;s.nextOffset=result.next_offset;
        const target=orderId || s.orders[0]?.id;
        if(target){
          const detail=await s.api(base+'/orders/'+target);if(!live())return;
          if(detail.scope_id!==s.scopeId)throw new Error('该采购单与指定仓库不符。');
          s.order=detail;if(!s.orders.some(o=>o.id===target))s.orders.push(detail);
          if(noticeId&&!detail.notices.some(n=>n.id===noticeId)){
            s.tab='purchase';s.noticeId='';tell(s,'目标通知不属于当前采购单，请核对来源。',true);
          }
        }
      }else{s.tab='access';s.newScope=true;}
      s.ready=true;
    }catch(err){if(live()){s.scopes=[];s.scopeId='';s.orders=[];s.order=null;tell(s,'读取失败。'+err.message,true);}}
    finally{if(live()){
      s.loading=false;draw(s);
      if(s.tab==='scan')s.root.querySelector('[data-rx-form=scan] input')?.focus();
    }}
  }

  async function pickOrder(s,id){
    const revision=++s.revision;s.loading=true;draw(s);
    try{const result=await s.api(base+'/orders/'+id);
      if(current===s&&s.revision===revision)s.order=result;
    }catch(err){if(current===s&&s.revision===revision){s.order=null;tell(s,err.message,true);}}
    finally{if(current===s&&s.revision===revision){s.loading=false;draw(s);}}
  }
  async function moreOrders(s){
    if(s.nextOffset==null)return;
    const revision=++s.revision;s.loading=true;draw(s);
    try{const result=await s.api(base+'/orders?scope_id='+encodeURIComponent(s.scopeId)+'&offset='+s.nextOffset);
      if(current!==s||s.revision!==revision)return;
      const known=new Set(s.orders.map(o=>o.id));
      s.orders.push(...result.items.filter(o=>!known.has(o.id)));s.nextOffset=result.next_offset;
    }catch(err){if(current===s&&s.revision===revision)tell(s,err.message,true);}
    finally{if(current===s&&s.revision===revision){s.loading=false;draw(s);}}
  }

  function readRows(form,fields,positive){
    return Array.from(form.querySelectorAll('[data-line]'),row=>{
      const result={line_id:row.dataset.line};
      for(const name of fields){
        const el=row.querySelector('[name="'+name+'"]');
        const number=Number(el?.value||0);
        if(!Number.isSafeInteger(number)||number<0)throw new Error('数量须为非负整数。');
        result[name]=number;
      }
      return result;
    }).filter(row=>positive.some(key=>row[key]>0));
  }
  function requestFor(s,form){
    const type=form.dataset.rxForm, data=new FormData(form), val=name=>String(data.get(name)||'').trim();
    const n=noticeIn(s), o=s.order;let path,body;
    if(type==='scope'){
      path='/scopes';body={warehouse:val('warehouse'),site:val('site'),business_timezone:val('business_timezone'),account_ref:val('account_ref'),member_ids:data.getAll('member')};
      if(s.newScope&&s.scopes.some(w=>w.account_ref===body.account_ref&&w.site===body.site.toUpperCase()&&w.warehouse===body.warehouse))
        throw new Error('该仓库已存在，请选中已有仓库并核对授权成员。');
    }else if(type==='order'){
      const lines=Array.from(form.querySelectorAll('[data-rx-order-row]'),row=>({sku:row.querySelector('[name=sku]').value.trim(),qty:Number(row.querySelector('[name=qty]').value)}));
      if(lines.some(l=>!l.sku||!Number.isSafeInteger(l.qty)||l.qty<=0))throw new Error('每个采购行须填写 SKU 与正整数数量。');
      if(new Set(lines.map(l=>l.sku)).size!==lines.length)throw new Error('一张采购单内的 SKU 不能重复，请合并数量。');
      path='/orders';body={scope_id:s.scopeId,number:val('number'),supplier:val('supplier'),lines};
    }else if(type==='notice'){
      path='/orders/'+o.id+'/notices';body={lines:readRows(form,['qty'],['qty'])};
    }else if(type==='receive'){
      const quick=s.receiveMode==='quick';
      const lines=readRows(form,quick?['qty','good','bad']:['qty'],quick?['qty','good','bad']:['qty']);
      if(quick&&lines.some(l=>l.qty!==l.good+l.bad))throw new Error('快捷收货的实收数量必须等于良品与次品之和。');
      path='/notices/'+n.id+'/receive';body={scope_id:s.scopeId,mode:s.receiveMode,lines};
    }else if(type==='quality'){
      path='/notices/'+n.id+'/quality';body={mode:s.qualityMode,lines:readRows(form,['good','bad'],['good','bad']),note:val('note')};
    }else if(type==='cancel'||type==='reverse'){
      path=type==='cancel'?'/notices/'+n.id+'/cancel':'/events/'+s.reverseId+'/reverse';body={reason:val('reason')};
      if(!body.reason)throw new Error('请填写原因。');
    }else throw new Error('未知的收货操作。');
    if(body.lines&&!body.lines.length)throw new Error('至少填写一行大于零的数量。');
    return {type,path,body};
  }
  // Keep the same key if a response is lost, including after a refresh. Clear only confirmed outcomes.
  function pendingRequest(s,path,body){
    const scope=path==='/orders'?s.scopeId:path==='/scopes'?[body.account_ref,body.site,body.warehouse].join('|'):'';
    const key='homa.erp-receiving.pending.'+JSON.stringify([s.user.id,path,scope]);
    const payload=JSON.stringify(body);
    let stored=fallbackTokens.get(key);
    if(!stored){try{stored=JSON.parse(sessionStorage.getItem(key)||'null');}catch{}}
    if(!stored){
      stored={id:crypto.randomUUID(),payload};
      fallbackTokens.set(key,stored);
      try{sessionStorage.setItem(key,JSON.stringify(stored));}catch{}
    }
    return {id:stored.id,payload:stored.payload,clear(){
      fallbackTokens.delete(key);try{sessionStorage.removeItem(key);}catch{}
    }};
  }
  async function write(s,form,replay=false){
    if(s.loading||s.writing||current!==s)return;
    let request;
    try{request=requestFor(s,form);}catch(err){tell(s,err.message,true);return;}
    const {type,path}=request;let body=request.body;
    const pending=pendingRequest(s,path,body);
    if(pending.payload!==JSON.stringify(body)){
      if(!replay){
        tell(s,'上次提交的结果尚未确认。请核对上次提交，再办理不同数量或内容。',true);
        const zone=s.root.querySelector('[data-rx-feedback]');
        const retry=document.createElement('button');retry.className='btn';retry.type='button';retry.textContent='核对上次提交';
        retry.addEventListener('click',()=>write(s,form,true),{once:true});zone.append(' ',retry);return;
      }
      body=JSON.parse(pending.payload);
    }
    s.writing=true;lock(s,true);tell(s,'正在提交…');
    try{
      const result=await s.api(base+path,{method:'POST',body:JSON.stringify({...body,request_id:pending.id})});
      pending.clear();
      if(s!==current)return;
      s.createOrder=false;s.newScope=false;s.reverseId='';
      if(type==='scope'){
        tell(s,'仓库范围与授权已保存。');await load(s,result.id,null);return;
      }
      if(type==='order')s.orders=[result,...s.orders.filter(o=>o.id!==result.id)];
      if(type==='notice'){
        const old=new Set((s.order?.notices||[]).map(n=>n.id));
        s.noticeId=result.notices.find(n=>!old.has(n.id))?.id||result.notices.at(-1)?.id;s.tab='receiving';
      }
      s.order=result;
      const messages={order:'采购单已建立。',notice:'到货通知已建立。',receive:'收货已入账。',quality:'质检处理已入账。',cancel:'通知已取消。',reverse:'已登记冲销流水。'};
      tell(s,messages[type]);draw(s);
    }catch(err){
      if(err.status&&err.status>=400&&err.status<500)pending.clear();
      if(current===s)tell(s,err.status===409?'单据状态冲突。'+err.message+' 请刷新核对当前数量。':err.status&&err.status<500?err.message:'尚未确认提交结果。原数量可直接重试，系统会复用同一请求编号。',true);
    }finally{if(current===s){s.writing=false;lock(s,false);}}
  }
  function resetForms(s){s.barcode={code:'',checked:false,open:false,binding:null};s.scan={code:'',items:[],done:false};s.scanHit=null;s.createOrder=false;s.newScope=false;s.reverseId='';s.receiveMode='normal';s.qualityMode='inspect';}
  function click(s,event){
    const el=event.target.closest('[data-rx]');if(!el||s.loading||s.writing)return;
    switch(el.dataset.rx){
      case 'login':s.login();return;
      case 'tab':resetForms(s);s.tab=el.dataset.tab;if(!s.scopes.length)s.tab='access';break;
      case 'refresh':tell(s,'');load(s);return;
      case 'more-orders':moreOrders(s);return;
      case 'scan-more':scanCode(s,s.scan.code,true);return;
      case 'scan-pick':openScanned(s,s.scan.items[Number(el.dataset.index)]);return;
      case 'new-order':s.tab='purchase';s.createOrder=true;break;
      case 'close-order':s.createOrder=false;break;
      case 'new-scope':s.newScope=true;break;
      case 'close-scope':s.newScope=false;break;
      case 'open-notice':resetForms(s);s.tab='receiving';s.noticeId=el.dataset.id;break;
      case 'reverse':s.reverseId=el.dataset.id;break;
      case 'close-reverse':s.reverseId='';break;
      case 'add-line':el.closest('form').querySelector('tbody').insertAdjacentHTML('beforeend',orderRow());return;
      case 'remove-line':if(el.closest('tbody').rows.length>1)el.closest('tr').remove();else tell(s,'请保留至少一个采购行。',true);return;
      default:return;
    }
    tell(s,'');draw(s);
    if(s.tab==='scan')s.root.querySelector('[data-rx-form=scan] input')?.focus();
    if(s.reverseId)s.root.querySelector('[data-rx-form=reverse] input')?.focus();
  }
  function change(s,event){
    const el=event.target;if(!el.dataset.rxSelect||s.loading||s.writing)return;
    tell(s,'');
    switch(el.dataset.rxSelect){
      case 'scope':resetForms(s);s.noticeId='';load(s,el.value,null);return;
      case 'order':resetForms(s);s.noticeId='';pickOrder(s,el.value);return;
      case 'notice':resetForms(s);s.noticeId=el.value;break;
      case 'receive-mode':s.receiveMode=el.value;break;
      case 'quality-mode':s.qualityMode=el.value;break;
    }
    draw(s);
  }
  function mount(root,ctx){
    if(current?.root===root&&current.mode===ctx.mode&&current.user?.id===ctx.user?.id)return;
    unmount();
    const s={...ctx,root,scopes:[],orders:[],people:[],order:null,scopeId:'',noticeId:'',tab:'purchase',revision:0,ready:false,loading:false,writing:false,message:'',error:false};
    resetForms(s);current=s;root.className='rx-workspace';
    const abort=new AbortController();s.abort=abort;
    root.addEventListener('click',ev=>click(s,ev),{signal:abort.signal});
    root.addEventListener('change',ev=>change(s,ev),{signal:abort.signal});
    root.addEventListener('input',ev=>{
      if(ev.target.matches('[data-rx-form=barcode-lookup] [name=fnsku]')){
        const edit=root.querySelector('[data-rx-form=barcode-save]');
        if(edit)edit.hidden=ev.target.value.trim()!==s.barcode.code;
      }
    },{signal:abort.signal});
    root.addEventListener('submit',ev=>{
      const form=ev.target;if(!form.dataset.rxForm)return;ev.preventDefault();
      if(form.dataset.rxForm==='scan')scanCode(s,form.elements.code.value);
      else if(['barcode-lookup','barcode-save'].includes(form.dataset.rxForm))barcodeAction(s,form);
      else write(s,form);
    },{signal:abort.signal});
    if(ctx.mode==='server'&&ctx.user)load(s);else draw(s);
  }
  function unmount(){current?.abort.abort();current=null;}
  function openOrder(scopeId,orderId,noticeId=''){
    const s=current;if(!s||!s.user||s.mode!=='server'||s.writing)return false;
    resetForms(s);s.tab=noticeId?'receiving':'purchase';s.noticeId=noticeId;tell(s,'');
    load(s,scopeId,orderId,noticeId);return true;
  }
  return {mount,unmount,openOrder};
})();
