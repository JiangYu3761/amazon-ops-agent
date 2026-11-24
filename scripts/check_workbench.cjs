// Invoked by check_workbench.py on a freshly created local test database.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { randomBytes } = require('node:crypto');
const { chromium } = require('playwright');

const origin = process.env.WORKBENCH_BROWSER_URL;
assert.equal(new URL(origin).hostname, '127.0.0.1');
const headers = { 'X-Workflow-Request': '1' };
const prefix = '/api/workbench/items';
const definitions = ['growth', 'operations', 'workspace'].flatMap(group =>
  JSON.parse(fs.readFileSync('frontend/workbench-' + group + '.json', 'utf8')));
assert.equal(new Set(definitions.map(page => page.id)).size, 17);
const wb = name => '#wbDialog [data-wb="' + name + '"]';

async function api(context, method, path, data, status) {
  const response = await context.request.fetch(origin + path, { method, headers, data });
  const body = await response.text();
  assert.equal(response.status(), status, method + ' ' + path + ': ' + body.slice(0, 350));
  return body ? JSON.parse(body) : null;
}

async function waitForPost(page, path, action) {
  const response = page.waitForResponse(res =>
    new URL(res.url()).pathname === path && ['POST', 'PATCH'].includes(res.request().method()));
  await action();
  const received = await response;
  assert(received.ok(), await received.text());
  return received.json();
}

async function go(page, definition, product) {
  await page.goto(origin + '/#' + definition.id);
  await page.reload();
  await page.locator('#productSelect option[value="' + product + '"]').waitFor({ state: 'attached' });
  await page.locator('#productSelect').selectOption(product);
  await page.locator('#workbenchRoot [data-wb-section="' + definition.sections[0].id + '"]').click();
}

async function valueFor(input, value) {
  if (await input.evaluate(el => el.tagName === 'SELECT')) {
    const choices = await input.locator('option').evaluateAll(list => list.map(o => o.value).filter(Boolean));
    assert(choices.length, 'select needs a business choice');
    await input.selectOption(choices.includes(value) ? value : choices[0]);
  } else {
    await input.fill(String(value));
  }
}

async function checkServerAutosave(page, reader, productId, reviewerId) {
  const definition = definitions.find(page => page.id === 'process');
  const object = 'AUTOSAVE-HANDOFF-001';
  const title = '无需点保存的阶段交接';
  const scope = prefix + '?product_id=' + productId + '&page_id=process&section_id=handoff';
  const list = async () => (await api(reader, 'GET', scope, undefined, 200)).items;
  const baseline = (await list()).map(item => item.id).sort();
  const dialog = page.locator('#wbDialog');
  const input = name => dialog.locator('[name="' + name + '"]');
  const saved = page.locator('#wbMessage[data-save-state="saved"]');
  await go(page, definition, productId);
  await page.locator('#workbenchRoot [data-wb="new"]').click();
  await input('title').fill(title);
  await input('site').fill('US');
  // Allow more than two debounce periods for this deliberately incomplete draft.
  await page.waitForTimeout(2100);
  assert.deepEqual((await list()).map(item => item.id).sort(), baseline);
  assert.equal(await dialog.getAttribute('data-wb-item-id') || '', '');
  await input('reviewer_id').selectOption(reviewerId);
  await input('object_ref').fill(object);
  await saved.waitFor({ timeout: 10000 });
  const first = (await list()).filter(item => item.object_ref === object);
  assert.equal(first.length, 1);
  const id = first[0].id;
  const path = prefix + '/' + id;
  assert.equal(await dialog.getAttribute('data-wb-item-id'), id);
  const draft = await api(reader, 'GET', path, undefined, 200);
  assert.equal(draft.title, title);
  assert.equal(draft.status, 'draft');
  assert.equal(draft.reviewer_id, reviewerId);
  await go(page, definition, productId);
  await page.locator('[data-wb-id="' + id + '"]').click();
  assert.equal(await input('title').inputValue(), title);
  const changedTitle = title + '（二次编辑）';
  await input('title').fill(changedTitle);
  await input('from_stage').fill('备货');
  await input('to_stage').fill('入仓');
  await input('handoff_material').fill('自动保存凭据 H-222');
  await input('promised_date').fill('2026-12-01');
  await saved.waitFor({ timeout: 10000 });
  const revised = await api(reader, 'GET', path, undefined, 200);
  assert.equal(revised.title, changedTitle);
  assert.equal(revised.values.handoff_material, '自动保存凭据 H-222');
  assert(revised.version > draft.version);
  // Submit another revision immediately, before its save timer finishes.
  await input('handoff_material').fill('正式提交回执 H-223');
  const submitted = await waitForPost(page, path + '/actions', () => page.locator(wb('submit')).click());
  assert.equal(submitted.id, id);
  assert.equal(submitted.status, 'pending_review');
  const final = await api(reader, 'GET', path, undefined, 200);
  assert.equal(final.values.handoff_material, '正式提交回执 H-223');
  assert.equal(final.status, 'pending_review');
  assert.deepEqual((await list()).filter(item => item.object_ref === object).map(item => item.id), [id]);
  console.log('PASS: title alone stayed local, two sessions confirmed automatic database creation, revision, refresh, and submit on one ID');
}

