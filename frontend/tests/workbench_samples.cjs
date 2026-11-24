// Run with Node, Playwright and Chromium. All requests are intercepted on loopback.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const origin = 'http://127.0.0.1:44991';
const sampleKey = 'homa.workbench.demo.samples.1';
const recordKey = 'homa.workbench.records.1';
const draftKey = 'homa.workbench.draft.1/demo/op/inventory/replenish/new/all';
const mime = { '.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript', '.json': 'application/json' };

async function test() {
  const examples = JSON.parse(await fs.readFile(path.join(root, 'workbench-samples.json')));
  const pages = (await Promise.all(['growth', 'operations', 'workspace'].map(async group =>
    JSON.parse(await fs.readFile(path.join(root, 'workbench-' + group + '.json')))))).flat();
  assert.deepEqual(examples.map(s => s.page_id).sort(), pages.map(p => p.id).sort());
  assert.equal(new Set(examples.map(s => s.id)).size, 17);
  for (const sample of examples) {
    assert.equal(sample.id, 'demo-sample:' + sample.page_id);
    assert.match(sample.title, /^示例 · /);
    assert(pages.find(p => p.id === sample.page_id).sections.some(s => s.id === sample.section_id));
  }

  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium' });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    page.setDefaultTimeout(6000);
    const calls = [];
    let workspace;
    const existing = {
      id: 'legacy-replenishment', product_id: 'demo-g11', page_id: 'inventory', section_id: 'replenish',
      status: 'draft', version: 1, title: '我的补货记录', site: 'US', object_ref: '我的补货编号',
      owner_id: 'op', reviewer_id: 'supply', values: {}, history: []
    };
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort();
      calls.push([route.request().method(), url.pathname]);
      if (url.pathname.startsWith('/api/')) {
        if (!workspace) return route.fulfill({ status: 404, json: { detail: 'Static site' } });
        if (url.pathname === '/api/workspace') return route.fulfill({ json: workspace });
        if (url.pathname === '/api/workflow-auth/session') {
          return route.fulfill({ json: { initialized: true, user: { id: 'op', display_name: '测试用户', is_admin: true } } });
        }
        if (url.pathname === '/api/workbench/items') {
          return route.fulfill({ json: { items: [{ ...existing, id: 'server-item', title: '服务器工作单' }], has_more: false } });
        }
        return route.fulfill({ status: 404, json: { detail: 'No fixture for this endpoint' } });
      }
      const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      if (!/^[a-zA-Z0-9_.-]+$/.test(name)) return route.fulfill({ status: 404 });
      try {
        const body = await fs.readFile(path.join(root, name));
        return route.fulfill({ body, contentType: mime[path.extname(name)] || 'application/octet-stream' });
      } catch {
        return route.fulfill({ status: 404 });
      }
    });

    // First visit must include a meaningful queue for the default actor.
    await page.goto(origin);
    await page.locator('[data-wb-id="demo-sample:overview"]').waitFor();
    for (const sample of examples) {
      const nav = sample.page_id === 'settings' ? '.sidebar-settings' : '#navigation [data-view="' + sample.page_id + '"]';
      await page.locator(nav).click();
      await page.locator('[data-wb-section="' + sample.section_id + '"]').click();
      const row = page.locator('[data-wb-id="' + sample.id + '"]');
      await row.waitFor();
      assert.equal(await row.textContent(), sample.title);
    }
    const inventory = examples.find(s => s.page_id === 'inventory');
    await page.locator('#navigation [data-action="navigate"][data-view="inventory"]').click();
    await page.locator('[data-wb-id="demo-sample:inventory"]').click();
    assert.equal(await page.locator('#wbDialog [data-wb-field="suggested_qty"]').inputValue(), '168');
    await page.locator('#wbDialog [data-wb="close"]').click();

    // Simulate a pre-upgrade user with a saved record, an unsaved local form, and an edited sample.
    const edited = { ...inventory, version: inventory.history.length, title: '我的演练备注' };
    const oldDraft = { version: 0, form: { title: '下次继续填写的补货草稿' } };
    await page.evaluate(({ records, draft, keys }) => {
      localStorage.setItem(keys[0], JSON.stringify(records));
      localStorage.setItem(keys[1], JSON.stringify(draft));
    }, { records: [existing, edited], draft: oldDraft, keys: [recordKey, draftKey] });
    await page.reload();
    const ownRecord = page.locator('[data-wb-id="legacy-replenishment"]');
    await ownRecord.waitFor();
    assert.equal(await ownRecord.textContent(), existing.title);
    const sampleRow = page.locator('[data-wb-id="demo-sample:inventory"]');
    assert.equal(await sampleRow.count(), 1);
    assert.equal(await sampleRow.textContent(), edited.title);
    await page.locator('[data-wb-section="replenish"]').click();
    await page.locator('[data-wb="new"]').click();
    assert.equal(await page.locator('#wbDialog [name="title"]').inputValue(), oldDraft.form.title);
    assert.deepEqual(await page.evaluate(key => JSON.parse(localStorage.getItem(key)), recordKey), [existing, edited]);
    assert.deepEqual(await page.evaluate(key => JSON.parse(localStorage.getItem(key)), draftKey), oldDraft);
    assert.equal(await page.evaluate(key => JSON.parse(localStorage.getItem(key)).length, sampleKey), 17);
    assert.equal(calls.filter(([, url]) => url === '/workbench-samples.json').length, 1);

    // Same browser origin now has a backend. Neither cached demo samples nor user demo data may leak.
    workspace = await page.evaluate(() => ws);
    calls.length = 0;
    await page.reload();
    await page.locator('[data-wb-id="server-item"]').waitFor();
    assert.equal(await page.locator('[data-wb-id^="demo-sample:"]').count(), 0);
    assert.equal(await ownRecord.count(), 0);
    assert(!calls.some(([method, url]) => url === '/workbench-samples.json' || method !== 'GET'));
    console.log('17 个示例首次可见；刷新无重复、已有业务单和表单草稿保留、已连接 API 时独立读取。');
  } finally {
    await browser.close();
  }
}
test().catch(error => { console.error(error); process.exitCode = 1; });
