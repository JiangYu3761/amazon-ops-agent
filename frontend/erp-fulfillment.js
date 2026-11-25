/* Open the FBM desk from the order and service workspace. */
(() => {
  'use strict';
  const base='/api/erp-fulfillment';
  const labels={review:'待审核', released:'已放行，待仓库出库', shipped:'已收到出库回执', hold:'买家取消，暂停审核',
    intercepting:'待仓库截单回执', stop_failed:'截单未成功，待跟进', after_dispatch:'出库后取消，待核实包裹', cancelled:'已取消', closed_shipped:'已签收，异常已核实',
    create:'登记订单', allocate:'分配包裹', payment:'确认收款', release:'审核放行', dispatch:'登记仓库出库回执',
    buyer_cancel:'登记买家取消', withdraw_cancel:'登记取消申请撤回', confirm_cancel:'确认取消未放行包裹',
    intercept_ok:'登记拦截成功回执', intercept_failed:'登记拦截失败回执', retry_intercept:'登记再次申请截单', delivered_after_cancel:'登记承运商签收回执'};
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const button=(label,action)=>'<button type="button" class="btn" data-fbm="'+esc(action)+'">'+esc(label)+'</button>';
  const input=(label,name,attrs='required maxlength="140"')=>'<label class="field">'+label+'<input name="'+name+'" '+attrs+'></label>';
  function open(initialScope,initialOrder,initialPackage){
    if(document.getElementById('erpFbmDesk')?.open)return;
    const dialog=document.createElement('dialog');dialog.className='wb-dialog rfd-dialog';dialog.id='erpFbmDesk';
    dialog.setAttribute('aria-label','自发货履约');
    dialog.innerHTML='<header class="wb-dialog-head"><h2>自发货履约</h2>'+button('关闭','close')+'</header><div class="wb-dialog-body"><div data-body></div><p role="alert" class="rfd-error"></p></div>';
    document.body.append(dialog);dialog.showModal();dialog.addEventListener('close',()=>dialog.remove(),{once:true});
    const body=dialog.querySelector('[data-body]'),alert=dialog.querySelector('[role=alert]');
    let scopes=[],scopeId='',search='',offset=0,order,version=0,busy=false;
    const requestIds=new Map();
    const warning=message=>{alert.textContent=message;};
    function draw(content){body.innerHTML=content;warning('');}
    async function refreshScopes(){
      const stamp=++version;
      if(mode!=='server'){draw('<p>连接工作台服务器后即可登记履约单。</p>');return;}
      if(!window.processUI?.currentUser()){draw('<p>请先登录工作台。</p>'+button('登录工作台','login'));return;}
      try{
        const response=await api('/api/erp-receiving/scopes');
        if(stamp!==version||!dialog.open)return;
        scopes=response.scopes;scopeId=initialScope||scopes[0]?.id;
        if(!scopeId){draw('<p>当前没有可办理的仓库。管理员可在「库存 → 仓库收货」配置店铺、仓库和经办人。</p>');return;}
        if(initialOrder){await loadOrder(initialOrder);}else{await list();}
      }catch(err){if(stamp===version){draw(button('重新加载','reload'));warning(err.message);}}
    }
    function form(kind,fields,submit){
      return '<form data-form="'+kind+'"><div class="rfd-fields">'+fields+'</div><div class="rfd-actions"><button class="btn primary">'+submit+'</button></div></form>';
    }
    const scopeName=s=>[s.account_ref,s.site,s.warehouse].map(esc).join(' / ');
    async function list(){
      const stamp=++version;
      try{
        const data=await api(base+'/orders?'+new URLSearchParams({scope_id:scopeId,search,offset}));
        if(version!==stamp||!dialog.open)return;
        order=null;
        draw(form('filter','<label class="field">店铺与履约仓<select name="scope">'+scopes.map(s=>'<option value="'+esc(s.id)+'" '+(s.id===scopeId?'selected':'')+'>'+scopeName(s)+'</option>').join('')+'</select></label>'+
          '<label class="field">订单号或包裹号<input name="search" maxlength="140" value="'+esc(search)+'"></label>','查询')+
          '<div class="rfd-actions">'+button('登记订单','new')+'</div>'+
          (data.items.length?'<ul class="rfd-list">'+data.items.map(row=>'<li><div><b>'+esc(row.number)+'</b><small>'+esc(row.origin==='platform'?'平台来源':'手工订单')+' · '+(row.paid?'已确认收款':'待确认收款')+' · '+Number(row.packages)+' 个包裹</small></div>'+button('办理','order/'+row.id)+'</li>').join('')+'</ul>':'<p>当前范围没有符合条件的订单。</p>')+
          '<div class="rfd-actions">'+(offset?button('上一页','prev'):'')+(data.has_more?button('下一页','next'):'')+'</div>');
      }catch(err){if(stamp===version)warning(err.message);}
    }
    const lineFields=()=>'<fieldset data-line><legend>商品行</legend><div class="rfd-fields">'+input('来源订单行号','line_ref')+input('SKU','sku')+
      input('购买数量','qty','type="number" min="1" max="1000000" step="1" value="1" required')+'</div>'+button('移除此行','remove')+'</fieldset>';
    function newOrder(){
      draw(button('返回订单','list')+'<h3>登记订单</h3><p>'+scopeName(scopes.find(s=>s.id===scopeId))+'</p><form data-form="create"><div class="rfd-fields">'+
        input('订单编号','number')+input('订单来源凭证','source_ref')+'<label class="field">来源<select name="origin"><option value="platform">平台订单人工登记</option><option value="manual">手工订单</option></select></label>'+
        '<label class="field">收款核对<select name="paid"><option value="false">待确认</option><option value="true">来源凭证已确认收款</option></select></label></div><div data-lines>'+lineFields()+'</div>'+
        '<div class="rfd-actions">'+button('添加商品行','add')+'<button class="btn primary">保存订单</button></div></form>');
    }
    function orderView(x){
      order=x;
      const remaining=x.lines.filter(l=>Number(l.remaining)>0);
      const packForm=remaining.length?'<details><summary>分配包裹</summary><form data-form="allocate">'+input('包裹编号','package_ref')+
        remaining.map(l=>'<label class="field">行 '+esc(l.line_ref)+' · '+esc(l.sku)+' · 可分配 '+Number(l.remaining)+' 件<input data-allocation="'+esc(l.id)+'" type="number" min="0" max="'+Number(l.remaining)+'" step="1" value="0" required></label>').join('')+
        '<button class="btn primary">保存包裹</button></form></details>':'<p>全部订单件数已分配。</p>';
      const packages=x.packages.map(p=>'<section class="panel" tabindex="-1" data-fbm-package="'+esc(p.id)+'"><h4>'+esc(p.package_ref)+' · '+esc(labels[p.status])+'</h4><p>'+p.lines.map(l=>esc(l.line_ref)+' / '+esc(l.sku)+' × '+Number(l.qty)).join('，')+'</p>'+
        (p.actions.length?'<form data-form="event" data-package="'+esc(p.id)+'" data-version="'+Number(p.version)+'"><div class="rfd-fields"><label class="field">办理动作<select name="action">'+
          p.actions.map(a=>'<option value="'+esc(a)+'">'+esc(labels[a])+'</option>').join('')+'</select></label>'+input('来源消息、仓库或承运商回执编号','evidence')+'</div><button class="btn">登记办理结果</button></form>':'')+'</section>').join('');
      draw('<div class="rfd-actions">'+button('返回订单','list')+button('刷新单据','refresh')+'</div><h3>'+esc(x.number)+'</h3><p>'+scopeName(x)+' · '+(x.origin==='manual'?'手工订单':'平台来源')+' · '+(x.paid?'已确认收款':'待确认收款')+'</p>'+
        '<p>每个包裹单独流转。放行、出库及截单均记录外部凭证，实际发运和平台回传在原履约系统办理。</p>'+
        (!x.paid?'<details><summary>确认收款</summary>'+form('payment',input('收款凭证编号','evidence'),'登记收款凭证')+'</details>':'')+
        '<h4>订单行</h4><ul>'+x.lines.map(l=>'<li>'+esc(l.line_ref)+' · '+esc(l.sku)+' × '+Number(l.qty)+'，待分配 '+Number(l.remaining)+'</li>').join('')+'</ul>'+packForm+
        '<h4>履约包裹</h4>'+ (packages||'<p>尚无包裹。</p>')+'<details><summary>办理流水（'+x.events.length+'）</summary><ul class="rfd-list">'+
        x.events.map(v=>'<li><div><b>'+esc(labels[v.event])+' · '+esc(v.package_ref||x.number)+'</b><span>'+esc(v.evidence)+'</span><small>'+esc(v.actor_name)+' · '+esc(new Date(v.created_at).toLocaleString())+'</small></div></li>').join('')+'</ul></details>');
    }
    async function loadOrder(id){
      const stamp=++version;
      try{
        const x=await api(base+'/orders/'+id);
        if(version===stamp&&dialog.open){
          orderView(x);
          if(initialPackage){
            const target=Array.from(body.querySelectorAll('[data-fbm-package]')).find(el=>el.dataset.fbmPackage===initialPackage);
            if(target){target.focus();target.scrollIntoView({block:'center'});}
            initialPackage=null;
          }
        }
      }
      catch(err){if(version===stamp)warning(err.message);}
    }
    dialog.addEventListener('click',event=>{
      const control=event.target.closest('[data-fbm]');
      if(!control)return;
      const cmd=control.getAttribute('data-fbm');
      if(cmd==='close'){dialog.close();return;}
      if(busy)return;
      if(cmd==='login'){dialog.close();window.processUI.login();}
      if(cmd==='reload')refreshScopes();
      if(cmd==='list')list();
      if(cmd==='new'){version++;newOrder();}
      if(cmd==='add')body.querySelector('[data-lines]').insertAdjacentHTML('beforeend',lineFields());
      if(cmd==='remove'&&body.querySelectorAll('[data-line]').length>1)control.closest('[data-line]').remove();
      if(cmd==='next'||cmd==='prev'){offset+=cmd==='next'?30:-30;list();}
      if(cmd.startsWith('order/'))loadOrder(cmd.slice(6));
      if(cmd==='refresh'&&order)loadOrder(order.id);
    });
    dialog.addEventListener('submit',async event=>{
      event.preventDefault();if(busy)return;
      const f=event.target, kind=f.dataset.form, values=Object.fromEntries(new FormData(f));
      if(kind==='filter'){scopeId=values.scope;search=values.search.trim();offset=0;return list();}
      let url=base+'/orders', payload;
      if(kind==='create'){
        payload={scope_id:scopeId,number:values.number,origin:values.origin,source_ref:values.source_ref,paid:values.paid==='true',
          lines:[...f.querySelectorAll('[data-line]')].map(l=>({line_ref:l.querySelector('[name=line_ref]').value,sku:l.querySelector('[name=sku]').value,qty:Number(l.querySelector('[name=qty]').value)}))};
      }else if(kind==='payment'){url+='/'+order.id+'/payment';payload={evidence:values.evidence};}
      else if(kind==='allocate'){
        url+='/'+order.id+'/packages';payload={package_ref:values.package_ref,
          lines:[...f.querySelectorAll('[data-allocation]')].filter(el=>Number(el.value)>0).map(el=>({line_id:el.dataset.allocation,qty:Number(el.value)}))};
        if(!payload.lines.length){warning('请为本包裹选择至少一件商品。');return;}
      }else if(kind==='event'){
        url=base+'/packages/'+f.dataset.package+'/events';payload={action:values.action,evidence:values.evidence,expected_version:Number(f.dataset.version)};
      }else{return;}
      const key=url+JSON.stringify(payload);
      if(!requestIds.has(key))requestIds.set(key,crypto.randomUUID());
      busy=true;const submit=f.querySelector('button:not([type=button])');if(submit)submit.disabled=true;
      try{
        const x=await api(url,{method:'POST',body:JSON.stringify({...payload,request_id:requestIds.get(key)})});
        requestIds.delete(key);if(dialog.open)orderView(x);
      }catch(err){if(dialog.open)warning(err.message);}
      finally{busy=false;if(submit)submit.disabled=false;}
    });
    refreshScopes();
  }
  window.erpFulfillment={openOrder:(scopeId,orderId,packageId)=>open(scopeId,orderId,packageId)};
  document.addEventListener('click',event=>{if(event.target.closest('[data-erp-fulfillment-open]'))open();});
})();
