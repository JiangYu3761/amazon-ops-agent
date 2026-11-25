// Static frontend: node frontend/tests/today_dashboard.cjs
// Real API: set WORKBENCH_TEST_PG to a local test PostgreSQL database and run
// WORKBENCH_BROWSER_SCRIPT=frontend/tests/today_dashboard.cjs python scripts/check_workbench.py
const assert = require('node:assert/strict');
const { randomBytes, randomUUID } = require('node:crypto');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');

const frontend = path.resolve(__dirname, '..');
const liveOrigin = process.env.WORKBENCH_BROWSER_URL;
const isLive = Boolean(liveOrigin);
const scopeName = 'QA-TODAY-WH';

async function startStaticSite() {
  const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' };
  const server = http.createServer(async (request, response) => {
    const { pathname } = new URL(request.url, 'http://localhost');
    if (pathname.startsWith('/api/')) {
      response.writeHead(404, { 'Content-Type': 'application/json' });
      response.end('{"detail":"Static frontend"}');
      return;
    }
    const filename = path.resolve(frontend, (pathname === '/' ? './index.html' : '.' + pathname));
    if (!filename.startsWith(frontend + path.sep)) { response.writeHead(404); response.end(); return; }
    try {
      const data = await fs.readFile(filename);
      response.writeHead(200, { 'Content-Type': mime[path.extname(filename)] || 'application/octet-stream' });
      response.end(data);
    } catch { response.writeHead(404); response.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return server;
}

async function seed(context, origin) {
  assert.equal(new URL(origin).hostname, '127.0.0.1', 'Only the isolated local test API is supported.');
  async function post(route, data) {
    const response = await context.request.post(origin + route, { data, headers: { 'X-Workflow-Request': '1' } });
    assert(response.ok(), route + ': ' + await response.text());
    return response.json();
  }
  await post('/api/workflow-auth/setup', {
    email: 'today@testing.example', display_name: '首页验收', password: randomBytes(24).toString('hex'),
    setup_code: process.env.WORKFLOW_SETUP_TOKEN,
  });
  await post('/api/erp-receiving/scopes', {
    request_id: randomUUID(), account_ref: 'QA-TODAY-SHOP', site: 'US',
    business_timezone: 'America/Los_Angeles', warehouse: scopeName, member_ids: [],
  });
  const product = await post('/api/products', {
    model: 'TODAY-SCOPE', name: '今日工作台验收', marketplace: 'US', sku: 'QA-TODAY',
  });
  const task = await post('/api/tasks', {
    product_id: product.id, title: '核对当日订单凭证', module_code: 'service',
    due_date: new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' }),
  });
  return { product, task };
}

async function checkMobile(page, desk) {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().right <= 1);
  const cards = await desk.locator('[data-wb-desk]').evaluateAll(buttons => buttons.map(button => {
    const box = button.getBoundingClientRect();
    const centerElement = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    return { left: box.left, right: box.right, top: box.top, bottom: box.bottom, height: box.height, clickable: button.contains(centerElement) };
  }));
  assert(cards.every(box => box.left >= 0 && box.right <= 390 && box.top >= 0 && box.bottom <= 844 && box.height >= 44 && box.clickable));
  assert(cards[1].top >= cards[0].bottom && cards[2].top >= cards[1].bottom);
}

async function checkHome(context, origin, fixtures) {
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [], requests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    const { pathname } = new URL(request.url());
    if (pathname.startsWith('/api/')) requests.push({ method: request.method(), pathname });
  });
  await page.goto(origin + '/');
  const desk = page.getByRole('region', { name: '今日工作台' });
  const goHome = async () => {
    await page.locator('#navigation [data-view=overview]').click();
    await desk.waitFor();
  };
  const open = label => desk.getByRole('button', { name: label }).click();
  await desk.waitFor();
  assert.equal(await desk.locator('[data-wb-desk]').count(), 3);
  assert(await desk.locator('time').getAttribute('datetime'));
  const box = await desk.boundingBox();
  const tabs = await page.locator('#workbenchRoot .wb-nav').boundingBox();
  assert(box.y < 350 && tabs.y >= box.y + box.height - 1);

  await desk.getByRole('button', { name: '订单履约' }).focus();
  await page.keyboard.press('Enter');
  const fulfillment = page.locator('#erpFbmDesk[open]');
  await fulfillment.waitFor();
  assert.equal(new URL(page.url()).hash, '#service');
  if (isLive) {
    await fulfillment.locator('select[name=scope]').waitFor();
    assert((await fulfillment.innerText()).includes(scopeName));
  } else {
    assert((await fulfillment.innerText()).includes('连接工作台服务器'));
  }
  await page.goBack();
  await desk.waitFor();
  await fulfillment.waitFor({ state: 'hidden' });
  await open('订单履约');
  await fulfillment.getByRole('button', { name: '关闭' }).click();
  await goHome();

  await open('采购收货');
  const receiving = page.locator('#erpReceivingRoot');
  await receiving.getByRole('heading', { name: '仓库收货' }).waitFor();
  assert.equal(new URL(page.url()).hash, '#inventory');
  if (isLive) {
    await receiving.locator('[data-rx-select=scope] option').filter({ hasText: scopeName }).waitFor({ state: 'attached' });
  } else {
    assert((await receiving.innerText()).includes('需要连接服务器'));
  }
  await goHome();

  await open('广告效果复核');
  const reviews = page.locator('#adsReviewRoot');
  await reviews.waitFor();
  assert.equal(new URL(page.url()).hash, '#ads');
  const productSelect = page.locator('#productSelect');
  assert.equal(await productSelect.inputValue(), 'all');
  if (isLive) {
    await reviews.getByText('请在顶部选择具体产品，查看该产品的广告复盘。').waitFor();
    await page.waitForFunction(() => document.activeElement.id === 'productSelect');
    assert(!requests.some(request => request.pathname.endsWith('/ads-reviews')));
    await productSelect.selectOption(fixtures.product.id);
    await reviews.locator('[data-ar=lane]').first().waitFor();
    await goHome();
    const dueTask = desk.getByRole('button', { name: fixtures.task.title });
    assert.equal(await dueTask.getAttribute('data-id'), fixtures.task.id);
    const taskRequest = page.waitForResponse(r => new URL(r.url()).pathname === '/api/approvals/tasks/' + fixtures.task.id);
    await dueTask.click();
    assert((await taskRequest).ok());
    await page.locator('#approvalPortal #oaTitle').waitFor();
    await page.keyboard.press('Escape');
    await page.goto(origin + '/');
    await desk.waitFor();
    await productSelect.selectOption(fixtures.product.id);
    await open('广告效果复核');
    await page.waitForFunction(() => document.activeElement.id === 'adsReviewRoot');
    assert(requests.some(request => request.pathname === '/api/products/' + fixtures.product.id + '/ads-reviews'));
  } else {
    assert.match(await reviews.innerText(), /示例|服务器/);
    await goHome();
    await productSelect.selectOption({ index: 1 });
    await open('广告效果复核');
    await reviews.waitFor();
  }
  await goHome();
  await page.locator('#workbenchRoot [data-wb=legacy]').click();
  assert.equal(await desk.count(), 0);
  assert(await page.locator('#viewRoot').isVisible());
  await page.locator('#workbenchRoot [data-wb-section=""]').click();
  await desk.waitFor();
  await checkMobile(page, desk);
  if (!isLive) {
    await page.evaluate(() => {
      localStorage.setItem('homa.ops.workspace.v4', JSON.stringify({
        schema: 4, products: [], tasks: [], people: [], events: [], dependencies: [], rankings: [], queries: [],
        imports: [], actions: [], documents: [], evidence_links: [], records: { sales: [], advertising: [], inventory: [], costs: [] },
      }));
    });
    await page.reload();
    await desk.waitFor();
    assert.equal(await desk.locator('[data-wb-desk]').count(), 3);
    assert.match(await desk.innerText(), /没有到期的未完成任务/);
  }
  assert(!requests.some(request => !['GET', 'HEAD'].includes(request.method)));
  assert.deepEqual(errors, []);
}

(async () => {
  const site = isLive ? null : await startStaticSite();
  const origin = liveOrigin || 'http://127.0.0.1:' + site.address().port;
  let browser;
  try {
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium' });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const fixtures = isLive ? await seed(context, origin) : null;
    await checkHome(context, origin, fixtures);
    console.log((isLive ? 'PostgreSQL' : 'Static frontend') + ' today dashboard routes, return navigation, scope and mobile checks passed.');
  } finally {
    if (browser) await browser.close();
    if (site) await new Promise(resolve => site.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
