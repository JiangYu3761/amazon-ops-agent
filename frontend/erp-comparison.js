/* Read-only comparison in the product overview. Calculations and coverage come from the API. */
window.erpComparison = (() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  const statuses = { ready: '可比', estimated: '估算', zero_base: '零基数', partial: '部分数据',
    missing: '缺少数据', unavailable: '不可用', undefined: '不可计算' };
  const units = { units: '件', count: '次' };
  let active;

  function unmount() {
    if (active) active.node.replaceChildren();
    active = null;
  }

  function latestClosedDay(timezone) {
    const parts = new Intl.DateTimeFormat('en-CA', {
      year: 'numeric', month: '2-digit', day: '2-digit', timeZone: timezone
    }).formatToParts(new Date());
    const get = type => Number(parts.find(part => part.type === type).value);
    return new Date(Date.UTC(get('year'), get('month') - 1, get('day') - 1)).toISOString().slice(0, 10);
  }

  function valueText(value, unit, currency) {
    if (value == null) return '—';
    const amount = Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 4 });
    return escape(amount + (unit === 'money' ? ' ' + currency : unit === 'percent' ? '%' : units[unit] || ''));
  }

  function sourceDetail(observation) {
    const parts = observation.sources.map(source => {
      const observed = source.dataset === 'costs' ? '' :
        '，已确认完整 ' + source.complete_days + ' 天，已见记录 ' + source.observed_days + ' 天';
      const stamp = source.last_synced_at ? '，来源更新时间 ' + source.last_synced_at : '';
      const sourceKind = { api: 'API', csv: 'CSV' }[source.coverage_source];
      const origin = sourceKind ? '（覆盖信息来源 ' + sourceKind + '）' : '';
      return escape(source.label + origin + observed + stamp);
    });
    return parts.join('；') + (observation.reason ? '。' + escape(observation.reason) : '');
  }

  function show(target, report) {
    const { previous, current } = report.periods;
    const range = period => escape(period.start_date) + ' 至 ' + escape(period.end_date);
    const table = report.rows.map(row => {
      const showValue = value => valueText(value, row.unit, report.product.currency);
      return '<tr><th scope="row"><details><summary>' + escape(row.label) +
        '</summary><small>计算依据 ' + escape(row.current.formula) + '<br>本期 ' + sourceDetail(row.current) +
        '<br>上期 ' + sourceDetail(row.previous) + '</small></details></th><td>' + showValue(row.current.value) +
        '</td><td>' + showValue(row.previous.value) + '</td><td>' + showValue(row.difference) +
        '</td><td>' + valueText(row.change_percent, 'percent') + '</td><td>' +
        escape(statuses[row.status] || row.status) + (row.reason ? '<small>' + escape(row.reason) + '</small>' : '') + '</td></tr>';
    }).join('');
    target.innerHTML = '<p>本期 ' + range(current) + '　上期 ' + range(previous) +
      '　业务时区 ' + escape(current.timezone) + '</p><div class="table-wrap"><table class="wb-table">' +
      '<thead><tr><th>指标及来源</th><th>本期</th><th>上期</th><th>差额</th><th>环比</th><th>状态</th></tr></thead>' +
      '<tbody>' + table + '</tbody></table></div><p>环比 ' + escape(report.formula) +
      '。' + escape(report.comparison_policy) +
      ' <a href="https://www.lingxing.com/help/article/manageBoard" target="_blank" rel="noreferrer">领星销售看板计算说明</a></p>';
  }

  async function refresh(state) {
    const sequence = ++state.sequence;
    const params = new URLSearchParams(new FormData(state.form));
    const target = state.node.querySelector('[data-comparison-result]');
    target.textContent = '正在读取两个期间的产品指标…';
    try {
      const path = '/api/products/' + encodeURIComponent(state.productId) + '/analytics/comparison?' + params;
      const report = await state.api(path);
      if (active === state && sequence === state.sequence) show(target, report);
    } catch (error) {
      if (active === state && sequence === state.sequence) {
        target.textContent = '读取失败：' + error.message + '。可调整期间后重新查询。';
      }
    }
  }

  function mount(node, context) {
    const { mode, product, user, api, login } = context;
    const key = [mode, product?.id, product?.business_timezone, user?.id].join('|');
    if (active?.node === node && active.key === key) return;
    unmount();
    const state = { node, key, api, productId: product?.id, sequence: 0 };
    active = state;
    const heading = '<h2>经营环比</h2>';
    let message;
    if (mode !== 'server') message = '连接服务器后可对比产品已导入或同步的数据。';
    else if (!user) message = '请登录协作账户查看产品指标。';
    else if (!product) message = '请在上方选择一个产品型号，以该产品的币种和业务时区比较。';
    if (message) {
      node.innerHTML = '<section class="panel wb-queue">' + heading + '<p>' + message + '</p></section>';
      if (mode === 'server' && !user && login) {
        const button = document.createElement('button');
        button.className = 'btn';
        button.textContent = '登录协作账户';
        button.onclick = login;
        node.querySelector('section').append(button);
      }
      return;
    }
    const lastDay = latestClosedDay(product.business_timezone || 'America/Los_Angeles');
    node.innerHTML = '<section class="panel wb-queue">' + heading +
      '<form class="wb-toolbar"><label>本期截止日<input name="end_date" type="date" required max="' +
      lastDay + '" value="' + lastDay + '"></label><label>每期天数<input type="number" min="1" max="366" step="1"' +
      ' name="days" value="7" required></label><button type="submit" class="btn primary">比较两个期间</button></form>' +
      '<div data-comparison-result aria-live="polite"></div></section>';
    state.form = node.querySelector('form');
    state.form.onsubmit = event => { event.preventDefault(); refresh(state); };
    refresh(state);
  }

  return { mount, unmount };
})();
