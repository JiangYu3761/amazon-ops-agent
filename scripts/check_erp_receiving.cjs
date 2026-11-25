// Run with WORKBENCH_BROWSER_SCRIPT=scripts/check_erp_receiving.cjs via check_workbench.py.
const assert = require('node:assert/strict');
const { randomBytes } = require('node:crypto');
const { chromium } = require('playwright');

const origin=process.env.WORKBENCH_BROWSER_URL;
assert.equal(new URL(origin).hostname,'127.0.0.1');
const apiRoot='/api/erp-receiving';
const passwd=randomBytes(24).toString('hex');
async function api(ctx,method,path,data,status=200){
  const r=await ctx.request.fetch(origin+path,{method,data,headers:{'X-Workflow-Request':'1'}});
  const text=await r.text();assert.equal(r.status(),status,path+': '+text.slice(0,240));return JSON.parse(text);
}
const box=p=>p.locator('#erpReceivingRoot');
const form=(p,id)=>box(p).locator('[data-rx-form="'+id+'"]');
const input=(p,name)=>p.locator('[name="'+name+'"]');
const choose=(p,id)=>box(p).locator('[data-rx-select="'+id+'"]');
const submit=f=>f.locator('button[type=submit]').click();
const tab=(p,id)=>box(p).locator('[data-tab="'+id+'"]').click();
async function row(f,sku,values){
  const line=f.locator('tr[data-line]').filter({has:f.page().getByRole('cell',{name:sku,exact:true})});
  for(const [name,value] of Object.entries(values))await input(line,name).fill(String(value));
}
async function start(p){
  await p.goto(origin+'/#inventory');
  await p.locator('button[data-wb=receiving]').click();
  await box(p).getByRole('heading',{name:'仓库收货'}).waitFor();
  await box(p).locator('[data-rx=refresh]:enabled').waitFor();
}
async function feedback(p,part){
  await p.waitForFunction(part=>document.querySelector('#erpReceivingRoot [data-rx-feedback]')?.textContent.includes(part),part);
}
async function main(){
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try{
    const admin=await browser.newContext({viewport:{width:1440,height:1000}});
    const member=await browser.newContext();const other=await browser.newContext();
    await api(admin,'POST','/api/workflow-auth/setup',{email:'lead@warehouse.example',display_name:'仓库负责人',password:passwd,setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    for(const [ctx,email,display_name] of [[member,'receiver@warehouse.example','收货员'],[other,'sales@warehouse.example','未授权']]){
      await api(admin,'POST','/api/workflow-auth/members',{email,display_name,department:'仓储验收',password:passwd},201);
      await api(ctx,'POST','/api/workflow-auth/login',{email,password:passwd});
    }
    const p=await admin.newPage(),m=await member.newPage(),x=await other.newPage();
    const errors=[];for(const page of [p,m,x]){page.setDefaultTimeout(8000);page.on('pageerror',err=>errors.push(err.message));}
    await start(p);
    const scope=form(p,'scope');
    for(const [key,value] of Object.entries({account_ref:'验收美国店铺',site:'US',business_timezone:'America/Los_Angeles',warehouse:'西仓'}))await input(scope,key).fill(value);
    await scope.getByLabel('收货员',{exact:false}).check();await submit(scope);await feedback(p,'授权已保存');
    await tab(p,'purchase');await box(p).getByRole('button',{name:'新建采购单'}).click();
    const order=form(p,'order');await input(order,'number').fill('RX-QA-001');await input(order,'supplier').fill('示例供应商');
    await input(order,'sku').fill('CAM-A');await input(order,'qty').fill('10');
    await input(order,'contract_currency').fill('USD');await input(order,'net_unit_price').fill('7.11');await input(order,'unit_tax').fill('0.17');
    await order.getByRole('button',{name:'增加 SKU'}).click();
    await input(order,'sku').nth(1).fill('CAM-B');await input(order,'qty').nth(1).fill('6');
    await submit(order);await feedback(p,'采购单已建立');
    // An identical warehouse document number is a visible business conflict.
    await box(p).getByRole('button',{name:'新建采购单'}).click();
    await input(order,'number').fill('RX-QA-001');await input(order,'supplier').fill('示例供应商');
    await input(order,'sku').fill('CAM-X');await input(order,'qty').fill('1');
    await submit(order);await feedback(p,'已有该采购单号');await box(p).getByRole('button',{name:'返回单据'}).click();
    await start(x);await box(x).getByText('尚未获得仓库权限，请联系流程管理员。').waitFor();
    assert.equal(await box(x).getByRole('button',{name:'新建采购单'}).count(),0);
    await start(m);assert.equal(await box(m).getByRole('button',{name:'新建采购单'}).count(),0);
    const n=form(m,'notice');await row(n,'CAM-A',{qty:8});await row(n,'CAM-B',{qty:4});
    // Let the server commit the notice, then simulate loss of the response. A retry must reuse its UUID.
    let committedId, retriedId;
    await m.route('**/api/erp-receiving/orders/*/notices',async route=>{
      const payload=route.request().postDataJSON();
      if(!committedId){
        committedId=payload.request_id;const upstream=await route.fetch();assert.equal(upstream.status(),201);await route.abort('failed');
      }else{retriedId=payload.request_id;await route.continue();}
    });
    await submit(n);await feedback(m,'尚未确认提交结果');await row(n,'CAM-A',{qty:7});await submit(n);await feedback(m,'上次提交的结果尚未确认');
    await box(m).getByRole('button',{name:'核对上次提交'}).click();await feedback(m,'通知已建立');
    assert.equal(retriedId,committedId);await m.unroute('**/api/erp-receiving/orders/*/notices');
    const currentOrder=(await api(admin,'GET',apiRoot+'/orders')).items[0].id;
    assert.equal((await api(admin,'GET',apiRoot+'/orders/'+currentOrder)).notices.length,1);
    await api(other,'GET',apiRoot+'/orders/'+currentOrder,undefined,404);
    const receive=form(m,'receive');await row(receive,'CAM-A',{qty:6});await row(receive,'CAM-B',{qty:4});await submit(receive);await feedback(m,'收货已入账');
    const q=form(m,'quality');await row(q,'CAM-A',{good:3,bad:2});await submit(q);await feedback(m,'质检处理已入账');
    await choose(m,'quality-mode').selectOption('waive');await row(q,'CAM-A',{good:1});await row(q,'CAM-B',{good:4});await submit(q);await feedback(m,'质检处理已入账');
    assert.equal(await box(m).getByRole('button',{name:'冲销',exact:true}).count(),0);
    await tab(m,'ledger');
    const net=box(m).locator('section').filter({has:m.getByRole('heading',{name:'本采购单库存变动净额'})});
    assert.deepEqual(await net.locator('tr').filter({hasText:'CAM-A'}).locator('td').allTextContents(),['CAM-A','0','4','2']);
    await tab(m,'purchase');await n.getByRole('cell',{name:'43.68 USD',exact:true}).waitFor();
    await row(n,'CAM-A',{qty:4});await submit(n);await feedback(m,'通知已建立');
    await choose(m,'receive-mode').selectOption('quick');await row(receive,'CAM-A',{qty:4,good:4,bad:1});await submit(receive);await feedback(m,'必须等于');
    await row(receive,'CAM-A',{good:3,bad:1});await submit(receive);await feedback(m,'收货已入账');
    const actual=await api(admin,'GET',apiRoot+'/orders/'+currentOrder);
    assert.deepEqual(actual.lines.map(l=>[l.sku,l.qty,l.received,l.notified,l.unnotified]),[['CAM-A',10,10,0,0],['CAM-B',6,4,0,2]]);
    await box(p).getByRole('button',{name:'刷新单据'}).click();await tab(p,'receiving');
    await choose(p,'notice').selectOption(actual.notices[0].id);
    await box(p).getByRole('row').filter({hasText:'普通收货'}).getByRole('button',{name:'冲销'}).click();
    await input(form(p,'reverse'),'reason').fill('复核修正');await submit(form(p,'reverse'));await feedback(p,'先冲销');await box(p).locator('[data-rx=close-reverse]').click();
    await box(p).getByRole('row').filter({hasText:'免检'}).getByRole('button',{name:'冲销'}).click();
    await input(form(p,'reverse'),'reason').fill('重新核验免检依据');await submit(form(p,'reverse'));await feedback(p,'已登记冲销流水');
    await tab(p,'ledger');const ledger=box(p).locator('section').filter({has:p.getByRole('heading',{name:'本采购单库存变动净额'})});
    assert.deepEqual(await ledger.locator('tr').filter({hasText:'CAM-A'}).locator('td').allTextContents(),['CAM-A','1','6','3']);
    assert.deepEqual(await ledger.locator('tr').filter({hasText:'CAM-B'}).locator('td').allTextContents(),['CAM-B','4','0','0']);
    await p.reload();await p.locator('[data-wb=receiving]').click();await tab(p,'ledger');
    assert.deepEqual(await ledger.locator('tr').filter({hasText:'CAM-A'}).locator('td').allTextContents(),['CAM-A','1','6','3']);
    await tab(p,'purchase');await row(form(p,'notice'),'CAM-B',{qty:2});await submit(form(p,'notice'));await feedback(p,'通知已建立');
    const cancel=form(p,'cancel');await box(p).getByText('填写取消原因',{exact:true}).click();await input(cancel,'reason').fill('供应商取消本次送货');await submit(cancel);await feedback(p,'通知已取消');
    const final=await api(admin,'GET',apiRoot+'/orders/'+currentOrder);
    assert.equal(final.notices.at(-1).status,'cancelled');assert.equal(final.lines[1].unnotified,2);
    await p.evaluate(([scope,order,notice])=>window.workbenchUI.openReceivingOrder(scope,order,notice),[final.scope_id,final.id,final.notices[1].id]);
    await choose(p,'notice').waitFor();assert.equal(await choose(p,'notice').inputValue(),final.notices[1].id);
    await p.evaluate(([scope,order])=>window.workbenchUI.openReceivingOrder(scope,order,'missing-notice'),[final.scope_id,final.id]);
    await feedback(p,'目标通知不属于当前采购单');assert.equal(await choose(p,'notice').count(),0);
    assert.deepEqual(errors,[]);console.log('PostgreSQL + Chromium receiving passed: warehouse ACL, multi-SKU contract terms and received tax-inclusive cost, duplicate order, dropped response and idempotent retry, short receipt, normal/quick allocation, inspection/waiver, reverse constraint, ledger after reload.');
  }finally{await browser.close();}
}
main().catch(err=>{console.error(err);process.exitCode=1;});
