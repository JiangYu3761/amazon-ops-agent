/* Purchase requisitions are versioned. Issuing an approved requisition creates its real receiving order. */
(() => {
  'use strict';
  const base='/api/erp-procurement/requests';
  const status={draft:'草稿',pending:'待独立审批',rejected:'已退回',approved:'已批准待签发',issued:'已签发采购单'};
  const actionLabels={create:'建立申请',edit:'修订申请',submit:'提交审批',approve:'批准',reject:'退回',issue:'签发采购单'};
  const btn=(title,action,attrs='')=>`<button type="button" class="btn" data-pr="${action}" ${attrs}>${e(title)}</button>`;
  const input=(label,name,value,attrs='')=>`<label class="field">${e(label)}<input name="${name}" value="${e(value||'')}" ${attrs}></label>`;
  const option=(value,label,selected)=>`<option value="${e(value)}"${value===selected?' selected':''}>${e(label)}</option>`;
  const table=(head,rows)=>`<div class="wb-table-wrap"><table class="wb-table"><thead><tr>${head.map(t=>`<th scope="col">${e(t)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
  const price=window.erpContractPricing;
  let active;
  function notice(s,message,error=false){
    s.message=message;s.error=error;const node=s.dialog.querySelector('[data-pr-msg]');
    if(node){node.textContent=message;node.setAttribute('role',error?'alert':'status');node.hidden=!message;}
  }
  function busy(s,value){
    s.dialog.querySelectorAll('button,select,input,textarea').forEach(el=>{
      if(value&&el.dataset.pr==='close'&&!s.saving)return;
      if(value&&!el.disabled){el.disabled=true;el.dataset.prLocked='1';}
      else if(!value&&el.dataset.prLocked){delete el.dataset.prLocked;el.disabled=false;}
    });
  }
  function render(s,body){
    if(s!==active)return;
    s.panel.innerHTML='<p class="ap-message" data-pr-msg hidden></p>'+body;notice(s,s.message,s.error);busy(s,s.loading||s.saving);
  }
  function line(item={}){
    const {sku='',qty=''}=item;
    return `<tr data-pr-line><td><input name="sku" required maxlength="120" aria-label="SKU" value="${e(sku)}"></td><td><input name="qty" type="number" min="1" max="1000000000" step="1" required aria-label="采购数量" value="${e(qty)}"></td>${price.inputs(item,e)}<td>${btn('移除','remove-line')}</td></tr>`;
  }
  function edit(s,d){
    s.screen=d?'edit':'create';
    const w=s.scopes.find(w=>w.id===s.scopeId);
    render(s,`<div class="ap-actions">${btn(d?'返回申请详情':'返回申请列表',d?'back-detail':'list')}</div><h3>${d?'修订 '+e(d.number):'新建采购申请'}</h3>
      <p class="ap-note">${e(w?.warehouse)} · ${e(w?.account_ref)} · ${e(w?.site)}</p>
      <form data-pr-form="edit"><div class="ap-fields">${input('供应商','supplier',d?.supplier,'required maxlength="120"')}
      <label class="field">采购事由<textarea name="reason" required maxlength="2000">${e(d?.reason||'')}</textarea></label></div>
      <p class="ap-note">按合同逐行填写币种、每件净价和税额。无合同价的行保留为空，到货后可凭单人工登记应付。</p>
      ${table(['SKU','申请数量',...price.columns,''],d?d.lines.map(line).join(''):line())}
      <div class="ap-actions">${btn('增加 SKU','add-line')}<button type="submit" class="btn primary">${d?'保存修订':'保存申请草稿'}</button></div></form>`);
  }
  function list(s){
    s.screen='list';const scope=s.scopes.map(w=>option(w.id,[w.warehouse,w.account_ref,w.site].join(' · '),s.scopeId)).join('');
    const rows=s.requests.map(r=>`<tr><td><button type="button" class="ap-bill-link" data-pr="open" data-id="${e(r.id)}">${e(r.number)}</button></td><td>${e(r.supplier)}</td>
      <td>${e(r.owner_name)}</td><td>${e(status[r.status]||r.status)}</td><td>${e(r.order_number||'—')}</td></tr>`).join('');
    render(s,`<div class="ap-controls"><label class="field">采购仓库<select data-pr-scope ${s.scopes.length?'':'disabled'}>${scope||option('','暂无授权仓库','')}</select></label>
      <label class="field">审批状态<select data-pr-status>${option('','全部状态',s.status)}${Object.entries(status).map(([k,v])=>option(k,v,s.status)).join('')}</select></label>${btn('刷新列表','refresh')}</div>
      <div class="ap-controls"><h3>采购申请列表</h3>${s.scopeId?btn('新建采购申请','new'):''}</div>${rows?table(['申请编号','供应商','申请人','状态','已签发采购单'],rows):`<p class="ap-note">${s.scopeId?'当前筛选没有采购申请。':'请联系管理员分配仓库权限。'}</p>`}${s.next!=null?btn('加载更多','more'):''}`);
  }
  function detail(s){
    s.screen='detail';const d=s.record;const owner=s.user.id===d.owner_id,editable=owner&&['draft','rejected'].includes(d.status);
    const lines=table(['SKU','申请数量','合同价'],d.lines.map(l=>`<tr><td>${e(l.sku)}</td><td>${e(l.qty)}</td><td>${e(price.description(l))}</td></tr>`).join(''));
    const zone=s.scopes.find(w=>w.id===d.scope_id)?.business_timezone;
    const events=table(['操作','经办人','仓库时间','意见','历史版本'],(d.events||[]).map(ev=>{
      const v=ev.snapshot;
      const snapshot=v?`<details><summary>第 ${e(ev.version)} 版</summary><small>${e(v.supplier)} · ${e(v.reason)}</small><ul>${v.lines.map(l=>`<li>${e(l.sku)} × ${e(l.qty)} · ${e(price.description(l))}</li>`).join('')}</ul></details>`:'—';
      return `<tr><td>${e(actionLabels[ev.action]||ev.action)}</td><td>${e(ev.actor_name||ev.actor_id)}</td><td>${e(new Date(ev.created_at).toLocaleString('zh-CN',{timeZone:zone}))}</td><td>${e(ev.note||'—')}</td><td>${snapshot}</td></tr>`;
    }).join(''));
    let actions='';
    if(editable)actions=btn('修订申请','edit')+btn('提交审批','submit');
    if(d.status==='pending'&&s.user.is_admin&&!owner)actions=`<form data-pr-form="decision">${input('审批意见（退回时必填）','note','','maxlength="2000"')}<div class="ap-actions"><button type="submit" class="btn primary" value="approve">批准申请</button><button type="submit" class="btn" value="reject">退回修订</button></div></form>`;
    if(d.status==='pending'&&owner)actions='<p class="ap-note">申请已提交，等待其他管理员独立审批。</p>';
    if(d.status==='approved'&&s.user.is_admin)actions=`<form data-pr-form="issue">${input('采购单号（可留空，由系统生成）','order_number','','maxlength="120"')}<div class="ap-actions"><button class="btn primary" type="submit">签发采购单</button></div></form>`;
    if(d.status==='issued'&&d.order_id)actions=`<div><strong>采购单 ${e(d.order_number)}</strong> ${btn('打开此采购单办理收货','receiving')}</div>`;
    render(s,`<div class="ap-actions">${btn('返回申请列表','list')}${btn('刷新详情','refresh')}</div><h3>${e(d.number)} · ${e(d.supplier)}</h3>
      <p class="ap-note">${e(d.warehouse)} · ${e(d.account_ref)} · ${e(d.site)} · 申请人 ${e(d.owner_name)} · <strong>${e(status[d.status])}</strong></p>
      <p>${e(d.reason)}</p>${lines}<div class="ap-actions">${actions}</div><h3>审批与签发记录</h3>${events}`);
  }
  async function load(s,id=null,{scopes=false,more=false}={}){
    const rev=++s.version;s.loading=true;busy(s,true);
    const live=()=>s===active&&rev===s.version;
    try{
      if(scopes){
        const result=await api('/api/erp-receiving/scopes');if(!live())return;s.scopes=result.scopes;
        if(id&&s.scopeId&&!s.scopes.some(w=>w.id===s.scopeId)){s.scopeId=s.scopes[0]?.id||'';throw new Error('当前账户无权查看指定仓库。');}
        s.scopeId=s.scopes.some(w=>w.id===s.scopeId)?s.scopeId:s.scopes[0]?.id||'';
      }
      if(id){
        const result=await api(base+'/'+id);if(!live())return;
        if(result.scope_id!==s.scopeId)throw new Error('该采购申请与指定仓库不符。');
        s.record=result;detail(s);
      }else{
        let result={items:[],next_offset:null};
        if(s.scopeId){const p=new URLSearchParams({scope_id:s.scopeId,offset:String(more?s.next:0)});if(s.status)p.set('status',s.status);
          result=await api(base+'?'+p);if(!live())return;}
        const existing=more?s.requests:[],ids=new Set(existing.map(r=>r.id));
        s.requests=[...existing,...result.items.filter(r=>!ids.has(r.id))];s.next=result.next_offset;s.record=null;list(s);
      }
    }catch(err){
      if(live()){
        s.requests=[];s.record=null;s.next=null;list(s);notice(s,err.message,true);
      }
    }finally{if(live()){s.loading=false;busy(s,false);}}
  }
  async function save(s,kind,form){
    if(s.saving||s.loading)return;const d=s.record;
    let path=base, method='POST', body;
    try{
      if(kind==='edit'||kind==='create'){
        const f=new FormData(form),lines=Array.from(form.querySelectorAll('[data-pr-line]'),row=>({sku:row.querySelector('[name=sku]').value.trim(),qty:Number(row.querySelector('[name=qty]').value),...price.read(row)}));
        if(lines.some(l=>!l.sku||!Number.isSafeInteger(l.qty)||l.qty<=0))throw new Error('请输入 SKU 和正整数采购数量。');
        if(new Set(lines.map(l=>l.sku)).size!==lines.length)throw new Error('同一申请的 SKU 须合并为一行。');
        body={supplier:String(f.get('supplier')).trim(),reason:String(f.get('reason')).trim(),lines};
        if(kind==='create')body.scope_id=s.scopeId;
        else{method='PATCH';path+='/'+d.id;body.expected_version=d.version;}
      }else{
        path+='/'+d.id+'/actions';body={action:kind,expected_version:d.version};
        if(form){
          const f=new FormData(form);if(f.has('note'))body.note=String(f.get('note')).trim();
          if(kind==='reject'&&!body.note)throw new Error('退回申请须说明需要修订的内容。');
          if(f.get('order_number')?.trim())body.order_number=f.get('order_number').trim();
        }
      }
    }catch(err){notice(s,err.message,true);return;}
    const signature=JSON.stringify([method,path,body]);
    if(s.pending&&s.pending.signature!==signature){notice(s,'上次操作结果尚未确认，请先以原内容重试或刷新核对。',true);return;}
    s.pending=s.pending||{signature,id:crypto.randomUUID()};s.saving=true;busy(s,true);notice(s,'正在登记…');
    try{
      const result=await api(path,{method,body:JSON.stringify({...body,request_id:s.pending.id})});
      if(active!==s)return;s.pending=null;s.record=result;detail(s);
      notice(s,kind==='issue'?'采购单已签发。':kind==='create'?'申请草稿已保存。':actionLabels[kind]+'已记录。');
    }catch(err){if(active===s){if(err.status&&err.status<500)s.pending=null;
      notice(s,err.status===409?err.message+' 请核对最新单据状态。':err.status&&err.status<500?err.message:'尚未确认提交结果，按原内容重试可复用请求编号。',true);}}
    finally{if(active===s){s.saving=false;busy(s,false);}}
  }
  function open(scopeId='',id=null){
    if(active?.dialog.open){
      if(id&&!active.saving&&active.user&&mode==='server'){active.scopeId=scopeId;active.pending=null;notice(active,'');render(active,'<p>正在读取采购申请…</p>');load(active,id,{scopes:true});}
      return;
    }
    const dialog=document.createElement('dialog');dialog.id='erpProcurementDialog';dialog.className='wb-dialog ap-dialog';dialog.setAttribute('aria-label','采购申请');
    dialog.innerHTML=`<header class="wb-dialog-head"><h2>采购申请</h2>${btn('关闭','close')}</header><div class="wb-dialog-body"></div>`;
    document.body.append(dialog);
    const s={dialog,panel:dialog.querySelector('.wb-dialog-body'),user:window.processUI?.currentUser(),scopes:[],scopeId,status:'',version:0,loading:false,saving:false,pending:null,screen:'list',requests:[],next:null,record:null,message:'',error:false};active=s;
    dialog.addEventListener('close',()=>{if(active===s)active=null;dialog.remove();},{once:true});
    dialog.addEventListener('cancel',event=>{if(s.saving)event.preventDefault();});
    dialog.addEventListener('change',event=>{
      if(s.saving||s.loading)return;
      if(event.target.matches('[data-pr-scope]'))s.scopeId=event.target.value;
      else if(event.target.matches('[data-pr-status]'))s.status=event.target.value;
      else return;
      s.pending=null;notice(s,'');load(s);
    });
    dialog.addEventListener('submit',event=>{
      const form=event.target;if(!form.dataset.prForm)return;event.preventDefault();
      const action=form.dataset.prForm==='decision'?event.submitter?.value:form.dataset.prForm==='edit'?s.screen:'issue';
      if(action)save(s,action,form);
    });
    dialog.addEventListener('click',event=>{
      const button=event.target.closest('[data-pr]');if(!button||s.saving)return;
      const cmd=button.dataset.pr;
      if(cmd==='close'){dialog.close();return;}
      if(cmd==='login'){dialog.close();window.processUI?.login();return;}
      if(s.loading)return;
      switch(cmd){
        case 'list':s.pending=null;notice(s,'');load(s);break;
        case 'refresh':s.pending=null;notice(s,'');load(s,s.screen==='detail'?s.record.id:null,{scopes:true});break;
        case 'more':load(s,null,{more:true});break;
        case 'new':s.pending=null;s.record=null;notice(s,'');edit(s,null);break;
        case 'open':s.pending=null;notice(s,'');load(s,button.dataset.id);break;
        case 'back-detail':s.pending=null;notice(s,'');load(s,s.record.id);break;
        case 'edit':notice(s,'');edit(s,s.record);break;
        case 'submit':save(s,'submit');break;
        case 'receiving':if(window.workbenchUI?.openReceivingOrder(s.record.scope_id,s.record.order_id))dialog.close();
          else notice(s,'当前收货工作区正处理其他操作，请稍后重试。',true);break;
        case 'add-line':button.closest('form').querySelector('tbody').insertAdjacentHTML('beforeend',line());break;
        case 'remove-line':if(button.closest('tbody').rows.length>1)button.closest('tr').remove();else notice(s,'请保留至少一行采购商品。',true);break;
      }
    });
    dialog.showModal();
    if(mode!=='server')render(s,'<p>请连接业务服务器，并使用有仓库权限的账户办理采购申请。</p>');
    else if(!s.user)render(s,'<p>请使用流程账户登录后办理采购申请。</p>'+btn('登录工作台','login'));
    else{render(s,'<p>正在读取业务仓库…</p>');load(s,id,{scopes:true});}
  }
  window.erpProcurement={openRequest:(scopeId,id)=>open(scopeId,id)};
  document.addEventListener('click',ev=>{if(ev.target.closest('[data-erp-procurement-open]'))open();});
})();
