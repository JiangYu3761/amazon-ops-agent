/* Reconcile external payout statements in their original account, site and currency. */
(() => {
  'use strict';
  const base='/api/erp-settlements/statements';
  const kindName={sale:'销售',fee:'费用',refund:'退款',adjustment:'有凭证调整',reversal:'本地冲正'};
  const shown=value=>value===null?'待明细':e(value);
  const statusName={pending_entries:'待导入明细',balanced:'金额相符',difference:'有差额'};
  const action=(name,cmd,attributes='')=>`<button type="button" class="btn" data-stl="${cmd}" ${attributes}>${e(name)}</button>`;
  const field=(label,name,attrs='',value='')=>`<label class="field">${e(label)}<input name="${name}" value="${e(value??'')}" ${attrs}></label>`;
  const ref='maxlength="150" required', cash='type="number" step="any" required';
  const nav=(prefix,offset,next)=>`<div class="rfd-actions">${offset?action('上一页',prefix+'-prev'):''}${next!==null?action('下一页',prefix+'-next'):''}</div>`;
  let dialog,panel,statement,listOffset,entryOffset,importOffset,revisionOffset,filter,serial=0,pending=false,alertMessage='';
  function show(html){panel.innerHTML=html+'<p role="alert" class="rfd-error"></p>';if(alertMessage){note(alertMessage);alertMessage='';}}
  function note(msg){panel.querySelector('[role="alert"]').textContent=msg;}
  function resetPages(){entryOffset=importOffset=revisionOffset=0;}
  function open(){
    if(dialog?.open)return;
    filter={account_ref:'',currency:'',start:'',end:''};listOffset=0;resetPages();statement=null;
    const d=document.createElement('dialog');dialog=d;d.className='wb-dialog rfd-dialog';d.setAttribute('aria-labelledby','stl-title');
    d.innerHTML='<header class="wb-dialog-head"><h2 id="stl-title">店铺结算对账</h2>'+action('关闭','close')+'</header><div class="wb-dialog-body"></div>';
    panel=d.querySelector('.wb-dialog-body');document.body.append(d);d.showModal();
    d.addEventListener('close',()=>{d.remove();if(dialog===d){dialog=null;serial++;}},{once:true});
    d.addEventListener('cancel',ev=>{if(pending)ev.preventDefault();});
    d.addEventListener('click',click);d.addEventListener('submit',submit);
    if(mode==='demo'){show('<h3>发放日对账示例</h3><p>期初 10.00 USD，销售 +100.00，扣费 −15.00，上月订单本期退款 −20.00，期末留存 5.00。应打款 70.00 USD，实打 68.00 USD，差额 −2.00 USD。</p><p>连接自有服务器后可导入自己的结算 CSV。</p>');return;}
    if(!window.processUI?.currentUser()){show('<p>登录协作账户后可登记和核对自己的结算单。</p>'+action('登录协作账户','login'));return;}
    list().catch(err=>{if(dialog?.open)note(err.message);});
  }
  function listForm(){
    return `<form data-stl-form="filter"><div class="rfd-fields">${field('店铺账号','account_ref','maxlength="150"',filter.account_ref)}
      ${field('币种','currency','maxlength="3"',filter.currency)}${field('发放日起','start','type="date"',filter.start)}${field('发放日至','end','type="date"',filter.end)}</div>
      <div class="rfd-actions"><button class="btn">筛选</button>${action('登记结算单','new')}</div></form>`;
  }
  async function list(){
    const token=++serial;statement=null;const params=new URLSearchParams({limit:'25',offset:String(listOffset)});
    for(const [k,v] of Object.entries(filter))if(v)params.set(k,v);
    show(listForm()+'<p>正在读取结算单…</p>');
    const result=await api(base+'?'+params);
    if(token!==serial||!dialog?.open)return;
    const totals=result.currency_totals.map(t=>`<p><b>${e(t.currency)}</b> · ${t.statement_count} 单，待明细 ${t.pending_count} 单 · 有明细单据应打 ${shown(t.expected)} · 实打 ${shown(t.actual)} · 差额 ${shown(t.difference)}${t.pending_count?' · 待明细实打 '+e(t.pending_actual):''}</p>`).join('');
    const rows=result.items.map(r=>`<li><div><b>${e(r.statement_ref)} · ${e(r.currency)}</b><span>${e(r.account_ref)} / ${e(r.site)} · 发放日 ${e(r.payout_date)}</span><small>${e(statusName[r.state])} · 差额 ${shown(r.difference)}</small></div>${action('核对明细','open',`data-id="${e(r.id)}"`)}</li>`).join('');
    show(listForm()+(totals?'<h3>按币种汇总当前筛选范围</h3>'+totals:'')+(rows?'<ul class="rfd-list">'+rows+'</ul>':'<p>当前范围内暂无结算单。</p>')+nav('list',listOffset,result.next_offset));
  }
  function editor(row){
    ++serial;
    const identity=row?'':field('店铺账号标识','account_ref',ref)+field('站点','site','required maxlength="4" placeholder="US"')+field('币种','currency','required minlength="3" maxlength="3" placeholder="USD"')+field('结算单编号','statement_ref',ref);
    show(action('返回',row?'detail':'list')+`<h3>${row?'修订结算单头':'登记结算单'}</h3><form data-stl-form="${row?'revise':'new'}">
      <div class="rfd-fields">${identity}${field('实际发放日期','payout_date','type="date" required',row?.payout_date)}
      ${field('期初余额','opening_balance',cash,row?.opening_balance)}${field('期末留存','closing_balance',cash,row?.closing_balance)}
      ${field('实际打款金额','actual_payout',cash,row?.actual_payout)}${field('到账或扣款凭证编号','payout_ref',ref,row?.payout_ref)}
      ${row?field('修订原因','reason',ref):''}</div><div class="rfd-actions"><button class="btn primary">保存结算单</button></div></form>`);
  }
  async function load(id){
    const token=++serial,params=new URLSearchParams({limit:'25',offset:String(entryOffset),import_offset:String(importOffset),revision_offset:String(revisionOffset)});
    const result=await api(base+'/'+id+'?'+params);
    if(token===serial&&dialog?.open){statement=result;draw();}
  }
  function draw(){
    const s=statement,groups=s.categories.map(g=>e(kindName[g.kind])+' '+e(g.total)).join(' · ');
    const rows=s.entries.items.map(t=>`<li><div><b>${e(kindName[t.kind])} · ${e(t.amount)} ${e(s.currency)}</b><span>${e(t.entry_ref)} · 交易日 ${e(t.occurred_on)}${t.original_order_date?' · 原单日 '+e(t.original_order_date):''}</span><small>${e(t.filename||t.reason||'')} · ${e(t.recorded_by)}${t.reversed?' · 已有冲正':''}</small></div>${t.kind==='fee'?`<button type="button" class="btn" data-erp-fee-open data-statement="${e(s.id)}" data-entry="${e(t.id)}">费用分摊</button>`:''}${!t.reversed&&t.kind!=='reversal'?action('冲正','reverse',`data-id="${e(t.id)}"`):''}</li>`).join('');
    const imports=s.imports.items.map(i=>`<li><div><b>${e(i.filename)}</b><span>新增 ${i.added} 行 · 已存在 ${i.skipped} 行 · ${e(i.actor_name)}</span><small>SHA-256 ${e(i.sha256)}</small></div></li>`).join('');
    const history=s.revisions.items.map(r=>`<li><div><b>版本 ${r.version} · ${e(r.reason)}</b><span>${e(r.snapshot.payout_date)} · 实打 ${e(r.snapshot.actual_payout)} · 凭证 ${e(r.snapshot.payout_ref)}</span><small>期初 ${e(r.snapshot.opening_balance)} · 期末 ${e(r.snapshot.closing_balance)} · ${e(r.actor_name)}</small></div></li>`).join('');
    show(action('返回结算单','list')+`<h3>${e(s.statement_ref)} · ${e(statusName[s.state])}</h3><p>${e(s.account_ref)} / ${e(s.site)} · ${e(s.currency)} · 发放日 ${e(s.payout_date)}</p>
      <p>期初 ${e(s.opening_balance)} ＋ 已导入交易净额 ${shown(s.transactions_net)} − 期末留存 ${e(s.closing_balance)} ＝ 应打款 <b>${shown(s.expected_payout)}</b>。</p>
      <p>实打 ${e(s.actual_payout)}，差额（实打减应打）<b>${shown(s.difference)} ${e(s.currency)}</b>。到账凭证 ${e(s.payout_ref)}。</p>
      <div class="rfd-actions">${action('修订单头','edit')}${action('下载 CSV 模板','template')}<button class="btn" type="button" data-erp-fee-summary data-statement="${e(s.id)}">本单费用归集</button></div>
      <form data-stl-form="import"><label class="field">外部结算明细 CSV<input type="file" name="file" accept=".csv,text/csv" required></label>
      <small>UTF-8，每批最多 1,000 行、1 MiB。填写来源报表的唯一行引用。kind 填 sale（销售正额）、fee（费用负额）、refund（退款负额）或 adjustment（有依据的调整）。日期填 YYYY-MM-DD，原单日可留空。所有行归入本单发放日。</small>
      <div class="rfd-actions"><button class="btn primary">核验并导入</button></div></form>
      <h3>交易明细 · ${s.entry_count} 行</h3><p>${groups}</p>
      ${rows?'<ul class="rfd-list">'+rows+'</ul>':'<p>请从外部结算报表录入明细。</p>'}${nav('entries',entryOffset,s.entries.next_offset)}
      <details><summary>导入记录</summary><ul class="rfd-list">${imports}</ul>${nav('imports',importOffset,s.imports.next_offset)}</details>
      <details><summary>单头修订历史</summary><ul class="rfd-list">${history}</ul>${nav('revisions',revisionOffset,s.revisions.next_offset)}</details>`);
  }
  function template(){
    const csv='account_ref,site,currency,statement_ref,entry_ref,kind,occurred_on,original_order_date,amount\r\n';
    const url=URL.createObjectURL(new Blob(['\ufeff',csv],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');
    a.href=url;a.download='settlement-entries.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function click(ev){
    const b=ev.target.closest('[data-stl]');if(!b||pending)return;
    const cmd=b.dataset.stl;
    try{
      if(cmd==='close')dialog.close();
      else if(cmd==='login'){dialog.close();window.processUI.login();}
      else if(cmd==='list')await list();
      else if(cmd==='new')editor(null);
      else if(cmd==='open'){resetPages();await load(b.dataset.id);}
      else if(cmd==='detail')await load(statement.id);
      else if(cmd==='edit')editor(statement);
      else if(cmd==='template')template();
      else if(cmd==='reverse'){
        const entry=statement.entries.items.find(t=>t.id===b.dataset.id);
        show(action('返回明细','detail')+`<h3>登记冲正</h3><p>原行 ${e(entry.entry_ref)} 的 ${e(entry.amount)} ${e(statement.currency)} 将由本地反向流水抵消。更正金额可用新行引用导入。</p>
          <form data-stl-form="reverse" data-id="${e(entry.id)}">${field('冲正原因','reason',ref)}<div class="rfd-actions"><button class="btn primary">确认冲正</button></div></form>`);
      }else{
        const [section,dir]=cmd.split('-'),n=dir==='next'?1:-1;
        if(section==='list'){listOffset+=n*25;await list();}
        else{if(section==='entries')entryOffset+=n*25;else if(section==='imports')importOffset+=n*10;else revisionOffset+=n*10;await load(statement.id);
          if(section==='imports'||section==='revisions')panel.querySelectorAll('details')[section==='imports'?0:1].open=true;}
      }
    }catch(err){if(dialog?.open)note(err.message);}
  }
  async function submit(ev){
    ev.preventDefault();if(pending)return;
    const f=ev.target,data=new FormData(f),body=Object.fromEntries(data),cmd=f.dataset.stlForm;
    pending=true;if(ev.submitter)ev.submitter.disabled=true;
    try{
      if(cmd==='filter'){filter=body;listOffset=0;await list();return;}
      if(cmd==='import'){
        const result=await api(base+'/'+statement.id+'/imports',{method:'POST',body:data});
        alertMessage=result.replayed?'该文件已导入，保留原交易。':`导入完成，新增 ${result.added} 行，已有 ${result.skipped} 行。`;
        resetPages();await load(statement.id);return;
      }
      let path=base,method='POST';
      if(cmd==='revise'){path+='/'+statement.id;method='PATCH';body.expected_version=statement.version;}
      if(cmd==='reverse')path+='/'+statement.id+'/entries/'+f.dataset.id+'/reversal';
      const result=await api(path,{method,body:JSON.stringify(body)});resetPages();statement=result;draw();
    }catch(err){if(dialog?.open)note(err.message);}
    finally{pending=false;if(ev.submitter)ev.submitter.disabled=false;}
  }
  document.addEventListener('click',ev=>{if(ev.target.closest('[data-erp-settlements-open]'))try{open();}catch(error){if(dialog?.open)note(error.message);}});
})();
