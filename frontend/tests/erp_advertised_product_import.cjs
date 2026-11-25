// Import through the normal data center and read the resulting persisted totals via the analytics API.
const assert = require('node:assert/strict');
const {randomBytes} = require('node:crypto');
const fs = require('node:fs/promises');
const {chromium} = require('playwright');

async function main() {
  const origin=process.env.WORKBENCH_BROWSER_URL;
  assert.equal(new URL(origin).hostname,'127.0.0.1');
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try {
    const admin=await browser.newContext(), member=await browser.newContext(), anon=await browser.newContext();
    const headers={'X-Workflow-Request':'1'}, password=randomBytes(24).toString('hex');
    async function post(ctx,path,data) {
      const result=await ctx.request.post(origin+path,{headers,data});
      assert.ok(result.ok(),await result.text());
      return result.json();
    }
    await post(admin,'/api/workflow-auth/setup',{email:'admin@csv.example',display_name:'报告管理员',password,
      setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    await post(admin,'/api/workflow-auth/members',{email:'reader@csv.example',display_name:'报表成员',password,department:'运营'});
    await post(member,'/api/workflow-auth/login',{email:'reader@csv.example',password});
    const product=await post(admin,'/api/products',{name:'商品报表测试',model:'CSV-P',sku:'CSV-MAIN',marketplace:'US',currency:'USD'});
    await post(admin,'/api/integrations/connections',{name:'测试用禁用连接',provider:'ads_sp',account_key:'csv-account',
      ads_profile_id:'123456',enabled:false,mappings:[{product_id:product.id,sku:'CSV-ALT'}]});
    const base=origin+'/api/products/'+product.id, page=await admin.newPage();
    async function openCenter(p) {
      await p.goto(origin+'/#imports');
      await p.locator('#productSelect').selectOption(product.id);
      await p.locator('[data-wb="legacy"]').click();
      await p.locator('[data-action="import"][data-type="advertised_product"]').waitFor();
    }
    const logs=async()=>(await admin.request.get(base+'/imports')).json();
    await openCenter(page);
    const card=page.locator('.import-card').filter({has:page.getByRole('heading',{name:'SP 商品广告日报'})});
    const [download]=await Promise.all([page.waitForEvent('download'),card.getByRole('link').click()]);
    assert.equal(download.suggestedFilename(),'advertised_product-template.csv');
    const [heading,example]=(await fs.readFile(await download.path(),'utf8')).trim().split(/\r?\n/);
    const cols=heading.split(',');
    function csvLine(i,overrides={}) {
      const row=Object.fromEntries(cols.map((h,j)=>[h,example.split(',')[j]]));
      Object.assign(row,{date:'2024-01-10',sku:'CSV-ALT',ad_id:String(400+i),profile_id:'123456',account_key:'',marketplace:'',spend:'2.56'},overrides);
      return cols.map(h=>row[h]).join(',');
    }
    const batch=[heading,...Array.from({length:6},(_,i)=>csvLine(i))].join('\n');
    const file=text=>({name:'product-source.csv',mimeType:'text/csv',buffer:Buffer.from(text)});
    async function check(text,code) {
      await page.locator('#importFile').setInputFiles(file(text));
      const result=page.waitForResponse(r=>r.url()===base+'/imports'&&r.request().method()==='POST');
      await page.getByRole('button',{name:'校验文件'}).click();
      assert.equal((await result).status(),code);
    }
    const launch=()=>card.getByRole('button',{name:'校验并导入'}).click();
    await launch();
    await check([heading,csvLine(1),csvLine(2,{ad_id:''})].join('\n'),422);
    await page.getByRole('alert').filter({hasText:/第 3 行/}).waitFor();
    assert.ok(await page.locator('#commitImportBtn').isDisabled());
    await check([heading,csvLine(1,{account_key:'other-shop'})].join('\n'),422);
    await page.getByRole('alert').filter({hasText:/店铺或站点/}).waitFor();
    await check(batch,200);
    await page.getByText(/已核对 6 行.*预览前 5 行/).waitFor();
    assert.equal((await logs()).length,0);
    await page.locator('#importFile').setInputFiles(file(batch+'\n'));
    assert.ok(await page.locator('#commitImportBtn').isDisabled());
    await check(batch,200);
    let result=page.waitForResponse(r=>r.url()===base+'/imports'&&r.request().method()==='POST');
    await page.locator('#commitImportBtn').click();
    const persisted=await result;
    assert.equal(persisted.status(),201);
    const receipt=await persisted.json();
    assert.equal(receipt.row_count,6);
    await page.getByText('已导入 6 行有效数据。').waitFor();
    const report=await (await admin.request.get(base+'/analytics?start_date=2024-01-10&end_date=2024-01-10')).json();
    assert.equal(Number(report.modules.ads.metrics.find(m=>m.key==='spend').value),15.36);
    await page.reload();
    await page.locator('#productSelect').selectOption(product.id);
    if(!await card.isVisible())await page.locator('[data-wb="legacy"]').click();
    await page.getByRole('row').filter({hasText:'product-source.csv'}).getByRole('button').click();
    await page.getByText(/有效 6 行.*错误 0 行/).waitFor();
    const stored=page.locator('#importSourceRows');
    await stored.getByRole('heading',{name:'SP 商品广告日报 · 已存来源行'}).waitFor();
    assert.equal(await stored.locator('tbody tr').count(),6);
    assert.match(await stored.locator('tbody tr').last().innerText(),/405[\s\S]*CSV-ALT/);
    const sourceUrl=base+'/imports/'+receipt.id+'/rows';
    assert.equal((await anon.request.get(sourceUrl)).status(),401);
    assert.equal((await (await admin.request.get(sourceUrl+'?offset=3&limit=2')).json()).next_offset,5);
    const secondProduct=await post(admin,'/api/products',{name:'第二款测试',model:'CSV-P2'});
    assert.equal((await admin.request.get(origin+'/api/products/'+secondProduct.id+'/imports/'+receipt.id+'/rows')).status(),404);
    await page.getByRole('button',{name:'关闭',exact:true}).last().click();
    await launch();await check(batch,200);
    result=page.waitForResponse(r=>r.url()===base+'/imports'&&r.request().method()==='POST');
    await page.locator('#commitImportBtn').click();
    assert.equal((await (await result).json()).id,receipt.id);
    await page.getByText(/此文件已登记，沿用原导入记录/).waitFor();
    assert.equal((await logs()).length,1);
    const readerPage=await member.newPage();await openCenter(readerPage);
    await readerPage.locator('[data-type="advertised_product"]').click();
    await readerPage.locator('#importFile').setInputFiles(file(batch));
    await readerPage.getByRole('button',{name:'校验文件'}).click();
    await readerPage.getByText(/核对和导入 SP 商品报告需要流程管理员/).waitFor();
    assert.ok(await readerPage.locator('#commitImportBtn').isDisabled());
    for(const [ctx,status] of [[member,403],[anon,401]]) {
      for(const validating of [true,false]) {
        const form={import_type:'advertising',required_grain:'advertised_product',file:file(batch)};
        if(validating)form.validate_only='true';
        assert.equal((await ctx.request.post(base+'/imports',{headers,multipart:form})).status(),status);
      }
    }
    const legacyFiles={
      sales:['date,sku,units_ordered,ordered_revenue,currency','2024-01-11,CSV-MAIN,1,10,USD'],
      advertising:['date,campaign_name,entity_name,impressions,clicks,spend,sales,orders,currency','2024-01-11,Campaign,Ad group,30,2,1.01,4,1,USD'],
      inventory:['date,sku,fulfillable,reserved,inbound,unsellable','2024-01-11,CSV-MAIN,2,1,3,0'],
      costs:['date,sku,product_cost,first_mile_cost,fba_fee,referral_fee,other_cost,currency','2024-01-11,CSV-MAIN,1,1,1,1,0,USD']
    };
    for(const [type,rows] of Object.entries(legacyFiles)) {
      await page.locator('[data-action="import"][data-type="'+type+'"]').click();
      await page.locator('#importFile').setInputFiles({name:type+'.csv',mimeType:'text/csv',buffer:Buffer.from(rows.join('\n'))});
      await page.getByRole('button',{name:'校验文件'}).click();
      await page.getByText(/1 行有效 · 0 行错误/).waitFor();
      result=page.waitForResponse(r=>r.url()===base+'/imports'&&r.request().method()==='POST');
      await page.locator('#commitImportBtn').click();
      assert.equal((await result).status(),201);
      await page.locator('#importFile').waitFor({state:'hidden'});
    }
    assert.equal((await logs()).length,5);
    console.log('SP 商品 CSV 已从数据导入页实测下载、错误定位、完整预检、6 行入库、刷新、原文件重传及权限。四种旧 CSV 均经原页面成功。');
  } finally {await browser.close();}
}
main().catch(err=>{console.error(err);process.exitCode=1;});
