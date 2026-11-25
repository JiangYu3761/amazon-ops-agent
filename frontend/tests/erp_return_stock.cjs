// Browser acceptance against a freshly migrated local database via scripts/check_workbench.py.
const assert = require('node:assert/strict');
const {randomBytes, randomUUID} = require('node:crypto');
const {chromium} = require('playwright');
const origin = process.env.WORKBENCH_BROWSER_URL;
assert.equal(new URL(origin).hostname, '127.0.0.1');

async function api(ctx, method, path, data, expected=200) {
  const r=await ctx.request.fetch(origin+path,{method,data,headers:{'X-Workflow-Request':'1'}});
  assert.equal(r.status(),expected,await r.text());
  return r.json();
}

(async()=>{
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try {
    const admin=await browser.newContext();
    const password=randomBytes(20).toString('hex');
    await api(admin,'POST','/api/workflow-auth/setup',{email:'returns@example.test',password,display_name:'仓库核对',
      setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    const product=await api(admin,'POST','/api/products',{model:'QA-RETURN',name:'退货库存测试',marketplace:'US',sku:'CAM-R'},201);
    const account='store-R';
    async function warehouse(name){
      return api(admin,'POST','/api/erp-receiving/scopes',{request_id:randomUUID(),account_ref:account,site:'US',
        warehouse:name,business_timezone:'America/Los_Angeles',member_ids:[]},201);
    }
    const from=await warehouse('接收仓'),to=await warehouse('补货仓');
    const refund=await api(admin,'POST','/api/erp-refunds/lines',{product_id:product.id,account_ref:account,
      order_ref:'RA-ORDER',line_ref:'ROW-A',sku:'CAM-R',currency:'USD',refundable_amount:'50.00'},201);
    const base='/api/erp-returns/lines/'+refund.id+'/packages';
    const pack=await api(admin,'POST',base,{package_ref:'RMA-18',ordered_units:9,expected_units:9,
      order_evidence:'RA-ORDER/ROW-A',channel:'local'},201);
    await api(admin,'POST',base+'/'+pack.id+'/events',{kind:'receive',event_ref:'dock-18',evidence_ref:'scan-18',
      occurred_on:'2026-11-24',evidence_type:'local_record',received:9},201);
    const p=await admin.newPage(),errors=[];
    p.on('pageerror',error=>errors.push(error.message));
    await p.goto(origin+'/#service');
    await p.locator('#workflowIdentity .pw-identity').waitFor();
    await p.locator('[data-erp-refunds-open]').click();
    const box=p.locator('.rfd-dialog');
    await box.locator('[data-rfd=item][data-id="'+refund.id+'"]').click();
    await box.locator('[data-rfd=packages]').click();
    await box.locator('[data-rfd=package][data-id="'+pack.id+'"]').click();
    const form=box.locator('form[data-rfd-form=dispose]');
    await form.locator('..').locator('summary').click();
    for(const [name,value] of Object.entries({good:'5',defective:'3',destroyed:'1',event_ref:'inspection-1',
      evidence_ref:'本地复核记录A',occurred_on:'2026-11-24'}))await form.locator('[name="'+name+'"]').fill(value);
    await form.locator('button').click();
    const post=box.locator('[data-rfd-form=stock]');
    await post.locator('[name=scope_id]').waitFor();
    assert.equal(await post.locator('[name=scope_id]').inputValue(),'');
    await post.locator('[name=scope_id]').selectOption(from.id);
    await post.locator('button').click();
    await box.getByText('已入本地库存 · 接收仓 · 良品 5 / 次品 3',{exact:true}).waitFor();
    assert.equal(await box.locator('[data-rfd-form=stock]').count(),0);
    const stored=async scope=>(await api(admin,'GET','/api/erp-stock/balances?scope_id='+scope.id+'&sku=CAM-R')).items[0];
    const stock=await stored(from);assert.equal(stock.good,5);assert.equal(stock.bad,3);
    await api(admin,'POST','/api/erp-stock/transfers',{request_id:randomUUID(),from_scope_id:from.id,to_scope_id:to.id,
      sku:'CAM-R',qty:5,reference:'MOVE-R',evidence:'仓库交接-18'},201);
    assert.equal((await stored(to)).good,5);
    assert.equal((await stored(from)).good,0);
    assert.equal((await stored(from)).bad,3);
    const final=await api(admin,'GET','/api/erp-refunds/lines/'+refund.id);
    assert.equal(final.recorded_amount,'0.00');assert.equal(final.remaining_amount,'50.00');
    assert.deepEqual(errors,[]);
    console.log('本地处置实际仓手选、良次入库、销毁排除、良品调拨、退款独立通过');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
