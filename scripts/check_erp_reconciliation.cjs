// Run against the disposable test stack through scripts/check_workbench.py.
const assert = require('node:assert/strict');
const {randomBytes} = require('node:crypto');
const {chromium, firefox} = require('playwright');

async function checkOrderLine(page, host, productId) {
 await page.goto(host);await page.locator('#sidebar [data-view="service"]').last().click();
 await page.locator('[data-erp-refunds-open]').click();
 const d=page.locator('.rfd-dialog');
 await d.getByRole('button',{name:'登记订单行'}).click();
 if(productId)await d.getByLabel('产品与站点').selectOption(productId);
 for(const [key,value] of [['account_ref','SELLER-UI'],['order_ref','ORDER-UI'],['line_ref','LINE-A'],['sku','SKU-Z'],['refundable_amount','25.00']])await d.locator('[name="'+key+'"]').fill(value);
 await d.getByRole('button',{name:'保存订单行'}).click();
 await d.getByLabel('标记依据').fill('客服已记账待核对');await d.getByRole('button',{name:'添加人工标记'}).click();
 await d.getByText('人工标记 · 已标记').waitFor();
 for(const [ref,amount] of [['TX-UI-A','20.00'],['TX-UI-B','5.00']]){
   await d.locator('summary').click();
   await d.getByLabel('流水行唯一引用').fill(ref);await d.getByLabel('结算单或报表凭证编号').fill('STM-UI');
   await d.getByLabel('结算日期').fill('2026-10-01');await d.locator('[name="amount"]').fill(amount);
   await d.getByRole('button',{name:'登记该笔流水'}).click();
   await d.getByText('流水行 '+ref+' · 结算单 STM-UI').waitFor();
 }
 await d.getByLabel('撤销原因').fill('已收到两笔结算流水');await d.getByRole('button',{name:'撤销人工标记'}).click();
 await d.getByText('人工标记 · 未标记').waitFor();await d.getByText('流水已登记足额',{exact:true}).waitFor();
 await d.getByRole('button',{name:'返回订单行'}).click();
 await d.getByLabel('订单号').fill('ORDER-UI');await d.getByRole('button',{name:'筛选'}).click();
 await d.getByRole('button',{name:'查看明细'}).click();await d.getByRole('heading',{name:'外部结算流水 · 2 笔'}).waitFor();
 await d.getByRole('button',{name:'退货包裹与实收'}).click();
 await d.getByRole('button',{name:'登记退货包裹'}).click();
 for(const [key,value] of [['ordered_units','5'],['order_evidence','ORDER-UI / LINE-A'],['package_ref','RMA-PACK-A'],['expected_units','4']])await d.locator('[name="'+key+'"]').fill(value);
 await d.getByRole('button',{name:'保存包裹'}).click();await d.getByRole('heading',{name:'RMA-PACK-A · 本地仓'}).waitFor();
 for(const [kind,ref,qty] of [['receive','R1',2],['dispose','Q1',0],['receive','R2',2],['dispose','Q2',0]]){
    await d.locator('summary',{hasText:kind==='receive'?'登记实收':'登记处置'}).click();
    const f=d.locator('form[data-rfd-form="'+kind+'"]');
    await f.getByLabel('本笔记录唯一引用').fill(ref);await f.getByLabel('本地签收或质检凭证').fill('WMS record '+ref);
    await f.getByLabel('凭证业务日期').fill('2026-10-12');
    if(qty)await f.locator('[name="received"]').fill(String(qty));
    else if(ref==='Q1'){await f.locator('[name="good"]').fill('1');await f.locator('[name="defective"]').fill('1');}
    else {await f.locator('[name="defective"]').fill('1');await f.locator('[name="destroyed"]').fill('1');}
    await f.getByRole('button',{name:qty?'保存实收流水':'保存处置流水'}).click();
    await d.getByText(ref+' · 凭证 WMS record '+ref,{exact:true}).waitFor();
 }
 await d.getByText('良品 1 · 次品 2 · 销毁 1',{exact:true}).waitFor();
 await d.getByRole('button',{name:'返回包裹列表'}).click();await d.getByRole('button',{name:'返回退款明细'}).click();
 await d.getByText('流水已登记足额',{exact:true}).waitFor();

}

