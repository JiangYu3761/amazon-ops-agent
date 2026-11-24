/* Explicit SP / SB keyword search-term CSV, scoped to the selected product. */
window.erpSearchTerms = (() => {
  let active;
  const header = 'date,campaign_name,impressions,clicks,spend,sales,orders,currency,report_grain,profile_id,ad_type,campaign_id,ad_group_id,keyword_id,match_type,search_term,attribution_days';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const n = (value, suffix = '') => value == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:4}) + suffix;
  function unmount() { if (active) active.node.replaceChildren(); active = null; }
  function table(labels, body) {
    return '<div class="table-wrap"><table class="wb-table"><thead><tr>' +
      labels.map(s => '<th>' + s + '</th>').join('') + '</tr></thead><tbody>' + body + '</tbody></table></div>';
  }
  const cell = (s = '') => '<td>' + s + '</td>';
  function list(state) {
    const body = state.rows.map((r, i) => '<tr>' + cell('<button class="wb-link" data-term-row="' + i + '">' + esc(r.search_term) + '</button>') +
      cell(esc(r.profile_id) + '<small>' + esc(r.ad_types.join(' / ')) + ' · ' + esc(r.attribution_days) + ' 天</small>') +
      [r.impressions,r.clicks,r.spend,r.sales,r.orders].map(x => cell(n(x))).join('') +
      cell(n(r.acos,'%')) + cell(n(r.cpc)) + cell(n(r.ctr,'%')) + cell(n(r.source_count)) + '</tr>').join('');
    state.result.innerHTML = '<p>' + esc(state.period) + '　' + esc(state.currency) + '</p>' +
      (body ? table(['搜索词及来源','广告账户 / 归因窗','曝光','点击','花费','归因销售额','归因订单','ACOS','CPC','CTR','来源行'], body) :
        '<p>此产品与期间尚无符合口径的搜索词报表。可检查日期，或由管理员导入报表。</p>') +
      (state.next != null ? '<button class="btn" data-term-more>更多搜索词</button>' : '');
  }
  async function load(state, more = false) {
    if (!more) { state.rows = []; state.sourceSerial++; state.source.replaceChildren(); }
    const id = ++state.serial;
    const params = more ? new URLSearchParams(state.query) : new URLSearchParams(new FormData(state.range));
    for (const k of ['start_date','end_date']) if (!params.get(k)) params.delete(k);
    params.set('offset', String(more ? state.next : 0));
    if (!more) state.result.textContent = '正在汇总搜索词…';
    try {
      const data = await state.api(state.base + '?' + params);
      if (active !== state || id !== state.serial) return;
      state.query = {start_date:data.start_date, end_date:data.end_date};
      if (!state.range.elements.start_date.value) state.range.elements.start_date.value = data.start_date;
      if (!state.range.elements.end_date.value) state.range.elements.end_date.value = data.end_date;
      state.rows.push(...data.rows); state.next = data.next_offset; state.currency = data.currency;
      state.period = data.start_date + ' 至 ' + data.end_date;
      list(state);
    } catch (e) { if (active === state && id === state.serial) state.result.textContent = e.message; }
  }
  async function drill(state, i, more = false) {
    const row = state.rows[i], seq = ++state.sourceSerial;
    const params = new URLSearchParams({...state.query,search_term:row.search_term,profile_id:row.profile_id,attribution_days:row.attribution_days});
    if (more) params.set('offset', String(state.detailNext));
    else { state.sources = []; state.source.textContent = '正在读取导入原行…'; }
    try {
      const data = await state.api(state.base + '/source?' + params);
      if (active !== state || seq !== state.sourceSerial) return;
      state.sources.push(...data.rows); state.detailNext = data.next_offset;
      const body = state.sources.map(r => '<tr>' + cell(esc(r.filename) + ' · 第 ' + esc(r.source_line) + ' 行<small>批次 ' + esc(r.import_id) + '</small>') +
        cell(esc(r.report_date)) + cell(esc(r.ad_type)) + cell(esc(r.campaign_name) + '<small>' + esc(r.campaign_id) + ' / ' + esc(r.ad_group_id) + '</small>') +
        cell(esc(r.keyword_id) + ' · ' + esc(r.match_type)) + [r.impressions,r.clicks,r.spend,r.sales,r.orders].map(v=>cell(n(v))).join('') + '</tr>').join('');
      state.source.innerHTML = '<h3>' + esc(row.search_term) + ' · 来源行</h3><p>' + esc(row.profile_id) + '　' + esc(row.attribution_days) +
        ' 天归因　' + esc(row.currency) + '</p>' + table(['导入文件 / 批次','日期','类型','广告活动 / 广告组','关键词 / 匹配','曝光','点击','花费','归因销售额','订单'],body) +
        (state.detailNext != null ? '<button class="btn" data-term-source-more="' + i + '">更多来源行</button>' : '');
    } catch (e) { if (active === state && seq === state.sourceSerial) state.source.textContent = e.message; }
  }
  function mount(node, { mode, product, user, api, login }) {
    const key = [mode,product?.id,user?.id,user?.is_admin].join('|');
    if (active?.node===node && active.key===key) return;
    unmount();
    const state = { key,node,api,base:'/api/products/' + encodeURIComponent(product?.id || '') + '/erp/search-terms',serial:0,sourceSerial:0 };
    active = state;
    node.innerHTML = '<section class="panel wb-queue"><h2>搜索词汇总</h2><div data-term-body></div></section>';
    const body = node.querySelector('[data-term-body]');
    if (mode !== 'server' || !user || !product) {
      body.textContent = mode !== 'server' ? '连接服务器并导入搜索词日报后，可查看汇总与原始投放。' :
        !user ? '请登录协作账户查看广告报表。' : '请在上方选择一个产品型号。';
      if (mode==='server' && !user && login) { const b=document.createElement('button');b.className='btn';b.textContent='登录';b.onclick=login;body.append(b); }
      return;
    }
    body.innerHTML = '<p>按同一广告账户、币种、归因窗与搜索词原文汇总已导入的 SP／SB 关键词行。比例使用合计数重算，分母为零时显示「—」。</p>' +
      '<form data-term-range class="wb-toolbar"><label>开始日期<input type="date" name="start_date"></label><label>截止日期<input type="date" name="end_date"></label><button class="btn primary">查询</button></form>' +
      '<details><summary>CSV 格式与导入</summary><p>使用本产品范围的关键词投放日报，每行填写文件模板中的全部列。report_grain 填 search_term，ad_type 填 SP 或 SB，match_type 填 EXACT、PHRASE 或 BROAD，attribution_days 填 1 至 90 的整数。币种须与选中产品一致。相同来源投放、搜索词、日期及窗口重新导入会更新原行。</p>' +
      '<a download="search-terms-template.csv" href="data:text/csv;charset=utf-8,' + encodeURIComponent('\uFEFF'+header+'\r\n') + '">下载空白模板</a>' +
      (user.is_admin ? '<form data-term-upload class="wb-toolbar"><label>搜索词 CSV<input type="file" accept=".csv,text/csv" required></label><button class="btn">导入当前产品</button></form>' : '<p>由工作空间管理员导入。</p>') +
      '<p data-term-message role="status"></p></details><div data-term-list aria-live="polite"></div><div data-term-source aria-live="polite"></div>';
    state.result = body.querySelector('[data-term-list]'); state.source = body.querySelector('[data-term-source]');
    state.range = body.querySelector('[data-term-range]');
    state.range.onsubmit = event => { event.preventDefault(); load(state); };
    body.onclick = event => {
      const el = event.target.closest('button');
      if (el?.hasAttribute('data-term-row')) drill(state, Number(el.dataset.termRow));
      if (el?.hasAttribute('data-term-more')) load(state,true);
      if (el?.hasAttribute('data-term-source-more')) drill(state,Number(el.dataset.termSourceMore),true);
    };
    const upload = body.querySelector('[data-term-upload]');
    if (upload) upload.onsubmit = async event => {
      event.preventDefault();
      const file=upload.querySelector('input').files[0], button=upload.querySelector('button'), message=body.querySelector('[data-term-message]');
      if (!file || button.disabled) return;
      const data = new FormData(); data.set('file',file); data.set('import_type','advertising');
      data.set('required_grain','search_term');
      button.disabled=true;
      try {
        const result = await api('/api/products/'+encodeURIComponent(product.id)+'/imports',{method:'POST',body:data});
        if (active !== state) return;
        message.textContent='已导入 '+result.row_count+' 条搜索词源行。'; upload.reset(); await load(state);
      } catch(e) { if(active===state) message.textContent=e.message; }
      finally { button.disabled=false; }
    };
    load(state);
  }
  return {mount,unmount};
})();
