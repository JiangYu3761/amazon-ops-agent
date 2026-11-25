// WORKBENCH_BROWSER_SCRIPT=frontend/tests/triage_drafts.cjs python scripts/check_workbench.py
// The runner provisions an isolated local PostgreSQL database and a temporary API.
const assert=require('node:assert/strict');
const {randomUUID,randomBytes}=require('node:crypto');
const {chromium}=require('playwright');
const origin=process.env.WORKBENCH_BROWSER_URL;
assert.equal(new URL(origin).hostname,'127.0.0.1');
const prefix='/api/workbench/triage-drafts';
const headers={'X-Workflow-Request':'1'};
async function api(ctx,method,url,data,code=200){
  const r=await ctx.request.fetch(origin+url,{method,data,headers});
  const body=await r.text();
  assert.equal(r.status(),code,method+' '+url+' '+body.slice(0,200));
  return JSON.parse(body);
}
async function until(read,accept){
  for(let i=0;i<100;i++){const v=await read();if(accept(v))return v;await new Promise(resolve=>setTimeout(resolve,75));}
  throw Error('Expected saved draft was not observed');
}
function panel(page){return page.locator('#wb-routing');}
function area(page){return panel(page).getByRole('textbox',{name:'待分派的运营记录'});}
async function openPanel(page,product='all'){
  await page.goto(origin+'/#overview');
  await page.locator('#productSelect option[value="'+product+'"]').waitFor({state:'attached'});
  await page.locator('#productSelect').selectOption(product);
  await panel(page).locator('summary').waitFor();
  if(!await area(page).isVisible())await panel(page).locator('summary').click();
}
async function shop(page,account,site){
  const desk=panel(page);
  await desk.getByRole('button',{name:'切换草稿店铺'}).click();
  await desk.getByRole('textbox',{name:'草稿店铺标识'}).fill(account);
  await desk.getByRole('textbox',{name:'草稿站点'}).fill(site);
  await desk.getByRole('button',{name:'打开此范围草稿'}).click();
}
async function checkStatic(browser){
  const context=await browser.newContext();
  await context.route('**/api/**',route=>route.fulfill({status:404,contentType:'application/json',body:'{}'}));
  const page=await context.newPage();
  await openPanel(page);await area(page).fill('演示身份甲的采购原文');
  await page.reload();await openPanel(page);
  assert.equal(await area(page).inputValue(),'演示身份甲的采购原文');
  const second=await context.newPage();await openPanel(second);
  await Promise.all([area(page).fill('同浏览器 A 窗口原文'),area(second).fill('同浏览器 B 窗口原文')]);
  const third=await context.newPage();await openPanel(third);
  const options=await panel(third).getByLabel('已保存的原文与并行副本').locator('option').allTextContents();
  assert(options.some(v=>v.includes('A 窗口'))&&options.some(v=>v.includes('B 窗口')));
  await third.locator('[data-wb-actor]').selectOption({index:1});
  if(!await area(third).isVisible())await panel(third).locator('summary').click();
  assert.equal(await area(third).inputValue(),'');
  await third.evaluate(()=>{Storage.prototype.setItem=function(){throw Error('quota');};});
  await area(third).fill('浏览器拒绝持久保存');await panel(third).getByRole('status').getByText(/本机未能持久保存/).waitFor();
  assert.equal(await area(third).inputValue(),'浏览器拒绝持久保存');
  await context.close();
}
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
 try{
  await checkStatic(browser);
  const admin=await browser.newContext(),other=await browser.newContext(),device=await browser.newContext();
  const password=randomBytes(24).toString('hex'),email='private-note-admin@qa.example';
  await api(admin,'POST','/api/workflow-auth/setup',{email,display_name:'原文验收甲',password,setup_code:process.env.WORKFLOW_SETUP_TOKEN});
  const owner=(await api(admin,'GET','/api/workflow-auth/session')).user.id;
  const peer=await api(admin,'POST','/api/workflow-auth/members',{email:'private-note-peer@qa.example',display_name:'原文验收乙',department:'测试',password},201);
  await api(other,'POST','/api/workflow-auth/login',{email:'private-note-peer@qa.example',password});
  await api(device,'POST','/api/workflow-auth/login',{email,password});
  const product=await api(admin,'POST','/api/products',{model:'NOTE-QA',name:'原文范围验收',marketplace:'US',sku:'NOTE-QA'},201);
  const list=(ctx=admin,filter={})=>api(ctx,'GET',prefix+'?'+new URLSearchParams({actor_id:owner,...filter}));
  const waitNote=(ctx,filter,text)=>until(()=>list(ctx,filter),r=>r.items.some(i=>i.note===text&&!i.archived));
  const page=await admin.newPage();const exceptions=[];
  page.on('pageerror',e=>exceptions.push(e.message));
  await openPanel(page);
  const initial='采购原文，刷新后立即恢复';
  await area(page).fill(initial);await page.reload();await openPanel(page);
  await until(()=>area(page).inputValue(),v=>v===initial);
  await waitNote(admin,{},initial);
  const second=await device.newPage();await openPanel(second);
  await until(()=>area(second).inputValue(),v=>v===initial);
  await Promise.all([area(page).fill('设备甲的库存原文'),area(second).fill('设备乙的广告原文')]);
  const both=await until(()=>list(),r=>['设备甲的库存原文','设备乙的广告原文'].every(note=>r.items.some(i=>i.note===note)));
  assert.equal(both.items.filter(d=>!d.archived).length,2);
  await api(other,'GET',prefix+'?actor_id='+owner,undefined,403);
  assert.deepEqual((await list(other,{actor_id:peer.id})).items,[]);
  const anonymous=await browser.newContext();
  await api(anonymous,'GET',prefix+'?actor_id='+owner,undefined,401);await anonymous.close();
  await openPanel(page,product.id);
  assert.equal(await area(page).inputValue(),'');
  await shop(page,'Private-Shop','US');
  const scoped={product_id:product.id,account_ref:'Private-Shop',site:'US'};
  const note='补货需要核对在途数量和到货单';
  await area(page).fill(note);
  await waitNote(admin,scoped,note);
  assert.deepEqual((await list(admin,{...scoped,site:'CA'})).items,[]);
  assert.deepEqual((await list(admin,{product_id:product.id})).items,[]);
  await openPanel(second,product.id);await shop(second,'Private-Shop','US');
  await until(()=>area(second).inputValue(),v=>v===note);
  // The preview and failed downstream submit must never consume the source note.
  await panel(page).getByRole('button',{name:'识别并预览'}).click();
  await panel(page).locator('.wb-routing-row').first().getByRole('button',{name:'打开办理草稿'}).click();
  await page.locator('#wbDialog').waitFor();
  assert.equal(await page.locator('[data-wb-source]').textContent(),note);
  await page.locator('#wbDialog [data-wb=submit]').click();
  assert((await list(admin,scoped)).items.some(d=>d.note===note&&!d.archived));
  await page.locator('#wbDialog [data-wb=close]').click();
  await openPanel(page,product.id);
  await until(()=>area(page).inputValue(),v=>v===note);
  let disabled=true;
  await page.route('**/api/workbench/triage-drafts*',async route=>{
    if(route.request().method()==='PUT'&&disabled)return route.fulfill({status:503,contentType:'application/json',body:'{"detail":"测试断网"}'});
    return route.continue();
  });
  const pending='掉线时仍需保留的采购补充';
  await area(page).fill(pending);
  await panel(page).getByRole('status').getByText(/服务器未确认保存/).waitFor();
  await page.reload();await openPanel(page);
  await panel(page).getByRole('button',{name:/恢复上次草稿范围/}).click();
  await until(()=>area(page).inputValue(),v=>v===pending);
  assert.equal(await page.locator('#productSelect').inputValue(),product.id);
  disabled=false;await panel(page).getByRole('button',{name:'重试同步'}).click();await waitNote(admin,scoped,pending);
  await area(page).fill('');await panel(page).getByRole('button',{name:'恢复原文'}).click();
  assert.equal(await area(page).inputValue(),pending);
  page.once('dialog',d=>d.accept());await panel(page).getByRole('button',{name:'已办理，归档原文'}).click();
  await until(()=>list(admin,scoped),r=>r.items.some(d=>d.note===pending&&d.archived));
  assert.equal(await area(page).inputValue(),'');
  const archived=panel(page).getByLabel('已保存的原文与并行副本');
  const option=await archived.locator('option').filter({hasText:'已归档'}).first().getAttribute('value');
  await archived.selectOption(option);assert.equal(await area(page).inputValue(),pending);
  // Store operations inherit the verified account, site and time zone without borrowing a product.
  await api(admin,'POST','/api/workbench/items',{request_id:randomUUID(),page_id:'shop',section_id:'case',title:'建立店铺范围',
    site:'US',account_ref:'Private-Shop',business_timezone:'America/Los_Angeles',object_ref:'QA-SCOPE',values:{}},201);
  await openPanel(page,'all');await shop(page,'Private-Shop','US');
  const original='账户健康的通知紧急，需今天18:00前复核';
  await area(page).fill(original);
  await panel(page).getByRole('button',{name:'识别并预览'}).click();
  const row=panel(page).locator('.wb-routing-row').first();
  assert.match(await row.innerText(),/明确紧急「紧急」/);
  assert.match(await row.innerText(),/明确截止「今天18:00前」/);
  await row.getByLabel('办理前人工判断').selectOption('priority');
  await page.reload();await openPanel(page,'all');
  await until(()=>area(page).inputValue(),v=>v===original);
  await panel(page).getByRole('button',{name:'识别并预览'}).click();
  assert.equal(await row.getByLabel('办理前人工判断').inputValue(),'priority');
  await row.getByLabel('办理子页').selectOption('case');
  await row.getByRole('button',{name:'打开办理草稿'}).click();
  const form=page.locator('#wbDialog');
  await form.waitFor();
  assert.equal(await page.locator('#productSelect').inputValue(),'all');
  assert.equal(await form.locator('[name=account_ref]').inputValue(),'Private-Shop');
  assert.equal(await form.locator('[name=business_timezone]').inputValue(),'America/Los_Angeles');
  assert.equal(await form.locator('[data-wb-source]').textContent(),original);
  await form.getByText('已选为重点，请确认安排日期。').waitFor();
  assert.equal(await form.locator('[name=focus_date]').inputValue(),'');
  await form.locator('[name=object_ref]').fill('QA-CASE-SOURCE');
  await form.locator('[data-wb=save]').click();
  await until(()=>api(admin,'GET','/api/workbench/items?scope=shop'),r=>r.items.some(i=>i.object_ref==='QA-CASE-SOURCE'&&i.account_ref==='Private-Shop'&&!i.product_id));
  await form.locator('[data-wb=close]').click();await openPanel(page,'all');
  await until(()=>area(page).inputValue(),v=>v===original);
  // Exact retries have one receipt; two different stale writes produce a recoverable second branch.
  const write={actor_id:owner,product_id:product.id,account_ref:'api-test',site:'US',draft_id:randomUUID(),request_id:randomUUID(),expected_version:0,note:'接口原文'};
  const [first,retry]=await Promise.all([api(admin,'PUT',prefix,write),api(admin,'PUT',prefix,write)]);
  assert.deepEqual(first,retry);assert.equal(first.version,1);
  await api(admin,'PUT',prefix,{...write,note:'冲突调用'},409);
  const updated=await api(admin,'PUT',prefix,{...write,expected_version:1,request_id:randomUUID(),note:'已修改'});
  assert.equal(updated.version,2);
  await api(admin,'PUT',prefix,{...write,action:'archive',expected_version:1,request_id:randomUUID()},409);
  const fork=await api(admin,'PUT',prefix,{...write,expected_version:1,request_id:randomUUID(),note:'较晚的离线版本'});
  assert(fork.branched&&fork.id!==first.id);
  await api(other,'PUT',prefix,write,403);
  assert.deepEqual(exceptions,[]);
  console.log('PASS private triage notes: static reload, browser tabs, unavailable storage, PostgreSQL private device sync, scope, conflict, replay, offline retry, dispatch and archive.');
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
