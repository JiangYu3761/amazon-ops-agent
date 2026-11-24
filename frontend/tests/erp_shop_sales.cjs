// Run with scripts/check_workbench.py on a fresh database. Exercises two real signed-in Chromium sessions.
const assert=require('node:assert/strict');
const {randomUUID,randomBytes}=require('node:crypto');
const {chromium}=require('playwright');
async function main(){
  const origin=process.env.WORKBENCH_BROWSER_URL;assert.equal(new URL(origin).hostname,'127.0.0.1');
  const headers={'X-Workflow-Request':'1'},browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try{
    const admin=await browser.newContext(),member=await browser.newContext(),password=randomBytes(24).toString('hex');
    async function post(ctx,path,data){const r=await ctx.request.post(origin+path,{headers,data});assert(r.ok(),await r.text());return r.json();}
    await post(admin,'/api/workflow-auth/setup',{email:'sales-admin@local.example',display_name:'财务',password,setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    const m=await post(admin,'/api/workflow-auth/members',{email:'sales-reader@local.example',display_name:'门店运营',department:'测试',password});
    await post(member,'/api/workflow-auth/login',{email:'sales-reader@local.example',password});
    for(const [account,site,zone,warehouses] of [['North','US','America/Los_Angeles',['west','east']],
      ['South','US','America/New_York',['S']],['British','GB','Europe/London',['GB']],['Unreported','GB','Europe/London',['N']]]){
      for(const warehouse of warehouses)await post(admin,'/api/erp-receiving/scopes',{request_id:randomUUID(),account_ref:account,site,
        business_timezone:zone,warehouse,member_ids:account==='North'?[m.id]:[]});
    }
    async function visit(ctx){const p=await ctx.newPage();p.setDefaultTimeout(7000);await p.goto(origin+'/#overview');
      await p.locator('[data-erp-shop-sales-open]').click();const dlg=p.locator('#erpShopSalesDialog');
      await dlg.locator('[data-ss-results]').getByText('店铺明细',{exact:true}).waitFor();return {p,dlg};}
    const {dlg}=await visit(admin);
    await dlg.locator('summary').click();
    const template=decodeURIComponent((await dlg.locator('a[download]').getAttribute('href')).split(',')[1]).trim();
    const lines=[template,'North,US,USD,2020-02-28,shop_day,complete,100,4,3','North,US,USD,2020-02-29,shop_day,complete,120,6,5',
      'South,US,USD,2020-02-28,shop_day,complete,10,2,2','South,US,USD,2020-02-29,shop_day,complete,30,3,2',
      'British,GB,GBP,2020-02-28,shop_day,complete,0,0,','British,GB,GBP,2020-02-29,shop_day,complete,8,1,1'];
    const csv=lines.join('\n'), corrected=csv.replace(',120,6,5',',140,6,5'), ep=origin+'/api/erp-shop-sales';
    async function upload(data,name,code){await dlg.locator('[type=file]').setInputFiles({name,mimeType:'text/csv',buffer:Buffer.from(data)});
      const response=dlg.page().waitForResponse(r=>r.url()===ep+'/imports');
      await dlg.getByRole('button',{name:'导入日报'}).click();const done=await response;
      assert.equal(done.status(),code,await done.text());await dlg.locator('[data-ss-upload] button:enabled').waitFor();return done.json();}
    await upload(csv,'original.csv',201);
    await upload(corrected,'revision.csv',201);
    assert.equal((await upload(csv,'retry.csv',201)).duplicate,true);
    async function dates(d){await d.locator('[name=end_date]').fill('2020-02-29');await d.locator('[name=days]').fill('1');
      await d.getByRole('button',{name:'查询两期'}).click();await d.locator('[data-ss-results]').getByText(/上期 2020-02-28/).waitFor();}
    await dates(dlg);
    async function row(d,store){const r=d.locator('tr').filter({has:d.page().getByRole('button',{name:store,exact:true})});return r.locator('td').allInnerTexts();}
    assert.deepEqual((await row(dlg,'North')).slice(1,7),['USD','140','100','40','40%','可比']);
    assert.deepEqual((await row(dlg,'British')).slice(1,7),['GBP','8','0','8','—','上期为零']);
    const sub=dlg.locator('table').first().locator('tr').filter({hasText:'USD'});
    assert.deepEqual((await sub.locator('td').allInnerTexts()).slice(0,5),['USD','2','170','110','60']);
    await dlg.getByRole('button',{name:'North',exact:true}).click();
    await dlg.locator('[data-ss-source]').getByText(/revision.csv · 第 3 行/).waitFor();
    const {dlg:mine}=await visit(member);await dates(mine);
    assert.equal(await mine.getByRole('button',{name:'South',exact:true}).count(),0);
    assert.equal(await mine.getByRole('button',{name:'North',exact:true}).count(),1);
    assert(!/GBP|British|Unreported/.test(await mine.locator('[data-ss-results]').innerText()));
    assert.equal(await mine.locator('[data-ss-upload]').count(),0);
    const q='?days=1&end_date=2020-02-29';
    const only=await (await member.request.get(ep+q)).json();
    assert.equal(only.stores.length,1);assert.equal(only.subtotals.length,1);assert.equal(Number(only.subtotals[0].metrics.ordered_revenue.current.value),140);
    const denied=await member.request.get(ep+'/source?account_ref=South&site=US&currency=USD&start_date=2020-02-28&end_date=2020-02-29');
    assert.equal(denied.status(),404);
    assert.equal((await member.request.post(ep+'/imports',{headers,multipart:{file:{name:'reader.csv',mimeType:'text/csv',buffer:Buffer.from(csv)}}})).status(),403);
    const report=await (await admin.request.get(ep+q)).json();
    const noData=report.stores.find(s=>s.account_ref==='Unreported');
    assert.equal(noData.metrics.ordered_revenue.current.value,null);
    assert.equal(report.stores.find(s=>s.account_ref==='British').metrics.orders.difference,null);
    const partial=await (await admin.request.get(ep+'?days=2&end_date=2020-02-29')).json();
    assert.equal(partial.stores.find(s=>s.account_ref==='North').metrics.ordered_revenue.change_percent,null);
    // An invalid row rejects the entire file without disturbing prior values.
    await upload(corrected.replace('South,US,USD,2020-02-28','Unknown,US,USD,2020-02-28'),'bad.csv',404);
    console.log('多店经营：真实管理员导入、双仓去重、两用户访问隔离、USD/GBP 原币分组、闰日回溯与修正版重试通过。');
  }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
