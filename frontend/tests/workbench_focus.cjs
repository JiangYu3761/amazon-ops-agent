// Run with WORKBENCH_BROWSER_SCRIPT=frontend/tests/workbench_focus.cjs using scripts/check_workbench.py.
const assert = require('node:assert/strict');
const {randomBytes, randomUUID} = require('node:crypto');
const {chromium} = require('playwright');

async function main() {
  const origin = process.env.WORKBENCH_BROWSER_URL;
  assert.equal(new URL(origin).hostname, '127.0.0.1');
  const browser = await chromium.launch({executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium'});
  try {
    const owner = await browser.newContext(), colleague = await browser.newContext(), anonymous = await browser.newContext();
    const password = randomBytes(24).toString('hex'), headers = {'X-Workflow-Request':'1'};
    async function request(context, method, path, data, status=200) {
      const response = await context.request.fetch(origin+path, {method, headers, data});
      const text = await response.text();
      assert.equal(response.status(), status, method+' '+path+' '+text.slice(0,350));
      return text ? JSON.parse(text) : null;
    }
    const auth = '/api/workflow-auth/';
    await request(owner,'POST',auth+'setup',{email:'focus-admin@local.example',display_name:'重点经办人',password,
      setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    const reviewer=await request(owner,'POST',auth+'members',{email:'focus-reader@local.example',display_name:'重点复核人',department:'运营',password},201);
    await request(colleague,'POST',auth+'login',{email:'focus-reader@local.example',password});
    const product=await request(owner,'POST','/api/products',{model:'FOCUS-CAL',name:'月历重点验证'},201);
    const other=await request(owner,'POST','/api/products',{model:'FOCUS-OTHER',name:'另一产品'},201);
    const page=await owner.newPage(), title='核对到仓数量和发票行';
    await page.goto(origin+'/#process');
    await page.locator('#productSelect').selectOption(product.id);
    await page.locator('#workbenchRoot [data-wb-section=handoff]').click();
    await page.locator('#workbenchRoot [data-wb=new]').click();
    const dialog=page.locator('#wbDialog'), field=name=>dialog.locator('[name="'+name+'"]');
    // Use stable days within the month shown by the browser, independent of when this test runs.
    const month=await page.evaluate(()=>{const d=new Date();return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0');});
    const first=month+'-10', next=month+'-16';
    const itemsPath='/api/workbench/items';
    const created=page.waitForResponse(r=>new URL(r.url()).pathname===itemsPath&&r.request().method()==='POST');
    await field('focus_date').fill(first);
    await field('title').fill(title);
    await field('site').fill('US');
    await field('reviewer_id').selectOption(reviewer.id);
    await field('object_ref').fill('QA-RECEIPT-FOCUS');
    const creating=await created;
    assert.equal(creating.status(),201,await creating.text());
    const item=await creating.json(), path=itemsPath+'/'+item.id;
    await dialog.locator('#wbMessage[data-save-state=saved]').waitFor();
    assert.equal((await request(owner,'GET',path)).focus_date,first);
    await dialog.locator('[data-wb=close]').click();
    const focusUrl=(pid,from=first,to=next)=>'/api/workbench/focus?'+new URLSearchParams({
      start_date:from,end_date:to,...(pid?{product_id:pid}:{})
    });
    const ids=async(pid,from,to)=>(await request(owner,'GET',focusUrl(pid,from,to))).items.filter(x=>x.id===item.id);
    assert.equal((await ids(product.id))[0].title,title);
    assert.equal((await ids(other.id)).length,0);
    assert.equal((await ids(null)).length,1);
    await request(anonymous,'GET',focusUrl(product.id),undefined,401);
    const draft=await request(owner,'GET',path);
    await request(colleague,'PATCH',path,{expected_version:draft.version,request_id:randomUUID(),focus_date:next},403);
    assert.equal((await request(owner,'GET',path)).focus_date,first);

    const shortcut='button[data-action="focus-item"][data-id="'+item.id+'"]';
    async function calendar() {
      await page.locator('#navigation [data-view=calendar]').click();
      const calendarTab=page.locator('#workbenchRoot [data-wb=legacy]');
      await calendarTab.waitFor();
      if(!await calendarTab.evaluate(b=>b.classList.contains('selected')))await calendarTab.click();
      await page.locator('.wb-cal-grid').waitFor();
    }
    async function day(date) {
      await page.locator('.wb-cal-grid button[data-action="day-events"][data-date="'+date+'"]').first().click();
      const modal=page.locator('#modal[open]');
      await modal.getByText(date+' · 工作清单',{exact:true}).waitFor();
      return modal;
    }
    async function openFocus(date) {
      const modal=await day(date);
      await modal.locator(shortcut).waitFor();
      assert.equal(await modal.locator(shortcut).count(),1);
      await modal.locator(shortcut).click();
      await dialog.waitFor();
      assert.equal(await field('title').inputValue(),title);
      assert.equal(await field('focus_date').inputValue(),date);
    }
    async function changeDate(value) {
      const patched=page.waitForResponse(r=>new URL(r.url()).pathname===path&&r.request().method()==='PATCH'&&
        r.request().postDataJSON()?.focus_date===(value||null));
      await field('focus_date').fill(value);
      const response=await patched;
      assert.equal(response.status(),200,await response.text());
      assert.equal((await response.json()).focus_date,value||null);
      await dialog.locator('[data-wb=close]').click();
    }
    async function absent(date) {
      const modal=await day(date);
      assert.equal(await modal.locator(shortcut).count(),0);
      await modal.getByRole('button',{name:'关闭弹窗'}).click();
    }
    await calendar();await openFocus(first);
    await changeDate(next);
    assert.equal((await ids(product.id,first,first)).length,0);
    assert.deepEqual((await ids(product.id)).map(x=>x.focus_date),[next]);
    await calendar();await absent(first);await openFocus(next);
    await changeDate('');
    assert.equal((await ids(product.id)).length,0);
    await page.reload();
    await page.locator('#productSelect').selectOption(product.id);
    await calendar();await absent(next);
    const after=await request(owner,'GET',path);
    assert.equal(after.focus_date,null);assert.equal(after.title,title);
    console.log('重点日期通过真实工作单建草稿、独立账户越权拒绝、月历定位原单、改期去重、清除与刷新回读。');
  } finally {await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
