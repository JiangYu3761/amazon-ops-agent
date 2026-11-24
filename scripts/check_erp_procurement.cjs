// Purchase requisition -> independent approval -> real purchasing and warehouse ledger.
const assert=require('node:assert/strict');
const {randomUUID,randomBytes}=require('node:crypto');
const {chromium}=require('playwright');
const origin=process.env.WORKBENCH_BROWSER_URL;assert.equal(new URL(origin).hostname,'127.0.0.1');
const pr='/api/erp-procurement/requests',rx='/api/erp-receiving';
async function req(ctx,method,path,data,status=200){
  const r=await ctx.request.fetch(origin+path,{method,data,headers:{'X-Workflow-Request':'1'}});const t=await r.text();
  assert.equal(r.status(),status,path+' '+t.slice(0,200));return JSON.parse(t);
}
const desk=p=>p.locator('#erpProcurementDialog'),btn=(p,name)=>desk(p).getByRole('button',{name,exact:true});
const field=(p,name)=>desk(p).locator('[name="'+name+'"]'),form=(p,type)=>desk(p).locator('[data-pr-form="'+type+'"]');
async function open(p){
  await p.goto(origin+'/#inventory');await p.reload();await p.locator('[data-erp-procurement-open]').click();
  await desk(p).getByRole('heading',{name:'采购申请列表'}).waitFor();
}
async function changed(p,text){await p.waitForFunction(text=>document.querySelector('[data-pr-msg]')?.textContent.includes(text),text);}
async function create(p,sku,count,supplier){
  await btn(p,'新建采购申请').click();await field(p,'supplier').fill(supplier);await field(p,'reason').fill('核对交期后采购补货');
  await field(p,'sku').fill(sku);await field(p,'qty').fill(String(count));
}
async function main(){
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try{
    const admin=await browser.newContext(),member=await browser.newContext(),other=await browser.newContext();
    const pw=randomBytes(24).toString('hex');
    await req(admin,'POST','/api/workflow-auth/setup',{email:'lead@pr.example',password:pw,display_name:'采购审批人',setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    const ids=[];
    for(const [ctx,email,display_name] of [[member,'buyer@pr.example','采购员'],[other,'other@pr.example','未授权人员']]){
      const actor=await req(admin,'POST','/api/workflow-auth/members',{email,display_name,password:pw,department:'采购'},201);ids.push(actor.id);
      await req(ctx,'POST','/api/workflow-auth/login',{email,password:pw});
    }
    await req(admin,'POST',rx+'/scopes',{request_id:randomUUID(),warehouse:'默认无关仓',account_ref:'A-account',site:'US',business_timezone:'America/New_York',member_ids:[]},201);
    const scope=await req(admin,'POST',rx+'/scopes',{request_id:randomUUID(),warehouse:'实际采购仓',account_ref:'Z-account',site:'US',business_timezone:'America/New_York',member_ids:[ids[0]]},201);
    // Keep other orders present so the handoff must route by IDs instead of relying on defaults.
    await req(admin,'POST',rx+'/orders',{request_id:randomUUID(),scope_id:scope.id,number:'EXISTING',supplier:'其他供应商',lines:[{sku:'OTHER',qty:1}]},201);
    const m=await member.newPage(),p=await admin.newPage(),a2=await admin.newPage(),x=await other.newPage();const errors=[];
    for(const page of [m,p,a2,x]){page.setDefaultTimeout(8000);page.on('pageerror',e=>errors.push(e.message));}
    await open(x);assert.equal(await btn(x,'新建采购申请').count(),0);
    await open(m);await create(m,'PR-A',5,'真实采购供应商');
    await btn(m,'增加 SKU').click();await field(m,'sku').nth(1).fill('PR-B');await field(m,'qty').nth(1).fill('9');
    await btn(m,'保存申请草稿').click();await changed(m,'草稿已保存');
    const request=(await req(member,'GET',pr+'?scope_id='+scope.id)).items[0];await req(other,'GET',pr+'/'+request.id,undefined,404);
    await btn(m,'提交审批').click();await changed(m,'提交审批已记录');
    assert.equal(await btn(m,'批准申请').count(),0);await desk(m).getByText('申请已提交，等待其他管理员独立审批。').waitFor();
    await open(p);await desk(p).locator('[data-pr-scope]').selectOption(scope.id);await btn(p,request.number).click();
    await btn(p,'退回修订').click();await changed(p,'须说明');
    await field(p,'note').fill('SKU A 本轮只需 4 件');await btn(p,'退回修订').click();await changed(p,'退回已记录');
    await btn(m,'刷新详情').click();await btn(m,'修订申请').click();await field(m,'qty').first().fill('4');await btn(m,'保存修订').click();await changed(m,'修订申请已记录');
    await btn(m,'提交审批').click();await changed(m,'提交审批已记录');
    await btn(p,'刷新详情').click();await field(p,'note').fill('数量已复核');await btn(p,'批准申请').click();await changed(p,'批准已记录');
    await field(p,'order_number').fill('EXISTING');await btn(p,'签发采购单').click();await desk(p).locator('[data-pr-msg][role=alert]').waitFor();
    await open(a2);await desk(a2).locator('[data-pr-scope]').selectOption(scope.id);await btn(a2,request.number).click();
    await Promise.all([field(p,'order_number').fill('ISSUED-PO'),field(a2,'order_number').fill('ISSUED-PO')]);
    await Promise.all([btn(p,'签发采购单').click(),btn(a2,'签发采购单').click()]);
    await changed(p,'已签发');await changed(a2,'已签发');
    const issued=await req(member,'GET',pr+'/'+request.id);assert.equal(issued.status,'issued');
    assert.equal(issued.events.filter(e=>e.action==='issue').length,1);assert.equal(issued.events[0].snapshot.lines[0].qty,5);
    assert.equal((await req(admin,'GET',rx+'/orders?scope_id='+scope.id)).items.filter(b=>b.number==='ISSUED-PO').length,1);
    // Make the issued order an older item in the authorized warehouse as well.
    await req(admin,'POST',rx+'/orders',{request_id:randomUUID(),scope_id:scope.id,number:'NEWEST-UNRELATED',supplier:'另一个供应商',lines:[{sku:'OTHER',qty:1}]},201);
    await btn(p,'打开此采购单办理收货').click();
    const receiving=p.locator('#erpReceivingRoot');await receiving.locator('[data-rx-form=notice]').waitFor();
    assert.equal(await receiving.locator('[data-rx-select=scope]').inputValue(),scope.id);
    assert.equal(await receiving.locator('[data-rx-select=order]').inputValue(),issued.order_id);
    const notice=receiving.locator('[data-rx-form=notice]');
    await notice.getByLabel('PR-A 本次通知数量').fill('4');await notice.getByRole('button',{name:'创建到货通知'}).click();
    const receive=receiving.locator('[data-rx-form=receive]');await receive.getByLabel('PR-A 实收数量').fill('4');await receive.getByRole('button',{name:'确认收货'}).click();
    await receiving.locator('[data-rx-form=quality]').waitFor();
    const actual=await req(admin,'GET',rx+'/orders/'+issued.order_id);
    assert.deepEqual(actual.lines.map(l=>[l.sku,l.qty,l.received]),[['PR-A',4,4],['PR-B',9,0]]);
    // Admin self-review is unavailable even if the actor has access to the scope.
    await open(p);await desk(p).locator('[data-pr-scope]').selectOption(scope.id);await btn(p,'新建采购申请').waitFor();
    await create(p,'SELF',1,'管理员自提');await btn(p,'保存申请草稿').click();await changed(p,'草稿已保存');
    await btn(p,'提交审批').click();await changed(p,'提交审批已记录');assert.equal(await btn(p,'批准申请').count(),0);
    const self=(await req(admin,'GET',pr+'?scope_id='+scope.id)).items.find(r=>r.supplier==='管理员自提');
    await req(admin,'POST',pr+'/'+self.id+'/actions',{request_id:randomUUID(),expected_version:self.version,action:'approve'},403);
    await btn(m,'关闭').click();await m.evaluate(([scope,id])=>window.erpProcurement.openRequest(scope,id),[scope.id,request.id]);
    await btn(m,'打开此采购单办理收货').waitFor();
    await x.evaluate(([scope,id])=>window.erpProcurement.openRequest(scope,id),[scope.id,request.id]);
    await changed(x,'无权查看指定仓库');assert.equal(await btn(x,'打开此采购单办理收货').count(),0);
    assert.deepEqual(errors,[]);
    console.log('PostgreSQL + Chromium procurement passed: scoped two-SKU request, independent review and required rejection reason, amendment and historical versions, duplicate PO conflict, simultaneous issuance -> one real PO, correct warehouse/order handoff and receipt, self-review blocked.');
  }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
