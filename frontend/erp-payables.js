/* Supplier liabilities against posted receipt lines; payments are recorded from an external voucher. */
(() => {
  'use strict';
  const base='/api/erp-payables';
  const states={open:'未付',partial:'部分付款',paid:'已付清',void:'已作废'};
  const currencies=['USD','EUR','GBP','CNY','CAD','AUD','JPY'];
  const btn=(label,id,attrs='')=>`<button type="button" class="btn" data-ap="${id}" ${attrs}>${e(label)}</button>`;
  const submit=label=>`<button class="btn primary" type="submit">${e(label)}</button>`;
  const input=(label,name,attrs='')=>`<label class="field">${e(label)}<input name="${name}" ${attrs}></label>`;
  const option=(value,label,active)=>`<option value="${e(value)}"${value===active?' selected':''}>${e(label)}</option>`;
  const table=(head,rows)=>`<div class="wb-table-wrap"><table class="wb-table"><thead><tr>${head.map(h=>`<th scope="col">${e(h)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
  const money=(value,currency)=>e(value)+' '+e(currency);
  let active;
  function msg(s,message,error=false){
    s.message=message;s.error=error;
    const node=s.dialog.querySelector('[data-ap-message]');
    if(node){node.textContent=message;node.setAttribute('role',error?'alert':'status');node.hidden=!message;}
  }
  function lock(s,on){
    s.dialog.querySelectorAll('button,input,select,textarea').forEach(el=>{
      if(on&&el.dataset.ap==='close'&&!s.saving)return;
      if(on&&!el.disabled){el.disabled=true;el.dataset.apLocked='1';}
      else if(!on&&el.dataset.apLocked){delete el.dataset.apLocked;el.disabled=false;}
    });
  }
  function shell(s,body){
    const scopeOptions=s.scopes.map(w=>option(w.id,[w.warehouse,w.account_ref,w.site].join(' · '),s.scopeId)).join('');
    s.panel.innerHTML=`<div class="ap-controls"><label class="field">账务仓库<select data-ap-scope ${s.scopes.length?'':'disabled'}>${scopeOptions||option('','暂无授权仓库','')}</select></label>
      ${btn('刷新','refresh')}</div><p class="ap-message" data-ap-message hidden></p>${body}`;
    msg(s,s.message,s.error);lock(s,s.reading||s.saving);
  }
  function listView(s){
    const rows=s.bills.map(b=>`<tr><td><button type="button" class="ap-bill-link" data-ap="open" data-id="${e(b.id)}">${e(b.document_ref)}</button></td><td>${e(b.supplier)}</td>
      <td>${money(b.amount,b.currency)}</td><td>${money(b.paid,b.currency)}</td><td>${money(b.balance,b.currency)}</td><td>${e(states[b.status]||b.status)}</td></tr>`).join('');
    shell(s,`<div class="ap-controls"><h3>供应商账单</h3>${s.scopeId?btn('登记应付账单','new'):''}</div>
      ${rows?table(['供应商凭证','供应商','应付金额','已付','余额','状态'],rows):`<p class="ap-note">${s.scopeId?'该仓库尚无已登记账单。':'请联系管理员在仓库收货中分配店铺与仓库范围。'}</p>`}
      ${s.nextBills!=null?btn('加载更多账单','more-bills'):''}`);
  }
  function sourceRows(s,start=0){
    return s.sources.slice(start).map((l,i)=>`<tr><td><input type="checkbox" name="source" value="${start+i}" aria-label="${e(l.sku+' · '+l.notice_number)}"></td>
      <td>${e(l.supplier)}</td><td>${e(l.order_number)}</td><td>${e(l.notice_number)}</td><td>${e(l.sku)}</td><td>${e(l.received_qty)}</td></tr>`).join('');
  }
  function createView(s){
    shell(s,`<div class="ap-actions">${btn('返回台账','list')}</div><h3>登记应付账单</h3><p class="ap-note">选择同一供应商已实收、尚未登记应付的明细，按供应商凭证填写币种与总额。</p>
      <form data-ap-form="bill">${s.sources.length?table(['选入','供应商','采购单','收货通知','SKU','实收数量'],sourceRows(s)):'<p class="ap-note">本仓库当前没有可登记的收货明细。</p>'}
      ${s.nextSources!=null?btn('加载更多收货行','more-sources'):''}
      <div class="ap-fields">${input('供应商账单号／凭证号','document_ref','required maxlength="120"')}
      <label class="field">币种<input name="currency" list="ap-currencies" pattern="[A-Za-z]{3}" maxlength="3" required placeholder="币种代码"><datalist id="ap-currencies">${currencies.map(c=>option(c,c,'')).join('')}</datalist></label>
      ${input('应付总额','amount','required inputmode="decimal" autocomplete="off"')}${input('账单备注','note','maxlength="1000"')}</div>
      <div class="ap-actions">${s.sources.length?submit('建立应付账单'):''}</div></form>`);
  }
  function detailView(s){
    const b=s.detail;
    const src=(b.sources||[]).map(l=>`<tr><td>${e(l.order_number)}</td><td>${e(l.notice_number)}</td><td>${e(l.sku)}</td><td>${e(l.received_qty)}</td></tr>`).join('');
    const actorName=id=>ws.people?.find(person=>person.id===id)?.display_name||id||'—';
    const payments=(b.payments||[]).map(p=>`<tr><td>${e(p.paid_on)}</td><td>${e(p.external_ref)}</td><td>${money(p.amount,b.currency)}</td><td>${e(actorName(p.actor_id))}</td><td>${e(p.note||'—')}</td></tr>`).join('');
    const totals=[['应付',b.amount],['已付',b.paid],['未付余额',b.balance]];
    shell(s,`<div class="ap-actions">${btn('返回台账','list')}</div><h3>${e(b.supplier)} · ${e(b.document_ref)}</h3><p class="ap-note">${e(b.warehouse)} · ${e(b.account_ref)} · ${e(b.site)} · <strong>${e(states[b.status]||b.status)}</strong></p>
      <dl class="ap-money">${totals.map(([k,v])=>`<div><dt>${k}</dt><dd>${money(v,b.currency)}</dd></div>`).join('')}</dl>
      <h3>收货依据</h3>${table(['采购单','收货通知','SKU','实收数量'],src)}
      ${b.note?`<p class="ap-note">账单备注 · ${e(b.note)}</p>`:''}
      ${b.status==='void'?`<p class="ap-note">作废原因 · ${e(b.void_reason)} · ${e(actorName(b.void_by))}</p>`:''}
      <h3>分期付款记录</h3>${payments?table(['付款日期','外部付款流水号','登记金额','登记人','备注'],payments):'<p class="ap-note">尚无付款登记。</p>'}
      ${b.status==='open'&&s.admin?`<details><summary>作废未付款账单</summary><form data-ap-form="void"><p class="ap-note">作废后保留本单记录，并释放关联的收货明细。</p>${input('作废原因','reason','required maxlength="1000"')}<div class="ap-actions">${submit('确认作废')}</div></form></details>`:''}
      ${['paid','void'].includes(b.status)?'':`<h3>登记已付款项</h3><p class="ap-note">核对已发生的银行或支付机构流水后登记，可分多次付款。</p>
      <form data-ap-form="payment"><div class="ap-fields">${input('外部付款流水号','external_ref','required maxlength="120"')}
      ${input('本次付款额（'+b.currency+'）','amount','required inputmode="decimal" autocomplete="off"')}
      ${input('付款日期','paid_on','required type="date"')}${input('付款备注','note','maxlength="1000"')}</div><div class="ap-actions">${submit('登记付款')}</div></form>`}`);
  }
  function draw(s){
    if(active!==s)return;
    if(s.reading)shell(s,'<p class="ap-note">正在读取应付台账…</p>');
    else if(s.screen==='bill')createView(s);
    else if(s.screen==='detail'&&s.detail)detailView(s);
    else listView(s);
  }
  const addUnique=(existing,next,key)=>{const known=new Set(existing.map(key));return [...existing,...next.filter(v=>!known.has(key(v)))];};
  function params(s,offset=0){return '?scope_id='+encodeURIComponent(s.scopeId)+'&offset='+offset;}
  async function load(s,screen,options={}){
    const version=++s.version;s.reading=true;const valid=()=>active===s&&s.version===version;
    if(!options.more)draw(s);else lock(s,true);
    try{
      if(options.scopes){
        const data=await api('/api/erp-receiving/scopes');if(!valid())return;
        s.scopes=data.scopes;
        if(options.id&&s.scopeId&&!s.scopes.some(w=>w.id===s.scopeId)){s.scopeId=s.scopes[0]?.id||'';throw new Error('当前账户无权查看指定仓库。');}
        s.scopeId=s.scopes.some(w=>w.id===s.scopeId)?s.scopeId:s.scopes[0]?.id||'';
      }
      if(!s.scopeId){s.screen='list';s.bills=[];s.sources=[];s.detail=null;s.nextBills=null;return;}
      if(screen==='detail'){
        const b=await api(base+'/bills/'+options.id);if(!valid())return;
        if(b.scope_id!==s.scopeId)throw new Error('该账单与指定仓库不符。');s.detail=b;
      }else if(screen==='bill'){
        const result=await api(base+'/sources'+params(s,options.more?s.nextSources:0));if(!valid())return;
        const start=options.more?s.sources.length:0;s.sources=options.more?addUnique(s.sources,result.items,x=>x.event_id+':'+x.line_id):result.items;s.nextSources=result.next_offset;
        if(options.more){
          s.panel.querySelector('tbody')?.insertAdjacentHTML('beforeend',sourceRows(s,start));
          if(s.nextSources==null)s.panel.querySelector('[data-ap=more-sources]')?.remove();
        }
      }else{
        const result=await api(base+'/bills'+params(s,options.more?s.nextBills:0));if(!valid())return;
        s.bills=options.more?addUnique(s.bills,result.items,x=>x.id):result.items;s.nextBills=result.next_offset;
      }
      s.screen=screen;
    }catch(err){
      if(valid()){
        // Loading a new scope or opening a revoked bill must never expose data from the previous one.
        if(!options.more){s.screen='list';s.bills=[];s.sources=[];s.detail=null;s.nextBills=null;}
        msg(s,err.message,true);
      }
    }finally{if(valid()){s.reading=false;if(options.more&&screen==='bill')lock(s,false);else draw(s);}}
  }
  function amount(value){
    // The API applies the chosen currency's minor-unit precision. Keep the original decimal string.
    const v=value.trim();
    if(!/^\d{1,14}(?:\.\d{1,4})?$/.test(v)||!/[1-9]/.test(v))throw new Error('请输入正数金额，最多 14 位整数与 4 位小数。');
    return v;
  }
  async function save(s,form){
    if(s.reading||s.saving)return;
    const f=new FormData(form), value=n=>String(f.get(n)||'').trim();
    const isBill=form.dataset.apForm==='bill',voiding=form.dataset.apForm==='void';let body,path,wasRecorded=false;
    try{
      if(isBill){
        const rows=f.getAll('source').map(i=>s.sources[Number(i)]);if(!rows.length)throw new Error('请选择至少一行实收明细。');
        if(new Set(rows.map(r=>r.supplier)).size!==1)throw new Error('同一账单的收货明细须属于同一供应商。');
        path='/bills';body={scope_id:s.scopeId,document_ref:value('document_ref'),currency:value('currency').toUpperCase(),amount:amount(value('amount')),note:value('note'),sources:rows.map(({event_id,line_id})=>({event_id,line_id}))};
      }else if(voiding){
        path='/bills/'+s.detail.id+'/void';body={reason:value('reason')};if(!body.reason)throw new Error('请填写作废原因。');
      }else{
        path='/bills/'+s.detail.id+'/payments';body={external_ref:value('external_ref'),amount:amount(value('amount')),paid_on:value('paid_on'),note:value('note')};
        wasRecorded=s.detail.payments.some(p=>p.external_ref===body.external_ref);
      }
    }catch(err){msg(s,err.message,true);return;}
    const signature=JSON.stringify([path,body]);
    if(s.pending&&s.pending.signature!==signature){msg(s,'上次登记的响应尚未确认，请先按原内容重试，或刷新台账核对凭证。',true);return;}
    s.pending=s.pending||{signature,id:crypto.randomUUID()};
    s.saving=true;lock(s,true);msg(s,'正在登记…');
    try{
      const b=await api(base+path,{method:'POST',body:JSON.stringify({...body,request_id:s.pending.id})});
      if(active!==s)return;s.pending=null;s.detail=b;s.screen='detail';
      msg(s,isBill?'应付账单已登记。':voiding?'账单已作废，收货明细已释放。':wasRecorded?'相同付款流水已核对，余额未重复扣减。':'付款登记已确认，请核对账单余额。');
      draw(s);
    }catch(err){
      if(active===s){
        if(err.status&&err.status<500)s.pending=null;
        msg(s,err.status&&err.status<500?err.message:'登记结果尚未确认。请以原凭证重试，系统会复用请求编号。',true);
      }
    }finally{if(active===s){s.saving=false;lock(s,false);}}
  }
  function open(scopeId='',id=null){
    if(active?.dialog.open){
      if(id&&!active.saving&&mode==='server'&&window.processUI?.currentUser()){
        active.scopeId=scopeId;active.pending=null;msg(active,'');load(active,'detail',{scopes:true,id});
      }
      return;
    }
    const dialog=document.createElement('dialog');dialog.id='erpPayablesDialog';dialog.className='wb-dialog ap-dialog';
    dialog.setAttribute('aria-labelledby','ap-title');
    dialog.innerHTML=`<header class="wb-dialog-head"><h2 id="ap-title">供应商应付</h2>${btn('关闭','close')}</header><div class="wb-dialog-body"></div>`;
    document.body.append(dialog);
    const s={dialog,admin:Boolean(window.processUI?.currentUser()?.is_admin),panel:dialog.querySelector('.wb-dialog-body'),screen:'list',scopes:[],scopeId,bills:[],sources:[],detail:null,version:0,reading:false,saving:false,pending:null,message:'',error:false};
    active=s;
    dialog.addEventListener('cancel',ev=>{if(s.saving)ev.preventDefault();});
    dialog.addEventListener('close',()=>{if(active===s)active=null;dialog.remove();},{once:true});
    dialog.addEventListener('change',ev=>{
      if(ev.target.matches('[data-ap-scope]')&&!s.reading&&!s.saving){s.scopeId=ev.target.value;s.bills=[];s.sources=[];s.detail=null;s.nextBills=null;s.pending=null;msg(s,'');load(s,'list');}
    });
    dialog.addEventListener('submit',ev=>{if(ev.target.dataset.apForm){ev.preventDefault();save(s,ev.target);}});
    dialog.addEventListener('click',ev=>{
      const cmd=ev.target.closest('[data-ap]');if(!cmd||s.saving)return;
      if(cmd.dataset.ap==='close'){dialog.close();return;}
      if(cmd.dataset.ap==='login'){dialog.close();window.processUI?.login();return;}
      if(s.reading)return;msg(s,'');
      switch(cmd.dataset.ap){
        case 'refresh':s.pending=null;load(s,s.screen,{scopes:true,id:s.detail?.id});break;
        case 'list':s.pending=null;load(s,'list');break;
        case 'open':s.pending=null;load(s,'detail',{id:cmd.dataset.id});break;
        case 'new':s.pending=null;load(s,'bill');break;
        case 'more-bills':load(s,'list',{more:true});break;
        case 'more-sources':load(s,'bill',{more:true});break;
      }
    });
    dialog.showModal();
    if(mode!=='server')s.panel.innerHTML='<p>供应商应付需要连接服务器，以获授权的仓库收货流水和付款凭证办理。</p>';
    else if(!window.processUI?.currentUser())s.panel.innerHTML='<p>请使用有仓库权限的流程账户登录。</p>'+btn('登录工作台','login');
    else load(s,id?'detail':'list',{scopes:true,id});
  }
  window.erpPayables={openBill:(scopeId,id)=>open(scopeId,id)};
  document.addEventListener('click',ev=>{if(ev.target.closest('[data-erp-payables-open]'))open();});
})();
