// Invoke with WORKBENCH_BROWSER_SCRIPT=frontend/tests/erp_comparison.cjs via scripts/check_workbench.py.
const assert = require('node:assert/strict');
const { randomBytes } = require('node:crypto');
const { chromium } = require('playwright');

async function main() {
  const origin = process.env.WORKBENCH_BROWSER_URL;
  assert.equal(new URL(origin).hostname, '127.0.0.1');
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium' });
  try {
    const logged = await browser.newContext();
    const headers = { 'X-Workflow-Request': '1' };
    async function post(route, body) {
      const response = await logged.request.post(origin + route, { headers, ...body });
      assert(response.ok(), await response.text());
      return response.json();
    }
    await post('/api/workflow-auth/setup', { data: { email: 'compare@local.example', password: randomBytes(24).toString('hex'),
      display_name: '经营环比验收', setup_code: process.env.WORKFLOW_SETUP_TOKEN } });
    const product = await post('/api/products', { data: { model: 'CMP1', name: '环比测试型号', sku: 'CMP1-US' } });
    await post('/api/products/' + product.id + '/imports', { multipart: { import_type: 'sales', file: {
      name: 'comparison.csv', mimeType: 'text/csv', buffer: Buffer.from(
        'date,sku,units_ordered,ordered_revenue,currency\n2020-02-28,CMP1-US,1,65,USD\n2020-02-29,CMP1-US,2,80,USD\n') } } });
    const page = await logged.newPage();
    await page.goto(origin + '/#overview');
    await page.locator('[data-wb="comparison"]').click();
    const panel = page.locator('#erpComparisonRoot');
    await panel.getByText(/请选择|上方选择/).waitFor();
    await page.locator('#productSelect').selectOption(product.id);
    await panel.locator('[name=end_date]').fill('2020-02-29');
    await panel.locator('[name=days]').fill('1');
    const received = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname.endsWith('/analytics/comparison') && url.searchParams.get('end_date') === '2020-02-29';
    });
    await panel.getByRole('button', { name: '比较两个期间' }).click();
    assert.equal((await received).status(), 200);
    await panel.getByText(/上期 2020-02-28 至 2020-02-28/).waitFor();
    const revenue = panel.locator('tr').filter({ has: page.getByText('订购销售额', { exact: true }) });
    const cells = await revenue.locator('td').allInnerTexts();
    assert.deepEqual(cells.slice(0, 4), ['80 USD', '65 USD', '—', '—']);
    assert.match(cells[4], /部分数据/);
    await revenue.locator('summary').click();
    await revenue.getByText(/覆盖信息来源 CSV/).waitFor();
    await panel.locator('[name=end_date]').fill('2099-01-01');
    assert.equal(await panel.locator('form').evaluate(form => form.checkValidity()), false);
    await page.locator('[data-wb-section="risk_triage"]').click();
    assert.equal(await panel.count(), 0);

    const demo = await browser.newContext();
    const preview = await demo.newPage();
    const comparisonCalls = [];
    await preview.route('**/api/**', route => {
      if (route.request().url().includes('/analytics/comparison')) comparisonCalls.push(route.request().url());
      return route.fulfill({ status: 404, json: { detail: 'static demo' } });
    });
    await preview.goto(origin);
    await preview.locator('[data-wb="comparison"]').click();
    await preview.locator('#erpComparisonRoot').getByText(/连接服务器后/).waitFor();
    assert.deepEqual(comparisonCalls, []);
    console.log('总览 → 经营环比已用真实本地 API、已导入 CSV、闰日及离线状态验证。');
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
