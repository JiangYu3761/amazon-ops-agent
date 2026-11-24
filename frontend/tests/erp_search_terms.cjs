// Real HTTP, browser import, and PostgreSQL on the disposable server created by check_workbench.py.
const assert = require('node:assert/strict');
const { randomBytes } = require('node:crypto');
const { chromium } = require('playwright');

async function main() {
  const origin=process.env.WORKBENCH_BROWSER_URL;
  assert.equal(new URL(origin).hostname,'127.0.0.1');
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium'});
  try {
    const ctx=await browser.newContext(), headers={'X-Workflow-Request':'1'};
    async function post(path,opts) {
      const r=await ctx.request.post(origin+path,{headers,...opts});
      assert(r.ok(),await r.text());return r.json();
    }
    await post('/api/workflow-auth/setup',{data:{email:'term@local.example',display_name:'测试广告人员',
      password:randomBytes(24).toString('hex'),setup_code:process.env.WORKFLOW_SETUP_TOKEN}});
    const product=await post('/api/products',{data:{model:'TERMS1',name:'广告测试'}}), page=await ctx.newPage();
    const base='/api/products/'+product.id, lookup=origin+base+'/erp/search-terms';
    const anon=await browser.newContext();assert.equal((await anon.request.get(lookup)).status(),401);
    await page.goto(origin+'/#ads');await page.locator('#productSelect').selectOption(product.id);
    await page.locator('[data-wb="searchTerms"]').click();
    const panel=page.locator('#erpSearchTermsRoot'), summary=panel.locator('[data-term-list]');
    await summary.getByText(/尚无符合/).waitFor();
    await panel.locator('summary').click();
    const template=decodeURIComponent((await panel.locator('a[download]').getAttribute('href')).split(',')[1]).trim();
    const data=[template,
      '2020-03-01,A,100,10,10,50,2,USD,search_term,P1,SP,C1,G1,K1,EXACT,cold brew,7',
      '2020-03-02,B,300,30,30,100,4,USD,search_term,P1,SB,C2,G2,K2,PHRASE,cold brew,7',
      '2020-03-01,A,0,0,3,0,0,USD,search_term,P1,SP,C1,G1,K1,EXACT,cold brew,14',
      '2020-03-01,A,1,0,4,0,0,USD,search_term,P2,SP,C1,G1,K1,EXACT,cold brew,7'].join('\n');
    async function upload(csv,status) {
      await panel.locator('[type=file]').setInputFiles({name:'keyword-report.csv',mimeType:'text/csv',buffer:Buffer.from(csv)});
      const sent=page.waitForResponse(r=>new URL(r.url()).pathname===base+'/imports');
      await panel.getByRole('button',{name:'导入当前产品'}).click();
      const resp=await sent;assert.equal(resp.status(),status,await resp.text());
      await panel.locator('[data-term-upload] button:enabled').waitFor();
    }
    await upload(data,201);await upload(data,201);
    await panel.locator('[name=start_date]').fill('2020-03-01');
    await panel.locator('[name=end_date]').fill('2020-03-02');
    await panel.getByRole('button',{name:'查询',exact:true}).click();
    await summary.getByText('26.6667%').waitFor();
    assert.equal(await summary.locator('[data-term-row]').count(),3);
    const first=summary.locator('tbody tr').first();
    assert.deepEqual((await first.locator('td').allInnerTexts()).slice(2),['400','40','40','150','6','26.6667%','1','10%','2']);
    await first.locator('button').click();
    const detail=panel.locator('[data-term-source]');
    await detail.getByText(/keyword-report.csv · 第 2 行/).waitFor();
    await detail.getByText(/keyword-report.csv · 第 3 行/).waitFor();
    assert.equal(await detail.locator('tbody tr').count(),2);
    const zero=summary.locator('tbody tr').filter({hasText:'14 天'});
    assert.deepEqual((await zero.locator('td').allInnerTexts()).slice(7,10),['—','—','—']);
    const legacy='date,campaign_name,entity_name,impressions,clicks,spend,sales,orders,currency\n2020-03-01,A,cold brew,900,90,100,300,4,USD';
    await upload(legacy,422);
    await post(base+'/imports',{multipart:{import_type:'advertising',file:{name:'legacy.csv',mimeType:'text/csv',buffer:Buffer.from(legacy)}}});
    await upload(data.replace(',USD,search_term',',CAD,search_term'),422);
    const summaryJson=await (await ctx.request.get(lookup+'?start_date=2020-03-01&end_date=2020-03-02&limit=1')).json();
    assert.equal(summaryJson.rows.length,1);assert.equal(summaryJson.next_offset,1);
    assert.equal(Number(summaryJson.rows[0].spend),40);
    const analytics=await (await ctx.request.get(origin+base+'/analytics?start_date=2020-03-01&end_date=2020-03-02')).json();
    assert.equal(analytics.modules.ads.metrics.find(m=>m.key==='spend').value,100);
    console.log('广告搜索词浏览器真实 CSV 导入、双渠道汇总、账户/窗口隔离、原行追溯、重传、零分母及旧数据隔离通过。');
  } finally {await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
