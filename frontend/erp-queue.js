/* All warehouses visible to the active account, independent of the overview's product filter. */
(() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  const kinds = {receive:'待收货',quality:'待质检',payment:'待付余额',approval:'待我审批'};
  const cancellation = {after_dispatch:'发货后取消待处理',stop_failed:'截单未成功，待催截或核对出库',intercepting:'待核实拦截结果',hold:'待确认买家取消'};
  const format = number => Number(number).toLocaleString('zh-CN',{maximumFractionDigits:4});
  let dialog;
  function open() {
    if (dialog?.open) return;
    const dlg=document.createElement('dialog'); dialog=dlg;
    dlg.className='wb-dialog'; dlg.id='erpQueueDialog'; dlg.setAttribute('aria-labelledby','erpQueueTitle');
    dlg.innerHTML='<header class="wb-dialog-head"><h2 id="erpQueueTitle">ERP 待办</h2><button class="btn" data-queue-close>关闭</button></header>' +
      '<div class="wb-dialog-body"><p>按当前账户全部可访问仓库汇总，独立于总览产品筛选。包裹取消异常优先显示。</p><div data-queue-list aria-live="polite"></div></div>';
    document.body.append(dlg); dlg.showModal();
    const target=dlg.querySelector('[data-queue-list]');
    const actor=window.processUI?.currentUser()?.id;
    let sequence=0, offset=0, nextOffset=null, items=[];
    async function refresh(position) {
      if (!dlg.open) return;
      const seq=++sequence;
      if(mode!=='server'){target.textContent='请连接服务器查看实际仓库待办。';return;}
      if(!actor||window.processUI?.currentUser()?.id!==actor){
        target.innerHTML='<button class="btn" data-queue-login>登录协作账户</button>';return;
      }
      target.textContent='正在核对仓库单据与余额…';
      try {
        const data=await api('/api/erp-queue?limit=30&offset='+position);
        if (!dlg.open||seq!==sequence||window.processUI?.currentUser()?.id!==actor) return;
        offset=position;nextOffset=data.next_offset;items=data.items;
        if (offset>0&&!items.length) return refresh(Math.max(0,offset-30));
        const rows=items.map((row,i)=>{
          const label=kinds[row.kind]||cancellation[row.state], remaining=row.quantity!=null?format(row.quantity)+' 件':
            row.balance!=null?format(row.balance)+' '+escape(row.currency):'—';
          return '<tr><th scope="row">'+escape(label)+'</th><td><button type="button" class="wb-link" data-queue-item="'+i+'">'+
            escape(row.reference)+'</button><small>'+escape(row.subject)+'</small></td><td>'+escape(row.warehouse)+'<small>'+
            escape(row.account_ref)+' · '+escape(row.site)+'</small></td><td>'+remaining+'</td></tr>';
        }).join('');
        target.innerHTML=(rows?'<div class="table-wrap"><table class="wb-table"><thead><tr><th>待办</th><th>单据与来源</th><th>仓库及店铺</th><th>待处理余额 / 件数</th></tr></thead><tbody>'+
          rows+'</tbody></table></div>':'<p>当前可访问仓库暂无符合条件的待办。</p>')+
          '<div class="wb-toolbar"><button class="btn" data-queue-refresh>刷新待办</button>'+
          (offset>0?'<button class="btn" data-queue-prev>上一页</button>':'')+
          (nextOffset!=null?'<button class="btn" data-queue-next>下一页</button>':'')+'</div>';
      } catch (e) {
        if(dlg.open&&seq===sequence)target.innerHTML='<p role="alert">'+escape(e.message)+'</p><button class="btn" data-queue-refresh>重试</button>';
      }
    }
    function reopenSession(){dlg.close();}
    const timer=setInterval(()=>{if(!document.hidden && !positionHasFocus())refresh(offset);},30000);
    function positionHasFocus(){return dlg.contains(document.activeElement) && document.activeElement.hasAttribute('data-queue-item');}
    window.addEventListener('homa-session',reopenSession);
    dlg.addEventListener('close',()=>{
      clearInterval(timer);window.removeEventListener('homa-session',reopenSession);dlg.remove();if(dialog===dlg)dialog=null;
    },{once:true});
    dlg.onclick=event=>{
      const button=event.target.closest('button');if(!button)return;
      if(button.hasAttribute('data-queue-close'))return dlg.close();
      if(button.hasAttribute('data-queue-login')){dlg.close();return window.processUI?.login();}
      if(button.hasAttribute('data-queue-refresh'))return refresh(0);
      if(button.hasAttribute('data-queue-prev'))return refresh(Math.max(0,offset-30));
      if(button.hasAttribute('data-queue-next'))return refresh(nextOffset);
      if(!button.hasAttribute('data-queue-item'))return;
      const row=items[Number(button.dataset.queueItem)];if(!row)return;
      const openSource={receive:window.workbenchUI?.openReceivingOrder,quality:window.workbenchUI?.openReceivingOrder,
        cancel:window.erpFulfillment?.openOrder,payment:window.erpPayables?.openBill,approval:window.erpProcurement?.openRequest}[row.kind];
      if(!openSource){target.textContent='来源办理模块未加载，请刷新页面后重试。';return;}
      dlg.close();openSource(row.scope_id,row.target_id,row.id);
    };
    refresh(0);
  }
  document.addEventListener('click',event=>{if(event.target.closest('[data-erp-queue-open]'))open();});
})();
