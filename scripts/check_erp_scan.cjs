// Exercise the same form with a keyboard-wedge scan and manual entry against the real local API.
const assert=require('node:assert/strict');
const {randomUUID,randomBytes}=require('node:crypto');
const {chromium}=require('playwright');
const origin=process.env.WORKBENCH_BROWSER_URL;assert.equal(new URL(origin).hostname,'127.0.0.1');
const rx='/api/erp-receiving';
async function api(context,method,path,data,status=200){
  const r=await context.request.fetch(origin+path,{method,data,headers:{'X-Workflow-Request':'1'}});
  const t=await r.text();assert.equal(r.status(),status,path+' '+t.slice(0,220));return JSON.parse(t);
}
async function main(){
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try{
    const admin=await browser.newContext(),operator=await browser.newContext();const password=randomBytes(24).toString('hex');
    await api(admin,'POST','/api/workflow-auth/setup',{email:'admin@scan.example',password,display_name:'管理员',setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    const user=await api(admin,'POST','/api/workflow-auth/members',{email:'scanner@scan.example',password,display_name:'收货员',department:'仓库'},201);
    await api(operator,'POST','/api/workflow-auth/login',{email:'scanner@scan.example',password});
    const warehouse=async (name,ids)=>(await api(admin,'POST',rx+'/scopes',{request_id:randomUUID(),account_ref:'演练店铺',warehouse:name,site:'US',business_timezone:'America/Los_Angeles',member_ids:ids},201)).id;
    const allowed=await warehouse('有权仓',[user.id]),secret=await warehouse('未授权仓',[]);
    const order=async (scope_id,number,supplier)=>api(admin,'POST',rx+'/orders',{scope_id,number,supplier,request_id:randomUUID(),lines:[{sku:'BOX-A',qty:12},{sku:'BOX-B',qty:5},{sku:'BOX-C',qty:1}]},201);
    async function notify(o,qty){
      return (await api(admin,'POST',rx+'/orders/'+o.id+'/notices',{request_id:randomUUID(),lines:[{line_id:o.lines[0].id,qty},{line_id:o.lines[1].id,qty:1}]},201)).notices.at(-1);
    }
    const a=await order(allowed,'PO-ALPHA','供应商甲'),b=await order(allowed,'PO-BETA','供应商乙'),other=await order(secret,'PO-SECRET','保密供应商');
    const a1=await notify(a,2),a2=await notify(a,4),b1=await notify(b,3);await notify(other,4);
    const adminPage=await admin.newPage();adminPage.setDefaultTimeout(9000);
    await adminPage.goto(origin+'/#inventory');await adminPage.locator('[data-wb=receiving]').click();
    const ar=adminPage.locator('#erpReceivingRoot');
    await ar.locator('[data-rx-select=scope]:enabled').selectOption(allowed);
    await ar.locator('[data-tab=scan]').click();await ar.getByText('维护本仓 FNSKU 对照',{exact:true}).click();
    const lookup=ar.locator('[data-rx-form=barcode-lookup]'),edit=ar.locator('[data-rx-form=barcode-save]');
    await lookup.getByLabel('FNSKU',{exact:true}).fill('X0-BOX-A');await lookup.getByRole('button',{name:'查找对照'}).click();
    await edit.getByLabel('本仓采购 SKU').fill('NOT-ON-ANY-ORDER');await edit.getByRole('button',{name:'建立对照'}).click();
    await ar.locator('[data-rx-feedback][role=alert]').waitFor();await edit.getByLabel('本仓采购 SKU').fill('BOX-B');
    await edit.getByRole('button',{name:'建立对照'}).click();await ar.getByText('对照已保存 · X0-BOX-A → BOX-B').waitFor();
    await lookup.getByRole('button',{name:'查找对照'}).click();await ar.getByText('已读取本仓现有对照。').waitFor();
    // A second administrator changed the mapping after the visible lookup.
    await api(admin,'POST',rx+'/barcodes',{scope_id:allowed,fnsku:'X0-BOX-A',sku:'BOX-C',expected_sku:'BOX-B'},201);
    await edit.getByLabel('本仓采购 SKU').fill('BOX-A');await edit.getByRole('button',{name:'保存改配'}).click();
    await ar.getByText(/请重新查找并核对当前映射/).waitFor();
    await lookup.getByRole('button',{name:'查找对照'}).click();await ar.getByText('当前对照 · X0-BOX-A → BOX-C').waitFor();
    await edit.getByLabel('本仓采购 SKU').fill('BOX-A');await edit.getByRole('button',{name:'保存改配'}).click();
    await ar.getByText('对照已保存 · X0-BOX-A → BOX-A').waitFor();
    await api(operator,'POST',rx+'/barcodes',{scope_id:allowed,fnsku:'X0-BOX-A',sku:'BOX-B',expected_sku:'BOX-A'},403);
    await api(operator,'GET',rx+'/scan?scope_id='+secret+'&code=BOX-A',undefined,404);
    const p=await operator.newPage();p.setDefaultTimeout(9000);const errors=[];
    p.on('pageerror',err=>errors.push(err.message));
    await p.goto(origin+'/#inventory');await p.locator('[data-wb=receiving]').click();
    const root=p.locator('#erpReceivingRoot'),scan=root.locator('[data-rx-form=scan]'),code=scan.locator('[name=code]');
    const mode=()=>root.locator('[data-tab=scan]').click();
    const manual=async text=>{await code.fill(text);await scan.getByRole('button',{name:'查询待收通知'}).click();};
    const keyboard=async text=>{await code.focus();await code.press('Control+a');await code.pressSequentially(text);await code.press('Enter');};
    await mode();assert.equal(await code.evaluate(el=>document.activeElement===el),true);
    assert.equal(await root.getByText('维护本仓 FNSKU 对照',{exact:true}).count(),0);
    assert.equal(await root.locator('[data-rx-select=scope] option').count(),1);
    await manual('box-a');await root.getByText('当前仓库没有对应的待收通知。').waitFor();
    await manual('PO-ALPHA');await root.getByRole('button',{name:'选择通知 '+a2.number}).waitFor();
    assert.equal(await root.locator('[data-rx=scan-pick]').count(),2);
    assert.equal(await root.locator('[data-rx-form=receive]').count(),0);
    await root.getByRole('button',{name:'选择通知 '+a2.number}).click();
    const selected=root.locator('[data-rx-select=notice]');await selected.waitFor();assert.equal(await selected.inputValue(),a2.id);
    assert.equal(await root.locator('[data-rx-scanned-line]').count(),0);
    await mode();await keyboard(a1.number);await selected.waitFor();assert.equal(await selected.inputValue(),a1.id);
    assert.equal((await api(operator,'GET',rx+'/orders/'+a.id)).ledger.length,0);
    await mode();await keyboard('BOX-A');await root.getByRole('button',{name:'选择通知 '+b1.number}).waitFor();
    assert.equal(await root.locator('[data-rx=scan-pick]').count(),3);assert.equal(await root.getByText('保密供应商').count(),0);
    await root.getByRole('button',{name:'选择通知 '+b1.number}).click();await selected.waitFor();assert.equal(await selected.inputValue(),b1.id);
    const line=root.locator('[data-rx-scanned-line]');await line.waitFor();assert.equal(await line.count(),1);assert.equal(await line.getAttribute('data-line'),b.lines[0].id);
    assert.equal(await line.locator('[name=qty]').inputValue(),'0');await line.locator('[name=qty]').fill('2');
    await root.locator('[data-rx-form=receive]').getByRole('button',{name:'确认收货'}).click();
    await root.locator('[data-rx-form=quality]').waitFor();
    const after=await api(operator,'GET',rx+'/orders/'+b.id);assert.equal(after.lines[0].received,2);assert.equal(after.notices[0].lines[0].pending,2);
    await mode();await manual('X0-BOX-A');await root.getByRole('button',{name:'选择通知 '+a1.number}).waitFor();assert.equal(await root.locator('[data-rx=scan-pick]').count(),2);
    await root.getByRole('button',{name:'选择通知 '+a1.number}).click();await line.waitFor();assert.equal(await line.getAttribute('data-line'),a.lines[0].id);
    await mode();await manual(b1.number);await root.getByText('当前仓库没有对应的待收通知。').waitFor();
    await manual('BOX-A');await root.getByRole('button',{name:'选择通知 '+a2.number}).waitFor();
    await api(admin,'POST',rx+'/notices/'+a2.id+'/cancel',{request_id:randomUUID(),reason:'该通知已在另一工位撤销'});
    await root.getByRole('button',{name:'选择通知 '+a2.number}).click();
    await root.getByText('该通知已处理，请重新扫描并核对待收状态。').waitFor();
    assert.equal(await root.locator('[data-rx-form=receive]').count(),0);
    assert.deepEqual(errors,[]);console.log('Real PostgreSQL + Chromium scan passed: Enter/manual, selected warehouse only, exact-case empty state, PO and notice lookup, three-candidate explicit selection, FNSKU resolution, matched SKU highlight, no implicit posting, confirmed short receipt and exclusion of received notices.');
  }finally{await browser.close();}
}
main().catch(err=>{console.error(err);process.exitCode=1;});
