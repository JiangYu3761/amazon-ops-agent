// Browser entry and live posted quantities, against the disposable API from check_workbench.py.
const assert = require('node:assert/strict');
const {randomBytes,randomUUID} = require('node:crypto');
const {chromium} = require('playwright');
async function main() {
  const origin=process.env.WORKBENCH_BROWSER_URL;assert.equal(new URL(origin).hostname,'127.0.0.1');
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try {
    const ctx=await browser.newContext(),anonymous=await browser.newContext();
    async function post(path,data) {
      const r=await ctx.request.post(origin+path,{headers:{'X-Workflow-Request':'1'},data});
      assert(r.ok(),await r.text());return r.json();
    }
    async function action(path,data) {return post('/api/erp-receiving'+path,{request_id:randomUUID(),...data});}
    assert.equal((await anonymous.request.get(origin+'/api/erp-queue')).status(),401);
    await post('/api/workflow-auth/setup',{email:'queue@local.example',password:randomBytes(24).toString('hex'),
      display_name:'仓库验收',setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    const scope=await action('/scopes',{account_ref:'Queue Demo',site:'US',business_timezone:'America/Los_Angeles',warehouse:'Test-work'});
    const order=await action('/orders',{scope_id:scope.id,number:'Q-PO',supplier:'测试供方',lines:[{sku:'SKU-1',qty:15}]});
    const line=order.lines[0].id;
    const first=await action('/orders/'+order.id+'/notices',{lines:[{line_id:line,qty:5}]});
    const delivery=await action('/orders/'+order.id+'/notices',{lines:[{line_id:line,qty:10}]});
    const notice=delivery.notices.find(n=>n.id!==first.notices[0].id);
    const page=await ctx.newPage();page.setDefaultTimeout(6000);
    const queue=page.locator('#erpQueueDialog');
    async function visit() {
      await page.goto(origin+'/#overview');
      await page.locator('[data-erp-queue-open]').click();
    }
    await visit();
    const pending=queue.locator('tr').filter({hasText:notice.number});
    await pending.getByText('10 件').waitFor();
    assert.match(await pending.innerText(),/待收货/);
    await pending.locator('button').click();
    await page.locator('[data-rx-form=receive]').waitFor();
    assert.equal(await page.locator('[data-rx-select=notice]').inputValue(),notice.id);
    await action('/notices/'+first.notices[0].id+'/cancel',{reason:'仅保留第二张已到仓通知'});
    await action('/notices/'+notice.id+'/receive',{mode:'normal',lines:[{line_id:line,qty:8}]});
    await visit();
    await pending.getByText('8 件').waitFor();assert.match(await pending.innerText(),/待质检/);
    await action('/notices/'+notice.id+'/quality',{mode:'inspect',lines:[{line_id:line,good:4,bad:1}]});
    await queue.locator('[data-queue-refresh]').click();await pending.getByText('3 件').waitFor();
    await action('/notices/'+notice.id+'/quality',{mode:'inspect',lines:[{line_id:line,good:3,bad:0}]});
    await queue.locator('[data-queue-refresh]').click();await queue.getByText(/暂无符合/).waitFor();
    assert.equal(await pending.count(),0);
    console.log('经营总览真实待办、仓库单据跳转、实收转待检、部分质检余额与办结消失通过。');
  } finally {await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
