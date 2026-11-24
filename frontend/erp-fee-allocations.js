(() => {
  'use strict';
  const button=(text,cmd,attrs='')=>`<button class="btn" type="button" data-fee="${cmd}" ${attrs}>${e(text)}</button>`;
  const identity=t=>t.product_id+'|'+(t.sku||'');
  const label=t=>t.model+(t.sku?' / '+t.sku:'');
  let dialog,panel,sid,eid,data,basis,search,offset,historyOffset,summaryOffset,choices,selected,reason,preview,pending,serial=0;
  const path=()=>'/api/erp-settlements/statements/'+sid;
  const endpoint=()=>path()+'/entries/'+eid+'/allocations';
  const message=m=>{if(panel?.isConnected)panel.querySelector('[role="alert"]').textContent=m;};
  function draw(html){panel.innerHTML=html+'<p class="rfd-error" role="alert"></p>';}
  function table(rows,currency){
    return '<ul class="rfd-list">'+rows.map(t=>`<li><div><b>${e(label(t))}</b>${t.weight?'<small>权重 '+e(t.weight)+'</small>':''}</div><b>${e(t.expense)} ${e(currency)}</b></li>`).join('')+'</ul>';
  }
  function paging(prefix,here,next){
    return '<div class="rfd-actions">'+(here?button('上一页',prefix+'-prev'):'')+(next!==null?button('下一页',prefix+'-next'):'')+'</div>';
  }
  async function open(el,summary){
    if(dialog?.open)return;
    sid=el.dataset.statement;eid=el.dataset.entry;summaryOffset=historyOffset=0;preview=null;
    const d=document.createElement('dialog');dialog=d;d.className='wb-dialog rfd-dialog';d.setAttribute('aria-labelledby','fee-title');
    d.innerHTML='<header class="wb-dialog-head"><h2 id="fee-title">'+(summary?'本单费用归集':'产品费用分摊')+'</h2>'+button('关闭','close')+'</header><div class="wb-dialog-body"></div>';
    document.body.append(d);panel=d.querySelector('.wb-dialog-body');draw('<p>正在读取费用记录…</p>');d.showModal();
    d.addEventListener('close',()=>{d.remove();if(dialog===d){serial++;dialog=null;panel=null;}},{once:true});
    d.addEventListener('cancel',ev=>{if(pending)ev.preventDefault();});
    d.addEventListener('click',click);d.addEventListener('submit',submit);
    d.addEventListener('input',ev=>{
      if(ev.target.name==='reason')reason=ev.target.value;
      if(ev.target.hasAttribute('data-fee-weight'))selected.get(ev.target.dataset.feeWeight).weight=ev.target.value;
    });
    d.addEventListener('change',ev=>{
      if(ev.target.name==='basis'){
        basis=ev.target.value;selected.clear();offset=0;search='';
        fetchChoices().catch(err=>message(err.message));
      }
    });
    try{if(summary)await overview();else await loadHistory();}catch(err){message(err.message);}
  }
  async function overview(){
    const token=++serial,r=await api(path()+'/allocation-summary?offset='+summaryOffset);
    if(token!==serial||!dialog?.open)return;
    draw(`<h3>${e(r.payout_date)} · ${e(r.currency)}</h3><p>本结算单当前生效的产品和 SKU 费用。</p>`+
      (r.items.length?table(r.items,r.currency):'<p>尚无生效的费用分摊。</p>')+paging('summary',summaryOffset,r.next_offset));
  }
  async function loadHistory(){
    const token=++serial,r=await api(endpoint()+'?offset='+historyOffset);
    if(token!==serial||!dialog?.open)return;
    data=r;showHistory();
  }
  function showHistory(){
    const current=data.active;
    const events=data.history.items.map(x=>`<details><summary>版本 ${x.version} · ${x.action==='post'?'入账':'冲正'} · ${e(x.expense)} ${e(x.currency)}</summary>
      <p>${e(x.payout_date)} · ${e(x.actor_name)} · ${e(x.reason)}</p>${table(x.targets,x.currency)}</details>`).join('');
    draw(`<h3>费用行 ${e(data.entry_ref)}</h3><p>发放日 ${e(data.payout_date)} · 原费用流水 ${e(data.source_amount)} ${e(data.currency)} · 待归集费用 ${e(data.expense)} ${e(data.currency)}</p>`+
      (current?'<h3>当前分摊</h3>'+table(current.targets,data.currency)+button('冲正本次分摊','void-form'):
       data.source_reversed?'<p>原费用已冲正。</p>':button('选择分摊对象','new'))+
       '<h3>分摊履历</h3>'+(events||'<p>暂无入账记录。</p>')+paging('history',historyOffset,data.history.next_offset));
  }
  async function fetchChoices(){
    const token=++serial,params=new URLSearchParams({basis,search,limit:'40',offset:String(offset)});
    const result=await api(path()+'/allocation-targets?'+params);
    if(token===serial&&dialog?.open){choices=result;edit();}
  }
  function edit(){
    const rows=choices.items.map((t,index)=>`<li><span>${e(label(t))}</span>${selected.has(identity(t))?'<small>已选</small>':button('加入','add','data-index="'+index+'"')}</li>`).join('');
    const picks=[...selected].map(([k,t])=>`<li><label class="field">${e(label(t))} · 权重
      <input type="number" min="0.000001" max="1000000000" step="0.000001" data-fee-weight="${e(k)}" value="${e(t.weight)}" required></label>${button('移除','remove','data-key="'+e(k)+'"')}</li>`).join('');
    draw(button('返回履历','history')+`<h3>选择分摊对象</h3>
      <label class="field">分摊维度<select name="basis"><option value="product" ${basis==='product'?'selected':''}>产品</option><option value="sku" ${basis==='sku'?'selected':''}>SKU</option></select></label>
      <form data-fee-form="search"><label class="field">搜索产品型号、名称或 SKU<input name="search" value="${e(search)}" maxlength="100"></label><button class="btn">搜索</button></form>
      ${rows?'<ul class="rfd-list">'+rows+'</ul>':'<p>没有符合当前店铺、站点与币种的商品。</p>'}${paging('options',offset,choices.next_offset)}
      <form data-fee-form="preview"><h3>已选对象与权重</h3><ul class="rfd-list">${picks}</ul>
       <label class="field">权重依据<input name="reason" required maxlength="150" value="${e(reason)}"></label>
       <div class="rfd-actions"><button class="btn primary" ${selected.size?'':'disabled'}>计算预览</button></div></form>`);
  }
  function showPreview(){
    draw(button('修改权重','edit')+`<h3>费用分摊预览</h3><p>费用 ${e(preview.expense)} ${e(preview.currency)} · 分摊合计 ${e(preview.allocated)} ${e(preview.currency)}</p>
      <p>先分配整数最小货币单位，余数较大的对象优先获得尾差。余数相同时按产品 ID、SKU 排序。</p>${table(preview.targets,preview.currency)}
      ${button('确认分摊入账','post')}`);
  }
  async function click(ev){
    const b=ev.target.closest('[data-fee]');if(!b||pending)return;const cmd=b.dataset.fee;
    try{
      if(cmd==='close')dialog.close();
      else if(cmd==='new'){basis='product';selected=new Map();search='';offset=0;reason='';await fetchChoices();}
      else if(cmd==='edit')edit();
      else if(cmd==='add'){const t=choices.items[Number(b.dataset.index)];selected.set(identity(t),{...t,weight:'1'});edit();}
      else if(cmd==='remove'){selected.delete(b.dataset.key);edit();}
      else if(cmd==='history')await loadHistory();
      else if(cmd==='void-form')draw(button('返回履历','history')+`<h3>冲正分摊版本 ${data.active.version}</h3><form data-fee-form="void"><label class="field">冲正依据<input name="void_reason" required maxlength="150"></label><div class="rfd-actions"><button class="btn primary">确认冲正分摊</button></div></form>`);
      else if(cmd==='post'){
        pending=true;b.disabled=true;
        data=await api(endpoint()+'/post',{method:'POST',body:JSON.stringify({preview_id:preview.preview_id,request_id:preview.request_id})});
        historyOffset=0;showHistory();
      }else if(cmd.startsWith('history-')){historyOffset+=cmd.endsWith('next')?20:-20;await loadHistory();}
      else if(cmd.startsWith('options-')){offset+=cmd.endsWith('next')?40:-40;await fetchChoices();}
      else if(cmd.startsWith('summary-')){summaryOffset+=cmd.endsWith('next')?40:-40;await overview();}
    }catch(err){message(err.message);}finally{pending=false;b.disabled=false;}
  }
  async function submit(ev){
    ev.preventDefault();if(pending)return;const f=ev.target,cmd=f.dataset.feeForm;
    pending=true;if(ev.submitter)ev.submitter.disabled=true;
    try{
      if(cmd==='search'){search=f.elements.search.value;offset=0;await fetchChoices();}
      else if(cmd==='void'){
        const input={expected_version:data.active.version,reason:f.elements.void_reason.value,request_id:crypto.randomUUID()};
        data=await api(endpoint()+'/void',{method:'POST',body:JSON.stringify(input)});historyOffset=0;showHistory();
      }else{
        const input={basis,reason,targets:[...selected.values()].map(({product_id,sku,weight})=>({product_id,sku,weight}))};
        preview=await api(endpoint()+'/preview',{method:'POST',body:JSON.stringify(input)});
        preview.request_id=crypto.randomUUID();showPreview();
      }
    }catch(err){message(err.message);}finally{pending=false;if(ev.submitter)ev.submitter.disabled=false;}
  }
  document.addEventListener('click',ev=>{
    const b=ev.target.closest('[data-erp-fee-open],[data-erp-fee-summary]');
    if(b)open(b,b.hasAttribute('data-erp-fee-summary'));
  });
})();