async function saveAndRead(page, reader) {
  await page.locator(wb('save')).click();
  await page.locator('#wbMessage').filter({ hasText: /已写入服务器|已保存至服务器/ }).waitFor();
  const id = await page.locator('#wbDialog').getAttribute('data-wb-item-id');
  assert(id, 'Saved dialog should identify the persisted record.');
  return api(reader, 'GET', prefix + '/' + id, undefined, 200);
}

async function main() {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium' });
  try {
    const admin = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const reviewer = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const anonymous = await browser.newContext();
    const password = randomBytes(24).toString('hex');
    const reviewerEmail = 'reviewer@homa-e2e.example';
    assert.equal((await api(admin, 'GET', '/api/workflow-auth/session', undefined, 200)).initialized, false);
    await api(admin, 'POST', '/api/workflow-auth/setup', {
      email: 'owner@homa-e2e.example', display_name: '验收管理员', password, setup_code: process.env.WORKFLOW_SETUP_TOKEN,
    }, 200);
    const member = await api(admin, 'POST', '/api/workflow-auth/members', {
      email: reviewerEmail, display_name: '独立复核人', department: '测试', password,
    }, 201);
    await api(reviewer, 'POST', '/api/workflow-auth/login', { email: reviewerEmail, password }, 200);
    const product = await api(admin, 'POST', '/api/products', {
      model: 'E2E-WORKBENCH', name: '隔离验收型号', marketplace: 'US', sku: 'QA0001',
    }, 201);
    const page = await admin.newPage();
    const second = await reviewer.newPage();
    const errors = [];
    for (const target of [page, second]) target.on('pageerror', error => errors.push(error.message));
    await checkServerAutosave(page, reviewer, product.id, member.id);
    let inventory;
    for (const definition of definitions) {
      const section = definition.sections[0];
      await go(page, definition, product.id);
      for (const candidate of definition.sections) {
        await page.locator('#workbenchRoot [data-wb-section="' + candidate.id + '"]').click();
        await page.locator('#workbenchRoot [data-wb="new"]').click();
        const fields = page.locator('#wbItemForm [data-wb-field]');
        await fields.first().waitFor();
        assert.deepEqual(await fields.evaluateAll(inputs => inputs.map(input => input.dataset.wbField)),
          candidate.fields.map(field => field.key), definition.id + '/' + candidate.id);
        await page.locator(wb('close')).click();
      }
      await page.locator('#workbenchRoot [data-wb-section="' + section.id + '"]').click();
      await page.locator('#workbenchRoot [data-wb="new"]').click();
      const dialog = page.locator('#wbDialog');
      const title = definition.id === 'inventory' ? '美国站补货申请 · 11 月销售核算' : '隔离验收 · ' + definition.title;
      await dialog.locator('[name="title"]').fill(title);
      await valueFor(dialog.locator('[name="site"]'), 'US');
      if (definition.id !== 'inventory') await dialog.locator('[name="object_ref"]').fill('QA0001');
      await dialog.locator('[name="reviewer_id"]').selectOption(member.id);
      if (definition.id === 'inventory') {
        await go(page, definition, product.id);
        await page.locator('#workbenchRoot [data-wb="new"]').click();
        assert.equal(await dialog.locator('[name="title"]').inputValue(), title);
        assert.equal(await dialog.locator('[name="object_ref"]').inputValue(), '');
        await dialog.locator('[name="object_ref"]').fill('QA0001');
        assert.equal(await dialog.locator('[name="reviewer_id"]').inputValue(), member.id);
        const assist = await waitForPost(page, '/api/workbench/assist', () => page.locator(wb('assist')).click());
        assert.equal(assist.requires_human, true);
        assert.equal(assist.status, 'incomplete');
        assert(assist.missing.length > 0);
        assert(await page.locator('#wbAssistResult').isVisible());
        const fallback = await waitForPost(page, '/api/workbench/assist', () => page.locator(wb('model')).click());
        assert.equal(fallback.model_status, 'not_configured');
        assert.equal(fallback.model_draft, null);
        assert(fallback.suggestion && fallback.requires_human);
      }
      for (const field of section.fields) {
        const locator = dialog.locator('[data-wb-field="' + field.key + '"]');
        const sample = field.type === 'number' ? Math.min(field.max ?? Infinity, Math.max(field.min ?? 0, 10)) :
          field.type === 'date' ? '2026-11-24' : field.type === 'url' ? 'https://example.org/qa/source' :
          field.type === 'select' ? field.options[0] : '隔离验收资料';
        await valueFor(locator, sample);
      }
      if (definition.id === 'inventory') {
        const scenario = { suggested_qty: 168, requested_qty: 168, adjust_reason: '按整箱补齐，未人工调量。',
          quote_ref: '测试报价 Q-117 · 24 件/箱，确认交期 14 天',
          sold_qty: 140, closed_days: 14, available_qty: 90, eligible_inbound: 0,
          lead_days: 14, safety_days: 3, review_days: 7, min_order: 96, pack_size: 24 };
        for (const [key, value] of Object.entries(scenario)) {
          await valueFor(dialog.locator('[data-wb-field="' + key + '"]'), value);
        }
        const result = await waitForPost(page, '/api/workbench/assist', () => page.locator(wb('assist')).click());
        const figures = Object.fromEntries(result.findings.map(finding => [finding.label, finding.value]));
        assert.equal(figures['现货覆盖天数'], '9');
        assert.equal(figures['手工情景补货触发线'], '170');
        assert.equal(figures['手工情景补货量'], '168');
      }
      let item = await saveAndRead(page, reviewer);
      assert.equal(item.status, 'draft');
      await go(page, definition, product.id);
      const row = page.locator('[data-wb-id="' + item.id + '"]');
      await row.waitFor();
      assert((await row.textContent()).includes(title) || (await row.locator('..').textContent()).includes(title));
      if (definition.id === 'inventory') {
        await row.click();
        await page.locator('#wbDialog [name=title]').fill(title + '（修订）');
        item = await saveAndRead(page, reviewer);
        assert.equal(item.title, title + '（修订）');
        assert(item.version > 1);
        inventory = { item, definition };
        if (process.env.WORKBENCH_SCREENSHOT) {
          await page.locator('#wbDialog .wb-stamp').scrollIntoViewIfNeeded();
          await page.evaluate(() => document.fonts.ready);
          await page.screenshot({ path: process.env.WORKBENCH_SCREENSHOT, animations: 'disabled' });
        }
      }

    }
    const types = definitions.reduce((total, page) => total + page.sections.length, 0);
    console.log('PASS: 17 routes, ' + types + ' typed forms and 17 persisted drafts after reload');
    const { item, definition } = inventory;
    const actionPath = prefix + '/' + item.id + '/actions';
    await api(anonymous, 'GET', prefix + '/' + item.id, undefined, 401);
    let live = item;
    async function as(actorPage, action, expected) {
      await go(actorPage, definition, product.id);
      await actorPage.locator('[data-wb-id="' + live.id + '"]').click();
      await actorPage.locator(wb(action)).waitFor();
      const note = actorPage.locator('#wbDialog [name="note"]');
      const evidence = actorPage.locator('#wbDialog [data-wb-action-evidence]');
      if (await note.isVisible()) await note.fill('已核对隔离验收资料');
      if (await evidence.isVisible()) await evidence.fill('https://example.org/qa/proof');
      live = await waitForPost(actorPage, actionPath, () => actorPage.locator(wb(action)).click());
      assert.equal(live.status, expected);
    }
    await as(page, 'submit', 'pending_review');
    await api(admin, 'POST', actionPath, { action:'approve', expected_version:live.version, note:'测试权限' }, 403);
    await as(second, 'return', 'draft');
    await as(page, 'submit', 'pending_review');
    await as(second, 'approve', 'approved');
    await as(page, 'execute', 'executed');
    await as(second, 'verify', 'closed');
    const result = await api(admin, 'GET', prefix + '/' + item.id, undefined, 200);
    assert.equal(result.status, 'closed');
    assert(result.history.length >= 6);
    await go(page, definition, product.id);
    await page.locator('[data-wb-id="' + item.id + '"]').click();
    await page.locator('#wbDialog .wb-follow summary').click();
    await page.locator('#wbDialog [name="follow_page"]').selectOption('finance');
    await page.locator('#wbDialog [name="follow_object"]').fill('QA-COST-SOURCE');
    await page.locator(wb('follow')).click();
    await page.waitForURL(/#finance$/);
    const child = await waitForPost(page, prefix, () => page.locator(wb('save')).click());
    assert.equal(child.parent_id, item.id);
    assert.equal(child.parent_ref.id, item.id);
    const parentLink = page.locator('#wbDialog [data-ref="' + item.id + '"]');
    await parentLink.click();
    await page.locator('#wbDialog [data-ref="' + child.id + '"]').waitFor();
    assert.equal(new URL(page.url()).hash, '#inventory');
    const overview = definitions.find(data => data.id === 'overview');
    const quote = '补货资料需核实实际供应商回执' + '持续逐项核实来源材料'.repeat(14);
    const otherQuote = '下周补货以另一家供应商的交货回执为准';
    const unknown = '纸张颜色有待商议';
    assert(quote.length > 110);
    async function routeNote(excerpt) {
      await go(page, overview, product.id);
      await page.locator('#workbenchRoot [data-wb-section=""]').click();
      const triage = page.locator('#wb-routing');
      await triage.locator('summary').click();
      await triage.locator('textarea').fill([quote, otherQuote, unknown].join('。'));
      await triage.getByText('识别并预览').click();
      const unmatched = triage.locator('.wb-routing-row').filter({ hasText: unknown });
      assert.equal(await unmatched.getByLabel('建议办理板块').inputValue(), '');
      const matched = triage.locator('.wb-routing-row').filter({ hasText: excerpt });
      assert.equal(await matched.getByLabel('建议办理板块').inputValue(), 'inventory');
      await matched.getByText('打开办理草稿').click();
      await page.waitForURL(/#inventory$/);
      assert.equal(await page.locator('#wbDialog [data-wb-source]').textContent(), excerpt);
    }
    await routeNote(quote);
    const routedTitle = page.locator('#wbDialog [name=title]');
    assert.equal(await routedTitle.inputValue(), quote.slice(0, 110));
    const editedTitle = '经营人员已核实交货来源';
    await routedTitle.fill(editedTitle);
    await routeNote(otherQuote);
    assert.equal(await routedTitle.inputValue(), otherQuote);
    await routeNote(quote);
    assert.equal(await routedTitle.inputValue(), editedTitle);
    const routed = await waitForPost(page, prefix, () => page.locator(wb('save')).click());
    assert.equal(routed.page_id, 'inventory');
    assert.equal(routed.status, 'draft');
    assert.equal(routed.title, editedTitle);
    assert.equal(routed.source_note, quote);
    const savedSource = await api(admin, 'GET', prefix + '/' + routed.id, undefined, 200);
    assert.equal(savedSource.source_note, quote);
    console.log('PASS: human-confirmed note routing, unknown sentence, cross-page follow-up with bidirectional trace, no-model fallback');
    const shop = definitions.find(data => data.id === 'shop');
    await go(page, shop, 'all');
    await page.locator('#workbenchRoot [data-wb-section="case"]').click();
    await page.locator('#workbenchRoot [data-wb="new"]').click();
    const shopForm = page.locator('#wbDialog');
    await shopForm.locator('[name="record_scope"]').selectOption('shop');
    const account = 'E2E-ACCOUNT';
    const site = 'US';
    const zone = 'America/Los_Angeles';
    await shopForm.locator('[name="account_ref"]').fill(account);
    await shopForm.locator('[name="site"]').fill(site);
    await shopForm.locator('[name="business_timezone"]').fill(zone);
    await shopForm.locator('[name="title"]').fill('无产品的平台 Case');
    await shopForm.locator('[name="object_ref"]').fill('CASE-001');
    const shopBrief = await waitForPost(page, '/api/workbench/assist', () => page.locator(wb('assist')).click());
    assert(shopBrief.summary.includes(account));
    assert.deepEqual(shopBrief.metrics, []);
    const caseItem = await saveAndRead(page, reviewer);
    assert.equal(caseItem.product_id, null);
    assert.equal(caseItem.account_ref, account);
    assert.equal(caseItem.site, site);
    assert.equal(caseItem.business_timezone, zone);
    const shopItems = await api(admin, 'GET', prefix + '?scope=shop&account_ref=' + account + '&site=' + site, undefined, 200);
    assert(shopItems.items.some(record => record.id === caseItem.id));
    const productItems = await api(admin, 'GET', prefix + '?product_id=' + product.id, undefined, 200);
    assert(!productItems.items.some(record => record.id === caseItem.id));
    await go(page, shop, 'all');
    await page.locator('#workbenchRoot [data-wb-section="case"]').click();
    await page.locator('#workbenchRoot [data-wb-shop-filter]').selectOption('shop');
    await page.locator('#workbenchRoot [data-wb-id="' + caseItem.id + '"]').click();
    assert.equal(await shopForm.locator('[name="title"]').inputValue(), '无产品的平台 Case');
    console.log('PASS: store-only Case, scoped rules with no product metrics, independent shop persistence and filtering');
    const calendar = definitions.find(data => data.id === 'calendar');
    await go(page, calendar, product.id);
    await page.locator('#workbenchRoot [data-wb="legacy"]').click();
    const grid = page.locator('[data-cal-swipe]');
    const heading = page.locator('.wb-cal-head h2');
    await page.locator('[data-action=calendar-layout][data-layout=week]').click();
    assert.equal(await grid.locator('.calendar-day').count(), 7);
    const week = await heading.textContent();
    await grid.press('ArrowRight');
    assert.notEqual(await heading.textContent(), week);
    await grid.press('ArrowLeft');
    assert.equal(await heading.textContent(), week);
    await page.locator('[data-action=calendar-layout][data-layout=month]').click();
    assert.equal(await grid.locator('.calendar-day').count(), 42);
    const month = await heading.textContent();
    const touch = await admin.newCDPSession(page);
    await touch.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 });
    const weekday = grid.locator('.calendar-week').nth(3);
    await weekday.scrollIntoViewIfNeeded();
    const box = await weekday.boundingBox();
    const x = Math.round(box.x + box.width / 2), y = Math.round(box.y + box.height / 2);
    await touch.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
    await touch.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x - 100, y }] });
    await touch.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForFunction(before => document.querySelector('.wb-cal-head h2').textContent !== before, month);
    await touch.detach();
    await page.locator('[data-action=calendar-zoom][data-step="1"]').click();
    assert.equal(await page.locator('.wb-cal-options output').textContent(), '125%');
    await page.locator('[data-action=calendar-zoom][data-step="-1"]').click();
    assert.equal(await page.locator('.wb-cal-options output').textContent(), '100%');
    await grid.locator('button[data-action=day-events]').first().click();
    await page.locator('.wb-cal-daily').waitFor();
    await page.locator('[data-action=close-modal]:visible').last().click();
    await page.locator('[data-action=calendar-layout][data-layout=agenda]').click();
    await page.locator('.wb-cal-agenda').waitFor();
    console.log('PASS: month touch swipe, week keyboard paging, zoom, day details and agenda');
    await page.setViewportSize({ width: 390, height: 844 });
    await go(page, definition, product.id);
    const extent = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    assert(extent[0] <= extent[1] + 1, 'Mobile page extends beyond the viewport: ' + extent);
    assert.deepEqual(errors, []);

    console.log('PASS: incomplete evidence guidance, designated reviewer, return, resubmit, execute, verify and persisted audit history');
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
