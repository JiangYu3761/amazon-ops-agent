const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {randomUUID,randomBytes}=require('node:crypto');
async function main(){
  const origin=process.env.WORKBENCH_BROWSER_URL;
  assert.equal(new URL(origin).hostname,'127.0.0.1');
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try{
    const ctx=await browser.newContext();
    async function post(path,data){
      const response=await ctx.request.post(origin+path,{headers:{'X-Workflow-Request':'1'},data});
      assert.ok(response.ok(),await response.text());return response.json();
    }
    await post('/api/workflow-auth/setup',{email:'stock@local.example',password:randomBytes(20).toString('hex'),display_name:'仓管验收',setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    const recv=(suffix,data)=>post('/api/erp-receiving'+suffix,{request_id:randomUUID(),...data});
    const scope=warehouse=>recv('/scopes',{account_ref:'STOCK1',site:'US',warehouse,business_timezone:'America/Los_Angeles',member_ids:[]});
    const a=await scope('A'),b=await scope('B');
    const order=await recv('/orders',{scope_id:a.id,number:'STOCK-IN-01',supplier:'S',lines:[{sku:'STOCK-A',qty:10}]});
    const line=order.lines[0].id;
    const notice=await recv('/orders/'+order.id+'/notices',{lines:[{line_id:line,qty:10}]});
    const inbound=await recv('/notices/'+notice.notices[0].id+'/receive',{mode:'quick',lines:[{line_id:line,qty:10,good:9,bad:1}]});
    const page=await ctx.newPage();page.setDefaultTimeout(6500);
    await page.goto(origin+'/#inventory');await page.locator('[data-erp-stock-open]').click();
    const desk=page.getByRole('dialog',{name:'库存余额与调拨'});
    async function check(scope,good,bad){
      const loaded=page.waitForResponse(response=>{
        const url=new URL(response.url());
        return url.pathname==='/api/erp-stock/balances'&&url.searchParams.get('scope_id')===scope.id;
      });
      const select=desk.locator('[name=scope_id]');
      if(await select.inputValue()===scope.id){
        await desk.getByRole('button',{name:'查询余额'}).click();
      }else{await select.selectOption(scope.id);}
      await loaded;
      const row=desk.getByRole('row').filter({has:page.getByRole('cell',{name:'STOCK-A',exact:true})});
      await row.getByRole('cell',{name:String(good),exact:true}).waitFor();
      assert.deepEqual(await row.locator('td').allInnerTexts(),['STOCK-A','0',String(good),String(bad)]);
    }
    await check(a,9,1);await desk.getByText('登记实际调拨',{exact:true}).click();
    const f=desk.locator('[data-kind=transfer]');
    await f.locator('[name=to_scope_id]').selectOption(b.id);
    await f.getByLabel('调拨 SKU').fill('STOCK-A');await f.getByLabel('件数').fill('5');
    await f.getByLabel('调拨单号').fill('XFER-55');await f.getByLabel('交接凭证').fill('SIGNED-55');
    await f.getByRole('button',{name:'确认调拨'}).click();
    await desk.getByRole('status').getByText(/已登记调拨 XFER-55/).waitFor();
    await check(a,4,1);await check(b,5,0);
    await desk.getByText('XFER-55 · STOCK-A × 5',{exact:true}).waitFor();
    const reversal=await ctx.request.post(origin+'/api/erp-receiving/events/'+inbound.notices[0].events[0].id+'/reverse',
      {headers:{'X-Workflow-Request':'1'},data:{request_id:randomUUID(),reason:'检验负库存保护'}});
    assert.equal(reversal.status(),409);
    await page.reload();await page.locator('[data-erp-stock-open]').click();await check(a,4,1);
    console.log('真实库存界面通过：入库良9/次1，跨仓调5，双仓核对4/5，源收货冲销受保护。');
  }finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
