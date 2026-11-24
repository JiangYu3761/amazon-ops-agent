/* Shop/site-level daily reports with the same visibility as the local warehouse scopes. */
(() => {
  const endpoint='/api/erp-shop-sales';
  const header='account_ref,site,currency,date,report_grain,coverage,ordered_revenue,units_ordered,orders';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  const labels={ordered_revenue:'订购销售额',units_ordered:'订购件数',orders:'订单数'};
  const number=v=>{
    if(v==null)return '—';
    const [whole,fraction='']=String(v).split('.'), decimal=fraction.replace(/0+$/,'');
    return esc(whole.replace(/\B(?=(\d{3})+(?!\d))/g,','))+(decimal?'.'+esc(decimal):'');
  };
  const td=x=>'<td>'+x+'</td>';
  const table=(cols,rows)=>'<div class="table-wrap"><table class="wb-table"><thead><tr>'+cols.map(s=>'<th>'+s+'</th>').join('')+
    '</tr></thead><tbody>'+rows+'</tbody></table></div>';
  let active;
  function open(){
    if(active?.open)return;
    const dlg=document.createElement('dialog');active=dlg;dlg.id='erpShopSalesDialog';dlg.className='wb-dialog';dlg.setAttribute('aria-labelledby','shopSalesTitle');
    dlg.innerHTML='<header class="wb-dialog-head"><h2 id="shopSalesTitle">多店经营</h2><button class="btn" data-ss-close>关闭</button></header><div class="wb-dialog-body"></div>';
    document.body.append(dlg);dlg.showModal();
    const body=dlg.querySelector('.wb-dialog-body'),user=window.processUI?.currentUser();
    let generation=0,drillVersion=0,report,detailRows=[],nextOffset=null;
    const session=()=>dlg.close();
    window.addEventListener('homa-session',session);
    dlg.addEventListener('close',()=>{window.removeEventListener('homa-session',session);dlg.remove();if(active===dlg)active=null;},{once:true});
    dlg.querySelector('[data-ss-close]').onclick=()=>dlg.close();
    if(mode!=='server'||!user){body.textContent=mode!=='server'?'连接服务器后可对比已导入的店铺日报。':'请登录协作账户查看授权店铺的经营数据。';
      if(mode==='server'){const b=document.createElement('button');b.textContent='登录';b.className='btn';b.onclick=()=>{dlg.close();window.processUI?.login();};body.append(b);}return;}
    body.innerHTML='<p>按当前账户可访问的店铺与站点去重。各币种单独小计，区间须在所有可见店铺的业务时区内结束。</p>'+
      '<form class="wb-toolbar" data-ss-range><label>本期截止日<input type="date" name="end_date"></label><label>每期天数<input name="days" type="number" min="1" max="366" required value="7"></label>'+
      '<button class="btn primary">查询两期</button></form><label>指标 <select data-ss-metric>'+Object.entries(labels).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join('')+'</select></label>'+
      '<details><summary>日报口径与 CSV 导入</summary><p>每行对应整个店铺一天，不得混入单商品。account_ref 和 site 须与仓库登记一致，date 使用 YYYY-MM-DD，currency 填原币种。'+
      'report_grain 填 shop_day，coverage 填 complete（全天完整）或 partial（部分数据）。销售额、订购件数、订单数未知时留空，确认为零时填 0。重新导入同店同币同日会更新依据。</p>'+
      '<a download="shop-sales.csv" href="data:text/csv;charset=utf-8,'+encodeURIComponent('\uFEFF'+header+'\r\n')+'">下载空白日报模板</a>'+
      (user.is_admin?'<form class="wb-toolbar" data-ss-upload><label>店铺日报 CSV<input type="file" accept=".csv,text/csv" required></label><button class="btn">导入日报</button></form>':'<p>日报由工作空间管理员导入。</p>')+
      '<p role="status" data-ss-message></p></details><div data-ss-results aria-live="polite"></div><div data-ss-source aria-live="polite"></div>';
    const range=body.querySelector('[data-ss-range]'),result=body.querySelector('[data-ss-results]'),source=body.querySelector('[data-ss-source]'),metric=body.querySelector('[data-ss-metric]');
    const status=r=>r.status==='ready'?'可比':r.status==='zero_base'?'上期为零':'覆盖待补';
    function render(){
      if(!report)return;
      const k=metric.value, cells=m=>[m.current.value,m.previous.value,m.difference].map(v=>td(number(v))).join('')+
        td(number(m.change_percent)+(m.change_percent!=null?'%':''))+td(esc(status(m)));
      const rows=report.stores.map((s,i)=>{
        const m=s.metrics[k],label=s.currency?'<button class="wb-link" data-ss-store="'+i+'">'+esc(s.account_ref)+'</button>':esc(s.account_ref);
        return '<tr>'+td(label+'<small>'+esc(s.site)+' · '+esc(s.zones.join(' / '))+
          (s.zones.length>1?' · 仓库时区冲突':'')+'</small>')+td(esc(s.currency||'尚无日报'))+cells(m)+
          td(m.current.confirmed_days+' / '+m.previous.confirmed_days+' 天')+'</tr>';
      }).join('');
      const totals=report.subtotals.map(r=>'<tr>'+td(esc(r.currency))+td(r.stores)+cells(r.metrics[k])+'</tr>').join('');
      result.innerHTML='<p>本期 '+report.current.map(esc).join(' 至 ')+'　上期 '+report.previous.map(esc).join(' 至 ')+'</p>'+
        (totals?'<h3>原币小计</h3>'+table(['币种','已知使用该币种的店铺数','本期','上期','差额','环比','覆盖'],totals):'')+
        (rows?'<h3>店铺明细</h3>'+table(['店铺 / 站点','币种','本期','上期','差额','环比','覆盖','完整天数 本 / 上'],rows):'<p>当前账户尚无可见的店铺仓库。</p>')+
        '<p>部分期间只列已有金额或数量。两期完整才计算变化率，上期为零仅显示差额。没有币种依据的店铺列在明细中。</p>';
    }
    async function refresh(){
      const token=++generation;drillVersion++;source.replaceChildren();report=null;result.textContent='正在读取授权店铺日报…';
      const params=new URLSearchParams(new FormData(range));if(!params.get('end_date'))params.delete('end_date');
      try{const data=await api(endpoint+'?'+params);if(!dlg.open||generation!==token)return;report=data;
        if(!range.elements.end_date.value)range.elements.end_date.value=report.current[1];render();
      }catch(e){if(dlg.open&&generation===token)result.textContent=e.message;}
    }
    async function details(i,more){
      const token=++drillVersion,s=report.stores[i];
      const params=new URLSearchParams({account_ref:s.account_ref,site:s.site,currency:s.currency,start_date:report.previous[0],end_date:report.current[1],offset:more?nextOffset:0});
      if(!more){detailRows=[];source.textContent='正在读取原始日报行…';}
      try{const data=await api(endpoint+'/source?'+params);if(!dlg.open||drillVersion!==token)return;
        detailRows.push(...data.rows);nextOffset=data.next_offset;
        const rows=detailRows.map(r=>'<tr>'+td(esc(r.business_day))+td(esc(r.business_timezone))+td(r.included?(r.coverage==='complete'?'全天完整':'部分数据'):'时区已变更，未计入')+
          td(number(r.ordered_revenue))+td(number(r.units_ordered))+td(number(r.orders))+td(esc(r.filename)+' · 第 '+r.source_line+' 行<small>批次 '+esc(r.import_id)+'</small>')+'</tr>').join('');
        source.innerHTML='<h3>'+esc(s.account_ref)+' · '+esc(s.site)+' · '+esc(s.currency)+' 来源</h3>'+
          (rows?table(['业务日','时区','覆盖','订购销售额','件数','订单数','导入原行'],rows):'<p>这两个期间没有已导入的日报行。</p>')+
          (nextOffset!=null?'<button class="btn" data-ss-more="'+i+'">更多来源行</button>':'');
      }catch(e){if(dlg.open&&drillVersion===token)source.textContent=e.message;}
    }
    range.onsubmit=e=>{e.preventDefault();refresh();};metric.onchange=render;
    body.addEventListener('click',e=>{const b=e.target.closest('button');if(b?.hasAttribute('data-ss-store'))details(Number(b.dataset.ssStore),false);
      if(b?.hasAttribute('data-ss-more'))details(Number(b.dataset.ssMore),true);});
    const upload=body.querySelector('[data-ss-upload]');
    if(upload)upload.onsubmit=async e=>{e.preventDefault();const b=upload.querySelector('button'),message=body.querySelector('[data-ss-message]');
      if(b.disabled)return;b.disabled=true;const form=new FormData();form.append('file',upload.querySelector('input').files[0]);
      try{const r=await api(endpoint+'/imports',{method:'POST',body:form});if(!dlg.open)return;
        message.textContent=r.duplicate?'此文件已导入，原数据保留。':'已导入 '+r.rows+' 行。';upload.reset();await refresh();
      }catch(error){if(dlg.open)message.textContent=error.message;}finally{b.disabled=false;}};
    refresh();
  }
  document.addEventListener('click',e=>{if(e.target.closest('[data-erp-shop-sales-open]'))open();});
})();