async function checkStatement(page, host) {
 await page.goto(host);await page.locator('#sidebar [data-view="finance"]').last().click();
 await page.locator('[data-erp-settlements-open]').click();
 const d=page.locator('dialog[aria-labelledby="stl-title"]');
 await d.getByRole('button',{name:'登记结算单'}).click();
 for(const [name,v] of Object.entries({account_ref:'SHOP-U',site:'US',currency:'USD',statement_ref:'NOV-S',
     payout_date:'2026-11-07',opening_balance:'10',closing_balance:'5',actual_payout:'68',payout_ref:'BANK-01'})){
     await d.locator('[name="'+name+'"]').fill(v);
 }
 await d.getByRole('button',{name:'保存结算单'}).click();await d.getByText('NOV-S · 待导入明细',{exact:true}).waitFor();
 const csv='account_ref,site,currency,statement_ref,entry_ref,kind,occurred_on,original_order_date,amount\nSHOP-U,US,USD,NOV-S,TX1,sale,2026-11-01,,100\nSHOP-U,US,USD,NOV-S,TX2,fee,2026-11-02,,-15\nSHOP-U,US,USD,NOV-S,TX3,refund,2026-11-02,2026-10-01,-20\n';
 async function upload(data){await d.locator('[name="file"]').setInputFiles({name:'nov.csv',mimeType:'text/csv',buffer:Buffer.from(data)});
     await d.getByRole('button',{name:'核验并导入'}).click();}
 await upload(csv);await d.getByRole('heading',{name:'交易明细 · 3 行'}).waitFor();
 await d.getByText('-2.00 USD',{exact:true}).waitFor();await d.getByText(/原单日 2026-10-01/).waitFor();
 await upload(csv);await d.getByText('该文件已导入，保留原交易。',{exact:true}).waitFor();
 await upload(csv.replace('TX2,fee','TX2,sale'));await d.getByRole('alert').filter({hasText:'CSV 第 3 行'}).waitFor();
 await d.getByRole('button',{name:'修订单头'}).click();await d.locator('[name="actual_payout"]').fill('70');await d.locator('[name="reason"]').fill('核对回单');
 await d.getByRole('button',{name:'保存结算单'}).click();await d.getByRole('heading',{name:'NOV-S · 金额相符'}).waitFor();
 const item=d.locator('li').filter({hasText:'TX2 ·'});
 await item.getByRole('button',{name:'费用分摊'}).click();
 const fee=page.locator('dialog[aria-labelledby="fee-title"]');
 await fee.getByRole('button',{name:'选择分摊对象'}).click();
 for(const model of ['REFUND-UI','ALLOC-UI']){
   await fee.locator('li').filter({hasText:model}).getByRole('button',{name:'加入'}).click();
 }
 await fee.getByLabel('REFUND-UI · 权重').fill('2');
 await fee.getByLabel('权重依据').fill('验收样例订单数 2 比 1');
 await fee.getByRole('button',{name:'计算预览'}).click();
 await fee.getByText('费用 15.00 USD · 分摊合计 15.00 USD').waitFor();
 await fee.getByText('10.00 USD',{exact:true}).waitFor();await fee.getByText('5.00 USD',{exact:true}).waitFor();
 await fee.getByRole('button',{name:'确认分摊入账'}).click();await fee.getByRole('heading',{name:'当前分摊'}).waitFor();
 await fee.getByRole('button',{name:'关闭'}).click();
 await d.getByRole('button',{name:'本单费用归集'}).click();
 await fee.getByText('10.00 USD',{exact:true}).waitFor();await fee.getByRole('button',{name:'关闭'}).click();
 await item.getByRole('button',{name:'冲正'}).click();
 await d.getByLabel('冲正原因').fill('费用调账');await d.getByRole('button',{name:'确认冲正'}).click();await d.getByRole('heading',{name:'交易明细 · 4 行'}).waitFor();
 await d.getByText('-15.00 USD',{exact:true}).waitFor();
 await d.getByRole('button',{name:'本单费用归集'}).click();
 await fee.getByText('尚无生效的费用分摊。').waitFor();await fee.getByRole('button',{name:'关闭'}).click();
 await d.getByRole('button',{name:'返回结算单'}).click();await d.getByLabel('发放日起').fill('2026-10-01');await d.getByLabel('发放日至').fill('2026-10-31');
 await d.getByRole('button',{name:'筛选'}).click();await d.getByText('当前范围内暂无结算单。',{exact:true}).waitFor();

}

async function main() {
  const host = process.env.WORKBENCH_BROWSER_URL;
  assert.equal(new URL(host).hostname,'127.0.0.1');
  const browser = process.env.WORKBENCH_TEST_BROWSER === 'firefox' ? await firefox.launch()
    : await chromium.launch({executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium'});
  try {
    const context = await browser.newContext();
    const options = {headers:{'X-Workflow-Request':'1'}};
    const setup = await context.request.post(host+'/api/workflow-auth/setup',{...options,data:{
      display_name:'核对员',email:'reconciliation@example.test',password:randomBytes(24).toString('hex'),
      setup_code:process.env.WORKFLOW_SETUP_TOKEN
    }});
    assert.equal(setup.status(),200,await setup.text());
    const product = await context.request.post(host+'/api/products',{...options,data:{
      model:'REFUND-UI',name:'退款样品',marketplace:'US',currency:'USD',sku:'SKU-Z'
    }});
    assert.ok(product.ok(),await product.text());
    const another=await context.request.post(host+'/api/products',{...options,data:{
      model:'ALLOC-UI',name:'分摊样品',marketplace:'US',currency:'USD',sku:'SKU-Y'
    }});
    assert.ok(another.ok(),await another.text());
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror',error=>errors.push(error.message));
    await checkOrderLine(page,host,(await product.json()).id);
    await checkStatement(page,host);
    assert.deepEqual(errors,[]);
    console.log('Verified independent refund, physical return and statement reconciliation flows.');
  } finally {
    await browser.close();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
