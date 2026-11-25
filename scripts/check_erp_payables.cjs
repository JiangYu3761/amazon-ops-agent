// Real Chromium + disposable PostgreSQL/API. Run through scripts/check_workbench.py.
const assert=require('node:assert/strict');
const {randomBytes,randomUUID}=require('node:crypto');
const {chromium}=require('playwright');
const site=process.env.WORKBENCH_BROWSER_URL;
assert.equal(new URL(site).hostname,'127.0.0.1');
const rx='/api/erp-receiving', base='/api/erp-payables';
async function request(ctx,method,path,data,status=200){
  const r=await ctx.request.fetch(site+path,{method,data,headers:{'X-Workflow-Request':'1'}});
  const text=await r.text();assert.equal(r.status(),status,path+' '+text.slice(0,300));return JSON.parse(text);
}
const root=p=>p.locator('#erpPayablesDialog');
const form=(p,name)=>root(p).locator('[data-ap-form="'+name+'"]');
const btn=(p,name)=>root(p).getByRole('button',{name,exact:true});
const field=(f,name)=>f.locator('[name="'+name+'"]');
const submit=f=>f.locator('button[type="submit"]').click();
async function msg(p,text){await p.waitForFunction(w=>document.querySelector('#erpPayablesDialog [data-ap-message]')?.textContent.includes(w),text);}
async function open(p){
  await p.goto(site+'/#finance');await p.reload();await p.locator('[data-erp-payables-open]').click();
  await root(p).getByRole('heading',{name:'供应商账单',exact:true}).waitFor();
}
async function fill(f,values){for(const [key,value] of Object.entries(values))await field(f,key).fill(value);}
async function payment(p,ref,amount){await fill(form(p,'payment'),{external_ref:ref,amount,paid_on:'2026-01-10',note:'凭证已核对'});}
async function main(){
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
  try{
    const admin=await browser.newContext(),member=await browser.newContext(),other=await browser.newContext();
    const password=randomBytes(24).toString('hex');
    await request(admin,'POST','/api/workflow-auth/setup',{email:'finance-admin@ap.example',password,display_name:'财务管理员',setup_code:process.env.WORKFLOW_SETUP_TOKEN});
    const people=[];
    for(const [ctx,email,display_name] of [[member,'cashier@ap.example','经办人'],[other,'outsider@ap.example','其他仓库成员']]){
      const u=await request(admin,'POST','/api/workflow-auth/members',{email,display_name,password,department:'财务'},201);
      await request(ctx,'POST','/api/workflow-auth/login',{email,password});people.push(u.id);
    }
    const a=await request(admin,'POST',rx+'/scopes',{request_id:randomUUID(),account_ref:'应付测试美国店铺',site:'US',business_timezone:'America/New_York',warehouse:'东仓',member_ids:[people[0]]},201);
    const o=await request(admin,'POST',rx+'/orders',{request_id:randomUUID(),scope_id:a.id,number:'AP-PO-1',supplier:'测试供应商',lines:[{sku:'AP-A',qty:5},{sku:'AP-B',qty:12}]},201);
    const n=await request(admin,'POST',rx+'/orders/'+o.id+'/notices',{request_id:randomUUID(),lines:o.lines.map(x=>({line_id:x.id,qty:x.qty}))},201);
    await request(member,'POST',rx+'/notices/'+n.notices[0].id+'/receive',{request_id:randomUUID(),mode:'quick',lines:o.lines.map(x=>({line_id:x.id,qty:x.qty,good:x.qty,bad:0}))});
    const p=await admin.newPage(),m=await member.newPage(),x=await other.newPage();const errors=[];
    for(const pg of [p,m,x]){pg.setDefaultTimeout(9500);pg.on('pageerror',e=>errors.push(e.message));}
    await open(x);assert.equal(await btn(x,'登记应付账单').count(),0);
    await open(p);await btn(p,'登记应付账单').click();
    const bill=form(p,'bill');await bill.getByRole('checkbox',{name:/AP-A/}).check();
    await bill.locator('[data-ap-mode]').getByText(/凭证人工登记/).waitFor();
    await fill(bill,{document_ref:'INV-001',currency:'USD',amount:'100.25'});await submit(bill);await msg(p,'已登记');
    const billList=await request(admin,'GET',base+'/bills?scope_id='+a.id);const id=billList.items[0].id;
    await request(other,'GET',base+'/bills/'+id,undefined,404);
    await payment(p,'WIRE-001','25.25');await submit(form(p,'payment'));await msg(p,'付款登记已确认');
    await payment(p,'WIRE-001','25.25');await submit(form(p,'payment'));await msg(p,'余额未重复扣减');
    await payment(p,'WIRE-001','25.26');await submit(form(p,'payment'));
    await root(p).locator('[data-ap-message][role=alert]').waitFor();
    let d=await request(member,'GET',base+'/bills/'+id);assert.equal(Number(d.balance),75);assert.equal(d.payments.length,1);
    await open(m);await btn(m,'INV-001').click();await form(m,'payment').waitFor();
    await Promise.all([payment(p,'WIRE-002','50.00'),payment(m,'WIRE-003','50.00')]);
    const endpoints=pg=>pg.waitForResponse(r=>new URL(r.url()).pathname===base+'/bills/'+id+'/payments'&&r.request().method()==='POST');
    const responses=await Promise.all([endpoints(p),endpoints(m),submit(form(p,'payment')),submit(form(m,'payment'))]);
    assert.deepEqual(responses.slice(0,2).map(r=>r.status()).sort(),[201,409]);
    const loser=responses[0].status()===409?p:m;
    await root(loser).locator('[data-ap-message][role=alert]').waitFor();
    await btn(loser,'刷新').click();await form(loser,'payment').waitFor();
    await payment(loser,'WIRE-FINAL','25');await submit(form(loser,'payment'));await msg(loser,'付款登记已确认');
    d=await request(member,'GET',base+'/bills/'+id);assert.equal(d.status,'paid');assert.equal(Number(d.balance),0);assert.equal(d.payments.length,3);
    assert.equal(await form(loser,'payment').count(),0);
    await open(p);await btn(p,'INV-001').click();await root(p).getByText('已付清',{exact:true}).waitFor();
    await btn(p,'返回台账').click();await btn(p,'登记应付账单').click();
    assert.equal(await bill.getByRole('checkbox',{name:/AP-A/}).count(),0);await bill.getByRole('checkbox',{name:/AP-B/}).check();
    await fill(bill,{document_ref:'INV-VOID',currency:'JPY',amount:'2.55'});await submit(bill);
    await root(p).locator('[data-ap-message][role=alert]').waitFor();
    await fill(bill,{currency:'KWD',amount:'2.555'});await submit(bill);await msg(p,'已登记');
    await root(p).getByText('作废未付款账单',{exact:true}).click();await field(form(p,'void'),'reason').fill('供应商更换纸质凭证');await submit(form(p,'void'));await msg(p,'账单已作废');
    await btn(p,'返回台账').click();await btn(p,'登记应付账单').click();await bill.getByRole('checkbox',{name:/AP-B/}).waitFor();
    await btn(p,'关闭').click();await p.evaluate(([scope,id])=>window.erpPayables.openBill(scope,id),[a.id,id]);
    await root(p).getByText('已付清',{exact:true}).waitFor();
    await x.evaluate(([scope,id])=>window.erpPayables.openBill(scope,id),[a.id,id]);await msg(x,'无权查看指定仓库');
    assert.equal(await root(x).getByText('INV-001',{exact:true}).count(),0);
    // A priced contract is settled per receipt event using actual invoice values.
    const priced=await request(admin,'POST',rx+'/orders',{request_id:randomUUID(),scope_id:a.id,number:'AP-PO-PRICED',
      supplier:'测试供应商',lines:[{sku:'AP-PRICE',qty:7,net_unit_price:'5.27',unit_tax:'0.13',contract_currency:'USD'}]},201);
    const lineId=priced.lines[0].id;
    async function receiveBatch(qty){
      const order=await request(member,'POST',rx+'/orders/'+priced.id+'/notices',{request_id:randomUUID(),lines:[{line_id:lineId,qty}]},201);
      const notice=order.notices.find(n=>n.status==='waiting');
      await request(member,'POST',rx+'/notices/'+notice.id+'/receive',{mode:'normal',request_id:randomUUID(),lines:[{line_id:lineId,qty}]});
      return notice;
    }
    const first=await receiveBatch(3);const second=await receiveBatch(4);
    await open(m);await btn(m,'登记应付账单').click();
    const f=form(m,'bill'),selectReceipt=(sku,notice)=>f.getByRole('checkbox',{name:sku+' · '+notice.number,exact:true});
    await selectReceipt('AP-PRICE',first).check();
    const legacy=f.getByRole('checkbox',{name:/AP-B/});await legacy.check();
    assert.match(await f.locator('[data-ap-mode]').textContent(),/分别登记/);assert.equal(await f.locator('[data-ap=preview]').isDisabled(),true);
    await legacy.uncheck();
    await fill(f,{document_ref:'INV-MATCHED',currency:'USD',amount:'16.20'});
    const invoice=f.locator('[data-ap-invoice]:visible');
    await fill(invoice,{invoice_unit:'箱',invoice_qty:'4',invoice_net_unit_price:'5.27',invoice_tax_amount:'0.39',invoice_amount:'16.20'});
    const callPreview=async()=>{const result=m.waitForResponse(r=>new URL(r.url()).pathname===base+'/match-preview');await f.locator('[data-ap=preview]').click();return (await result).json();};
    const differing=await callPreview();assert.equal(differing.status,'difference');await f.getByRole('heading',{name:'三方匹配有差异'}).waitFor();
    assert.equal(await f.locator('[type=submit]').isDisabled(),true);
    const available=(await request(admin,'GET',base+'/sources?scope_id='+a.id)).items.find(r=>r.notice_id===first.id);
    assert.equal(available.expected_amount,'16.20');assert.equal(available.received_qty,3);
    const wrong={scope_id:a.id,currency:'USD',amount:'16.20',sources:[{event_id:available.event_id,line_id:lineId,invoice:{
      unit:'箱',qty:4,net_unit_price:'5.27',tax_amount:'0.39',amount:'16.20'}}]};
    await request(other,'POST',base+'/match-preview',wrong,404);
    await request(member,'POST',base+'/bills',{...wrong,document_ref:'INV-MATCHED',request_id:randomUUID()},422);
    await fill(invoice,{invoice_unit:'件',invoice_qty:'3'});
    assert.equal((await callPreview()).status,'matched');await msg(m,'发票匹配通过');
    // Changing just one cent invalidates the prior preview and blocks submission.
    await field(f,'amount').fill('16.21');assert.equal(await f.locator('[type=submit]').isDisabled(),true);
    assert.equal((await callPreview()).status,'difference');await field(f,'amount').fill('16.20');
    assert.equal((await callPreview()).status,'matched');await msg(m,'发票匹配通过');await submit(f);await msg(m,'已登记');
    await root(m).getByRole('heading',{name:'采购、实收与发票匹配明细'}).waitFor();
    const matchedId=(await request(admin,'GET',base+'/bills?scope_id='+a.id)).items.find(b=>b.document_ref==='INV-MATCHED').id;
    const matched=await request(admin,'GET',base+'/bills/'+matchedId);
    assert.equal(matched.amount_source,'po_receipt_invoice');assert.equal(matched.sources[0].invoice.unit,'件');
    assert.equal(matched.sources[0].expected_tax,'0.39');assert.equal(matched.sources[0].notice_id,first.id);
    await request(other,'GET',base+'/bills/'+matchedId,undefined,404);
    await open(p);await btn(p,'INV-MATCHED').click();await root(p).getByRole('heading',{name:'采购、实收与发票匹配明细'}).waitFor();
    await btn(p,'返回台账').click();await btn(p,'登记应付账单').click();
    assert.equal(await form(p,'bill').getByRole('checkbox',{name:'AP-PRICE · '+first.number,exact:true}).count(),0);
    await form(p,'bill').getByRole('checkbox',{name:'AP-PRICE · '+second.number,exact:true}).waitFor();
    assert.deepEqual(errors,[]);
    console.log('PostgreSQL + Chromium payables passed: warehouse ACL, legacy manual bill, partial priced receipt, invoice lines and one-cent mismatch, stale preview blocking, persisted three-way match and remaining unbilled quantity, two-actor concurrent overpayment prevention, external transfer replay and conflict, installments, paid balance on reload, JPY and KWD precision, void release.');
  }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
