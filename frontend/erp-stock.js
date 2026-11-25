(() => {
  'use strict';
  const url='/api/erp-stock';
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const action=(text,cmd)=>'<button type="button" class="btn" data-stock="'+cmd+'">'+text+'</button>';
  const option=(row,selected)=>'<option value="'+escape(row.id)+'" '+(row.id===selected?'selected':'')+'>'+[row.account_ref,row.site,row.warehouse].map(escape).join(' / ')+'</option>';
  function open(){
    if(document.getElementById('erpStockDesk')?.open)return;
    const dialog=document.createElement('dialog');
    dialog.id='erpStockDesk';dialog.className='wb-dialog rfd-dialog';dialog.setAttribute('aria-label','库存余额与调拨');
    dialog.innerHTML='<header class="wb-dialog-head"><h2>库存余额与调拨</h2>'+action('关闭','close')+'</header><div class="wb-dialog-body"><div data-stock-body></div><p role="alert"></p></div>';
    document.body.append(dialog);dialog.showModal();dialog.addEventListener('close',()=>dialog.remove(),{once:true});
    const body=dialog.querySelector('[data-stock-body]'),error=dialog.querySelector('[role=alert]');
    let scopes=[],selected='',sku='',balanceOffset=0,transferOffset=0,revision=0,busy=false,confirmation='';
    const requests=new Map();
    async function reloadScopes(){
      if(mode!=='server'){body.textContent='连接服务器后可核对本地仓库存余额及登记调拨。';return;}
      if(!window.processUI?.currentUser()){body.innerHTML='<p>请登录工作台查看授权仓库。</p>'+action('登录','login');return;}
      try{
        scopes=(await api('/api/erp-receiving/scopes')).scopes;
        if(!scopes.length){body.textContent='请先在「仓库收货」配置可操作的店铺和仓库。';return;}
        selected=scopes[0].id;await load();
      }catch(e){error.textContent=e.message;body.innerHTML=action('重试','reload');}
    }
    async function load(){
      const check=++revision;error.textContent='';
      try{
        const [balances,transfers]=await Promise.all([
          api(url+'/balances?'+new URLSearchParams({scope_id:selected,sku,offset:balanceOffset})),
          api(url+'/transfers?'+new URLSearchParams({scope_id:selected,offset:transferOffset}))]);
        if(check!==revision||!dialog.open)return;
        const scope=scopes.find(s=>s.id===selected);
        const others=scopes.filter(s=>s.id!==selected&&s.account_ref===scope.account_ref&&s.site===scope.site);
        const balancesHtml=balances.items.length?'<div class="table-scroll"><table><thead><tr><th>SKU</th><th>待检</th><th>良品</th><th>次品</th></tr></thead><tbody>'+
          balances.items.map(b=>'<tr><td>'+escape(b.sku)+'</td><td>'+Number(b.pending)+'</td><td>'+Number(b.good)+'</td><td>'+Number(b.bad)+'</td></tr>').join('')+'</tbody></table></div>':'<p>当前仓库与筛选条件下没有库存流水。</p>';
        const transferForm=others.length?'<details><summary>登记实际调拨</summary><form data-kind="transfer"><p>仅在两仓均已核实交接后登记。保存时转出仓扣减良品，转入仓同时增加相同件数。</p><div class="rfd-fields">'+
          '<label class="field">转入仓库<select name="to_scope_id">'+others.map(s=>option(s,'')).join('')+'</select></label>'+
          '<label class="field">调拨 SKU<input required name="sku" maxlength="140" value="'+escape(sku)+'"></label>'+
          '<label class="field">件数<input required name="qty" type="number" min="1" max="1000000" step="1"></label>'+
          '<label class="field">调拨单号<input required name="reference" maxlength="120"></label><label class="field">交接凭证<input required name="evidence" maxlength="140"></label></div>'+
          '<button class="btn primary">确认调拨</button></form></details>':'<p>要办理调拨，需要获得同店铺、同站点另一座仓库的权限。</p>';
        body.innerHTML='<form data-kind="filter"><div class="rfd-fields"><label class="field">查看仓库<select name="scope_id">'+scopes.map(s=>option(s,selected)).join('')+'</select></label>'+
          '<label class="field">精确查询 SKU<input name="sku" maxlength="140" value="'+escape(sku)+'"></label></div><div class="rfd-actions"><button class="btn">查询余额</button></div></form>'+
          (confirmation?'<p role="status">'+escape(confirmation)+'</p>':'')+'<h3>仓库库存</h3>'+balancesHtml+
          '<div class="rfd-actions">'+(balanceOffset?action('上一页库存','prevBalance'):'')+(balances.has_more?action('下一页库存','nextBalance'):'')+'</div>'+transferForm+
          '<h3>调拨流水</h3>'+(transfers.items.length?'<ul class="rfd-list">'+transfers.items.map(t=>'<li><div><b>'+escape(t.reference)+' · '+escape(t.sku)+' × '+Number(t.qty)+'</b><span>'+
            escape(t.from_warehouse)+' → '+escape(t.to_warehouse)+'</span><small>'+escape(t.evidence)+' · '+escape(t.actor_name)+' · '+escape(new Date(t.created_at).toLocaleString())+'</small></div></li>').join('')+'</ul>':'<p>尚无调拨记录。</p>')+
          '<div class="rfd-actions">'+(transferOffset?action('上一页流水','prevTransfer'):'')+(transfers.has_more?action('下一页流水','nextTransfer'):'')+'</div>';
      }catch(e){if(check===revision){error.textContent=e.message;if(!body.children.length)body.innerHTML=action('重试','reload');}}
    }
    dialog.addEventListener('click',event=>{
      const b=event.target.closest('[data-stock]');if(!b)return;
      const cmd=b.dataset.stock;
      if(cmd==='close'){dialog.close();return;}
      if(busy)return;
      if(cmd==='login'){dialog.close();window.processUI.login();}
      if(cmd==='reload')reloadScopes();
      if(cmd.endsWith('Balance')){balanceOffset=Math.max(0,balanceOffset+(cmd==='nextBalance'?50:-50));load();}
      if(cmd.endsWith('Transfer')){transferOffset=Math.max(0,transferOffset+(cmd==='nextTransfer'?50:-50));load();}
    });
    dialog.addEventListener('change',event=>{
      if(!event.target.matches('[name=scope_id]')||busy)return;
      const form=event.target.form;
      selected=event.target.value;sku=form.elements.sku.value.trim();
      balanceOffset=transferOffset=0;confirmation='';load();
    });
    dialog.addEventListener('submit',async event=>{
      event.preventDefault();if(busy)return;
      const form=event.target,data=Object.fromEntries(new FormData(form));
      if(form.dataset.kind==='filter'){
        selected=data.scope_id;sku=data.sku.trim();balanceOffset=transferOffset=0;confirmation='';await load();return;
      }
      const payload={...data,from_scope_id:selected,qty:Number(data.qty)},key=JSON.stringify(payload);
      if(!requests.has(key))requests.set(key,crypto.randomUUID());
      busy=true;form.querySelector('button').disabled=true;error.textContent='';
      try{
        const entry=await api(url+'/transfers',{method:'POST',body:JSON.stringify({...payload,request_id:requests.get(key)})});
        requests.delete(key);confirmation='已登记调拨 '+entry.reference+'，两仓数量已更新。';transferOffset=0;await load();
      }catch(e){error.textContent=e.message;}
      finally{busy=false;form.querySelector('button').disabled=false;}
    });
    reloadScopes();
  }
  document.addEventListener('click',event=>{if(event.target.closest('[data-erp-stock-open]'))open();});
})();
