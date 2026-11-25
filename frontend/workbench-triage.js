/* Route an operator's notes locally. Each proposal quotes the source and opens a draft only. */
(function (root) {
  'use strict';
  const topics = [
    ['compliance','产品与合规',['证书','认证','危险品','合规','标签审核','商标侵权','FCC']],
    ['service','订单与售后',['退货','买家消息','退款','售后','差评原因','换货']],
    ['inventory','备货与库存',['断货','缺货','库存','补货','采购','入仓','在途','货件','调拨']],
    ['ads','广告运营',['广告','竞价','否词','搜索词报告','ACOS','ROAS','CPC','SP花费']],
    ['finance','成本与利润',['成本','头程费用','对账','结算','利润','回款','税费']],
    ['listing','Listing 与素材',['主图','详情页','五点','A+','文案','Listing','变体','页面抑制']],
    ['shop','店铺与账户',['绩效通知','账户健康','平台通知','账号停用','申诉','Case编号']],
    ['calendar','活动与排期',['促销','秒杀','申报期','活动排期','活动上线','优惠券','Prime Day']],
    ['ranking','排名监控',['自然排名','自然位','广告位排名','类目排名','BSR','排名监控']],
    ['imports','数据导入中心',['补数据','补数','缺数','报表缺失','未映射','导入失败','CSV']],
    ['settings','功能覆盖与系统设置',['授权过期','连接失效','时区配置','同步配置']],
    ['deliverables','产品交付清单',['缺件','交付文件','素材版本','交付目录']],
    ['process','流程协作',['交接','前置依赖','上游阻塞']], ['workflow','全流程 SOP',['SOP','适用性','重新打开流程']],
    ['team','团队与审批',['待审批','等我审批','无人负责','指派负责人']],
    ['overview','产品总览',['季度目标','下架退市','新品上市计划','产品机会']],
    ['agent','Agent 决策中心',['每周复盘','复核结论','决策复盘']]
  ];
  const dateWord='(?:今天|今日|明天|明日|后天|(?:本周|下周|周|星期)[一二三四五六日天]|\\d{1,2}月\\d{1,2}(?:日|号)?|\\d{4}[-/]\\d{1,2}[-/]\\d{1,2})';
  const clockWord='(?:\\s*(?:上午|下午|晚上|中午|下班|收工)?\\s*(?:\\d{1,2}[:：]\\d{2}|\\d{1,2}点(?:半)?)?)';
  const priorityRules=[
    ['明确紧急',/紧急(?!联系人|联络人|预案|演练|通道)|加急|立即(?:处理|复核|确认|跟进|停用)|必须马上|重点跟进/gu],
    ['明确阻塞',/阻塞|受阻|阻断|卡住|卡在|无法(?:发货|出库|入库|上线|提交|放行|结算)|不能发货/gu],
    ['明确截止',new RegExp('(?:'+dateWord+clockWord+'(?:之前|前|截止|到期)|(?:截止(?:日期|时间)?(?:为|是|到|于)?|最[迟晚]|必须[在于])\\s*'+dateWord+clockWord+')','gu')],
  ];
  function priorityClues(quote,candidates){
    const buckets=new Map(candidates.map(c=>[c.page,[]]));
    let owners=candidates.length===1?candidates:[];
    for(const part of quote.split(/[，,]|但是|不过|然而|但/u)){
      const normalized=part.normalize('NFKC').toLowerCase();
      const named=candidates.filter(c=>c.terms.some(term=>normalized.includes(term.normalize('NFKC').toLowerCase())));
      if(named.length)owners=named;
      if(!owners.length)continue;
      const closed=/(?:已经|已)(?:经)?(?:解决|解除|恢复|排除|办结|关闭|完成|处理完)|(?:无需|不用|不再)(?:继续)?跟进|取消(?:了)?截止/u.test(part);
      if(closed){for(const c of owners)buckets.set(c.page,[]);continue;}
      if(/(?:如果|假如|万一|是否|可否|要不要|可能|预计|视情况|假设)|[吗么]\s*$|(?:待确认|尚未明确|还未确定).*(?:时间|日期|截止)/u.test(part))continue;
      for(const [kind,pattern] of priorityRules){
        for(const hit of part.matchAll(pattern)){
          const before=part.slice(0,hit.index);
          if(/(?:不需要|不要求|没有|并非|并未|未发现|暂无|尚无|无需|无须|不用|不必|不是|并不|取消).{0,6}$|(?:无|非|不|未)(?:再|算|是|必|算作|属于)?\s*$/u.test(before))continue;
          for(const c of owners){
            const clues=buckets.get(c.page);
            if(!clues.some(item=>item.kind===kind&&item.quote===hit[0]))clues.push({kind,quote:hit[0]});
          }
        }
      }
    }
    return buckets;
  }
  function classify(text) {
    const rows = [];
    const seen = new Set();
    for (const part of text.split(/[。！？；;\n]+/u)) {
      const quote = part.trim();
      if (!quote) continue;
      const normalized = quote.normalize('NFKC').toLowerCase();
      const matched = topics.map(([page,title,terms]) => ({
        page,title,terms:terms.filter(term=>normalized.includes(term.normalize('NFKC').toLowerCase()))
      })).filter(x=>x.terms.length);
      const candidates = matched.length ? matched : [{page:null,title:'待人工选择',terms:[]}];
      const clues=priorityClues(quote,candidates);
      for (const candidate of candidates) {
        const key = JSON.stringify([quote,candidate.page]);
        if (!seen.has(key)) {
          seen.add(key);
          rows.push({...candidate, quote,priorityEvidence:clues.get(candidate.page)});
        }
      }
    }
    return rows;
  }
  if (typeof module !== 'undefined' && module.exports) {module.exports = {classify}; return;}
  let catalog;
  function elem(tag, text, attrs={}) {
    const el = document.createElement(tag);
    if (text !== null) el.textContent=text;
    Object.assign(el,attrs);
    return el;
  }
  async function load() {
    if (!catalog) catalog = Promise.all(['growth','operations','workspace'].map(async name=>{
      const r=await fetch('workbench-'+name+'.json');
      if(!r.ok) throw Error('业务目录加载失败');
      return r.json();
    })).then(groups=>groups.flat()).catch(error=>{catalog=null;throw error;});
    return catalog;
  }
  const endpoint='/api/workbench/triage-drafts';
  const base='homa.triage.notes.1/';
  const sessions=new Map();
  let visible=null;
  function keyFor(parts){return base+encodeURIComponent(JSON.stringify(parts))+'/';}
  function emptyDraft(){
    return {id:uuid(),slot:uuid(),version:0,note:'',saved:'',updated:0,dirty:false,pending:null,archived:false,assessments:{},revision:uuid()};
  }
  function documents(s){
    const found=new Map(), remoteIds=new Set(s.remote.map(row=>row.id));
    const archived=new Map();
    for(const local of s.locals.values())if(local.archived)archived.set(local.id,local.note);
    for(const row of [...s.remote,...s.locals.values()]){
      if(!row.note||s.retired.has(row.revision))continue;
      if(row.slot && s.mode==='server' && !row.dirty && remoteIds.has(row.id))continue;
      const doc=archived.get(row.id)===row.note?{...row,archived:true}:row;
      const key=JSON.stringify([doc.id,doc.note,doc.archived]);
      if(!found.has(key)||found.get(key).updated<doc.updated)found.set(key,doc);
    }
    return [...found.values()].sort((a,b)=>Number(a.archived)-Number(b.archived)||b.updated-a.updated);
  }
  function readLocal(s){
    try{
      const local=new Map(),retired=new Set();
      const shops=new Map(s.shops);
      for(let i=0;i<localStorage.length;i++){
        const key=localStorage.key(i);
        if(!key?.startsWith(base))continue;
        let parts;
        try{parts=JSON.parse(decodeURIComponent(key.slice(base.length).split('/')[0]));}catch{continue;}
        if(parts[0]===s.mode&&parts[1]===s.actor&&parts[2]===s.product&&parts[3])shops.set(JSON.stringify(parts.slice(3)),parts.slice(3));
        if(!key.startsWith(s.prefix))continue;
        if(key.startsWith(s.prefix+'recovered/')){retired.add(key.slice((s.prefix+'recovered/').length));continue;}
        const row=JSON.parse(localStorage.getItem(key));
        if(row && typeof row.note==='string' && row.id && row.slot)local.set(row.slot,row);
      }
      s.locals=local;s.retired=retired;s.shops=shops;s.storageIssue='';
    }catch{s.storageIssue='本机草稿读取失败，请检查浏览器存储权限。';}
  }
  function persist(s,tx){
    const copy={id:tx.id,slot:tx.slot,version:tx.version,note:tx.note,saved:tx.saved,updated:tx.updated,
      dirty:tx.dirty,pending:tx.pending,archived:tx.archived,revision:tx.revision,replaces:tx.replaces||null,assessments:tx.assessments||{},reviewed:tx.reviewed||0};
    s.locals.set(tx.slot,copy);
    try{
      localStorage.setItem(s.prefix+tx.slot,JSON.stringify(copy));
      // Retire the exact recovered snapshot only after its replacement is durable. A peer's newer edit has another revision.
      if(!tx.dirty&&tx.replaces){
        localStorage.setItem(s.prefix+'recovered/'+tx.replaces,'1');s.retired.add(tx.replaces);
      }
      s.storageIssue='';return true;
    }
    catch{s.storageIssue='本机未能持久保存，请在关闭前复制原文或确认已存服务器。';return false;}
  }
  function adopt(s,row){
    s.tx=emptyDraft();
    if(row){
      const reviewed=[...s.locals.values()].filter(local=>local.id===row.id&&local.note===row.note&&local.reviewed)
        .sort((a,b)=>b.reviewed-a.reviewed)[0];
      Object.assign(s.tx,{assessments:reviewed?.assessments||row.assessments||{},reviewed:reviewed?.reviewed||0,id:row.id,version:row.version,note:row.note,saved:row.saved??row.note,dirty:Boolean(row.dirty),
        pending:row.pending||null,updated:row.updated,archived:row.archived,replaces:row.slot?row.revision:null});
    }
    s.blankInput=false;s.rows=[];s.notice='';
    if(s.ui){s.ui.area.value=s.tx.note;s.ui.results.replaceChildren();}
  }
  function display(s){
    const ui=s.ui;
    if(!ui?.host.isConnected)return;
    const tx=s.tx;
    if(s.authIssue){
      ui.area.value='';ui.area.disabled=true;ui.results.replaceChildren();ui.versions.hidden=true;ui.archive.disabled=true;
      ui.status.textContent='登录身份已变更。原文仍归原账户，请刷新后继续。';return;
    }
    const pieces=[s.notice,s.storageIssue].filter(Boolean);
    if(!pieces.length){
      if(tx.archived)pieces.push('已归档，可选取原文查看。编辑会另存新稿。');
      else if(tx.pending||tx.dirty)pieces.push(s.mode==='server'?'原文待同步服务器。':'原文已保存在本浏览器。');
      else if(tx.note)pieces.push(s.mode==='server'?'原文已存服务器（仅当前登录人可见）。':'原文已保存在本浏览器。');
      else pieces.push(s.mode==='server'?'输入后自动保存到本机与当前登录人的服务器草稿。':'输入后自动保存在当前浏览器的演示身份下。');
    }
    ui.status.textContent=pieces.join(' ');
    ui.known.replaceChildren(new Option('已有草稿店铺',''));
    for(const pair of s.shops.values())ui.known.append(new Option(pair.join(' / '),JSON.stringify(pair)));
    ui.known.hidden=ui.known.options.length===1;
    const docs=documents(s);
    ui.versions.replaceChildren(new Option('选择原文或并行副本',''));
    docs.forEach((doc,i)=>{
      const label=(doc.archived?'已归档':'草稿')+' · '+new Date(doc.updated).toLocaleString('zh-CN')+' · '+doc.note.replace(/\s+/g,' ').slice(0,50);
      ui.versions.append(new Option(label,String(i)));
    });
    ui.versions.hidden=!docs.length;
    ui.versions.onchange=()=>{
      const doc=docs[Number(ui.versions.value)];
      if(ui.versions.value===''||!doc)return;
      adopt(s,doc);s.edited++;display(s);
      if(s.tx.dirty&&s.mode==='server')schedule(s,s.tx);
    };
    ui.retry.hidden=s.mode!=='server'||!s.failed;
    ui.archive.disabled=!tx.note||tx.archived||s.archiving;
    ui.restore.hidden=!s.blankInput;
  }
  function params(s){
    const obj={actor_id:s.actor,account_ref:s.account,site:s.site};
    if(s.product)obj.product_id=s.product;
    return obj;
  }
  async function refresh(s){
    if(s.mode!=='server')return;
    const serial=++s.readSerial, edits=s.edited;
    try{
      const data=await api(endpoint+'?'+new URLSearchParams(params(s)));
      if(serial!==s.readSerial)return;
      s.remote=data.items.map(row=>({...row,updated:Date.parse(row.updated_at)}));
      for(const pair of data.shops||[])s.shops.set(JSON.stringify(pair),pair);
      s.failed=false;s.authIssue=false;if(s.ui)s.ui.area.disabled=false;
      if(s.edited===edits&&!s.tx.dirty&&!s.tx.pending&&!s.blankInput&&!s.archiving){
        const docs=documents(s);
        const preferred=docs.find(row=>row.id===s.tx.id&&!row.archived)||docs.find(row=>!row.archived);
        if(!s.tx.archived && (preferred || !s.tx.note || s.remote.some(row=>row.id===s.tx.id&&row.archived)))adopt(s,preferred);
      }
      display(s);
    }catch(error){
      if(serial!==s.readSerial)return;
      s.failed=true;s.authIssue=[401,403].includes(error.status);s.notice='服务器草稿读取失败。'+error.message;display(s);
    }
  }
  function schedule(s,tx){
    clearTimeout(tx.timer);
    tx.timer=setTimeout(()=>{tx.timer=null;flush(s,tx).catch(()=>{});},450);
  }
  function flush(s,tx){
    if(tx.saving)return tx.saving;
    if(s.mode!=='server'||!tx.dirty||!tx.note.trim())return Promise.resolve();
    // The exact request survives lost responses, reloads and edits made during the request.
    const send=async()=>{
      while(tx.dirty){
        const payload=tx.pending||{...params(s),draft_id:tx.id,expected_version:tx.version,request_id:uuid(),note:tx.note,action:'save'};
        tx.pending=payload;persist(s,tx);
        const ack=await api(endpoint,{method:'PUT',body:JSON.stringify(payload)});
        tx.id=ack.id;tx.version=ack.version;tx.saved=payload.note;tx.pending=null;
        tx.dirty=tx.note!==payload.note;
        persist(s,tx);
        if(s.tx===tx)s.notice=ack.branched?'检测到并行修改。两个版本均已保留，可在原文列表中分别查看。':'';
        s.remote=s.remote.filter(row=>row.id!==tx.id);
        s.remote.push({id:ack.id,version:ack.version,note:payload.note,archived:false,updated:Date.now()});
        if(ack.branched)await refresh(s);
      }
      s.failed=false;
    };
    tx.saving=send().catch(error=>{
      s.failed=true;s.authIssue=[401,403].includes(error.status);
      if(s.tx===tx)s.notice='服务器未确认保存。'+error.message;
      throw error;
    }).finally(()=>{tx.saving=null;display(s);});
    return tx.saving;
  }
  async function archive(s){
    const tx=s.tx;
    if(s.archiving||tx.archived||!tx.note)return;
    if(!window.confirm('已核实这段原文的事项办理成功？归档后可从原文列表回看。'))return;
    s.archiving=true;const ui=s.ui;ui.area.readOnly=true;display(s);
    try{
      if(s.mode==='server'){
        clearTimeout(tx.timer);await flush(s,tx);
        const op=tx.archiveRequest||{...params(s),draft_id:tx.id,expected_version:tx.version,request_id:uuid(),note:tx.note,action:'archive'};
        tx.archiveRequest=op;
        const ack=await api(endpoint,{method:'PUT',body:JSON.stringify(op)});
        tx.version=ack.version;
      }
      tx.archived=true;tx.dirty=false;tx.pending=null;tx.updated=Date.now();
      if(!persist(s,tx)&&s.mode==='demo'){tx.archived=false;s.locals.set(tx.slot,{...tx});throw Error('归档未保存，原文仍在编辑区。');}
      s.remote=s.remote.filter(row=>row.id!==tx.id);
      s.remote.push({...tx,slot:null});
      adopt(s,null);s.edited++;s.notice='已归档，原文可从列表回看。';
    }catch(error){s.failed=s.mode==='server';s.notice='归档未确认，原文已保留。'+error.message;await refresh(s);}
    finally{s.archiving=false;ui.area.readOnly=false;display(s);}
  }
  function edit(s,value){
    s.edited++;s.rows=[];s.ui.results.replaceChildren();
    if(!value.trim()){
      s.blankInput=Boolean(s.tx.note);
      s.notice=s.blankInput?'编辑区为空，上次原文仍保留。可恢复原文，或核实办理结果后归档。':'';
      display(s);return;
    }
    try{sessionStorage.setItem(keyFor([s.mode,s.actor])+'last-edited',JSON.stringify([s.product,s.account,s.site]));}catch{}
    const tx=s.tx;
    if(tx.archived || (s.mode==='demo' && !tx.touched)){
      s.tx=emptyDraft();
      if(!tx.archived)s.tx.replaces=tx.replaces;
    }
    const active=s.tx;active.touched=true;active.revision=uuid();active.assessments={};active.reviewed=0;active.note=value;active.updated=Date.now();active.archived=false;
    active.dirty=s.mode==='server';s.blankInput=false;s.notice='';
    persist(s,active);display(s);
    if(s.mode==='server')schedule(s,active);
  }
  function mount(actor){
    const host=document.getElementById('wb-routing');
    if(!host||host.childNodes.length||!actor)return;
    const product=productId==='all'?null:productId;
    const modeAtMount=mode;
    const scopePreference=keyFor([modeAtMount,actor,product])+'shop-choice';
    let account='',site='';
    try{[account,site]=JSON.parse(sessionStorage.getItem(scopePreference)||'["",""]');}catch{}
    function bind(shop,market,open){
      const prefix=keyFor([modeAtMount,actor,product,shop,market]);
      let s=sessions.get(prefix);
      if(!s){
        s={mode:modeAtMount,actor,product,account:shop,site:market,prefix,remote:[],locals:new Map(),shops:new Map(),retired:new Set(),readSerial:0,
          edited:0,rows:[],storageIssue:'',notice:'',failed:false,archiving:false,tx:emptyDraft()};
        sessions.set(prefix,s);readLocal(s);
        adopt(s,documents(s).find(row=>!row.archived));
      }
      visible=s;
      const details=elem('details',null,{className:'panel wb-routing-panel',open});
      details.append(elem('summary','把一段运营记录分给对应岗位'));
      const area=elem('textarea',null,{value:s.blankInput?'':s.tx.note,maxLength:6000,placeholder:'例如：SKU A 的在途货件缺 ETA。新主图已交二版，待审核。',ariaLabel:'待分派的运营记录'});
      const status=elem('p',null,{role:'status',ariaLive:'polite'});
      const storeLabel=shop?shop+' / '+market:'跨店';
      const productLabel=ws.products.find(p=>p.id===product)?.model||'全部产品';
      const label=elem('small','草稿范围：'+productLabel+' · '+storeLabel);
      const versions=elem('select',null,{ariaLabel:'已保存的原文与并行副本'});
      const makeButton=text=>elem('button',text,{type:'button',className:'btn'});
      const retry=makeButton('重试同步'),restore=makeButton('恢复原文'),archiveButton=makeButton('已办理，归档原文');
      const switcher=makeButton('切换草稿店铺'),scopeForm=elem('div',null,{hidden:true});
      const shopInput=elem('input',null,{ariaLabel:'草稿店铺标识',placeholder:'店铺标识',value:shop,maxLength:120});
      const siteInput=elem('input',null,{ariaLabel:'草稿站点',placeholder:'站点，例如 US',value:market,maxLength:4});
      const apply=makeButton('打开此范围草稿');
      const known=elem('select',null,{ariaLabel:'已有草稿店铺'});
      known.onchange=()=>{if(known.value)[shopInput.value,siteInput.value]=JSON.parse(known.value);};
      scopeForm.append(known,shopInput,siteInput,apply);
      switcher.onclick=()=>{scopeForm.hidden=!scopeForm.hidden;if(!scopeForm.hidden)shopInput.focus();};
      apply.onclick=()=>{
        const nextShop=shopInput.value.trim(),nextSite=siteInput.value.trim().toUpperCase();
        if(Boolean(nextShop)!==Boolean(nextSite)||(nextSite&&!/^[A-Z]{2,4}$/.test(nextSite))){status.textContent='请同时填写店铺标识和站点，跨店草稿请将两项留空。';return;}
        try{sessionStorage.setItem(scopePreference,JSON.stringify([nextShop,nextSite]));}catch{}
        bind(nextShop,nextSite,true);
      };
      const scopeSelect=elem('select',null,{ariaLabel:'选择办理产品'});
      scopeSelect.append(new Option('选择办理产品',''));
      ws.products.forEach(p=>scopeSelect.append(new Option(p.model,p.id)));
      scopeSelect.value=product||'';
      const button=makeButton('识别并预览'),results=elem('div',null,{ariaLive:'polite'});
      const actions=elem('div',null,{className:'wb-toolbar'});
      actions.append(scopeSelect,button,restore,retry,archiveButton);
      const editField=elem('label',null,{className:'field'});editField.append(elem('span','待分派原文'),area);
      details.append(label,switcher,scopeForm);
      let last;
      try{last=JSON.parse(sessionStorage.getItem(keyFor([modeAtMount,actor])+'last-edited')||'null');}catch{}
      if(last&&JSON.stringify(last)!==JSON.stringify([product,shop,market])){
        const [lastProduct,lastShop,lastSite]=last;
        const target=ws.products.find(p=>p.id===lastProduct);
        if(!lastProduct||target){
          const resume=makeButton('恢复上次草稿范围 · '+(target?.model||'全部产品')+' / '+(lastShop?lastShop+' '+lastSite:'跨店'));
          resume.onclick=()=>{
            if(lastProduct===product){bind(lastShop,lastSite,true);return;}
            try{sessionStorage.setItem(keyFor([modeAtMount,actor,lastProduct])+'shop-choice',JSON.stringify([lastShop,lastSite]));}catch{}
            const select=document.getElementById('productSelect');select.value=lastProduct||'all';select.dispatchEvent(new Event('change',{bubbles:true}));
            const next=document.querySelector('#wb-routing > details');if(next)next.open=true;
          };
          details.append(resume);
        }
      }
      details.append(editField,status,versions,actions,results);
      host.replaceChildren(details);
      s.ui={host,area,status,results,versions,retry,archive:archiveButton,restore,known};
      area.oninput=()=>edit(s,area.value);
      restore.onclick=()=>{area.value=s.tx.note;s.blankInput=false;s.notice='';display(s);area.focus();};
      archiveButton.onclick=()=>archive(s);
      retry.onclick=async()=>{try{await flush(s,s.tx);await refresh(s);}catch{}};
      const session=s;
      const active=()=>visible===s&&host.isConnected&&s.ui.area===area;
    async function renderRows(){
      results.replaceChildren();
      if (!session.rows.length) return;
      let defs;
      try {defs=await load();} catch {results.textContent='加载办理目录失败，请重新识别。';return;}
      if (!active()) return;
      for (const row of session.rows){
        const box=elem('div',null,{className:'wb-routing-row'});
        const quote=elem('blockquote',row.quote);
        const choice=elem('select',null,{ariaLabel:'建议办理板块'});
        choice.append(new Option('选择板块',''));
        defs.forEach(p=>choice.append(new Option(p.title,p.id)));
        choice.value=row.page||'';
        const section=elem('select',null,{ariaLabel:'办理子页'});
        const zone=elem('input',null,{ariaLabel:'店铺业务时区',placeholder:'未登记的店铺请填业务时区，如 Asia/Shanghai',maxLength:100});
        const clue=elem('small','');
        let evidence=[];
        function update(){
          const mapped=session.rows.find(item=>item.quote===row.quote&&item.page===choice.value);
          evidence=mapped?.priorityEvidence||(row.page===null?row.priorityEvidence:[]);
          clue.textContent=evidence.length?'规则建议重点 · '+evidence.map(item=>item.kind+'「'+item.quote+'」').join('、'):
            '规则未匹配到明确紧急、阻塞或截止表述，可人工选择重点。';
          zone.hidden=choice.value!=='shop'||!s.account;
          section.replaceChildren();
          const page=defs.find(p=>p.id===choice.value);
          page?.sections.forEach(s=>section.append(new Option(s.title,s.id)));
        }
        choice.onchange=update;update();
        const reviewLabel=elem('label',null,{className:'field'});
        reviewLabel.append(elem('span','办理前人工判断（本机预选）'));
        const review=elem('select',null,{ariaLabel:'办理前人工判断'});
        const reviewKey=JSON.stringify([row.page,row.quote]);
        for(const [key,title] of [['undecided','待人工核实'],['priority','重点'],['normal','常规']])review.append(new Option(title,key));
        review.value=s.tx.assessments[reviewKey]||'undecided';
        review.onchange=()=>{
          s.tx.assessments[reviewKey]=review.value;s.tx.reviewed=Date.now();
          persist(s,s.tx);display(s);
        };
        reviewLabel.append(review);
        const accept=elem('button','打开办理草稿',{type:'button',className:'btn'});
        accept.onclick=async()=>{
          if(!choice.value||!section.value){toast('请选择板块和办理子页');return;}
          if(accept.disabled)return;
          accept.disabled=true;
          const signal={page_id:choice.value,section_id:section.value,object_ref:'',title:row.quote.slice(0,110),source_note:row.quote,reason:row.quote,
            priority_review:{suggested:evidence.length>0,evidence,decision:review.value}};
          try{
            if(s.mode==='server'){
              const auth=await api('/api/workflow-auth/session');
              if(!active()||!accept.isConnected)return;
              if(auth.user?.id!==s.actor){s.authIssue=true;display(s);return;}
            }
            if(signal.page_id==='shop'&&s.account){
              const scopes=s.mode==='server'?(await api('/api/workbench/shop-scopes')).scopes:[];
              if(!active()||!accept.isConnected)return;
              const known=scopes.filter(x=>x.account_ref===s.account&&x.site===s.site);
              const entered=zone.value.trim();
              const match=entered?known.find(x=>x.business_timezone===entered):known.length===1?known[0]:null;
              const timezone=match?.business_timezone||entered;
              if(!timezone||(known.length&&!match))throw Error('请核实并填写此店铺站点的业务时区。');
              try{new Intl.DateTimeFormat('zh-CN',{timeZone:timezone});}catch{throw Error('请填写有效的 IANA 业务时区，如 Asia/Shanghai。');}
              Object.assign(signal,{account_ref:s.account,site:s.site,business_timezone:timezone,connection_id:match?.connection_id});
            }else{
              const product=ws.products.find(p=>p.id===scopeSelect.value);
              if(!product)throw Error('此环节按产品办理，请选择实际产品。');
              if(s.site&&product.marketplace!==s.site)throw Error('所选产品的站点与原文店铺不同，请重新核对。');
              Object.assign(signal,{product_id:product.id,site:product.marketplace||''});
            }
            window.workbenchUI.openSignal(signal);
          }catch(error){status.textContent=error.message;}
          finally{accept.disabled=false;}
        };
        box.append(quote,elem('small',row.terms.length?'识别依据：'+row.terms.join('、'):'没有匹配的业务词，请人工分派'),clue,reviewLabel,choice,section,zone,accept);
        results.append(box);
      }
    }
    button.onclick=()=>{session.rows=classify(area.value);if(!session.rows.length)results.textContent='先填写运营记录。';else renderRows();};
      if(s.rows.length)renderRows();
      display(s);refresh(s);
      if(s.tx.dirty&&s.mode==='server')schedule(s,s.tx);
    }
    bind(account,site,false);
  }
  window.addEventListener('storage',event=>{
    const s=visible;
    if(!s||!event.key?.startsWith(s.prefix)||!s.ui?.host.isConnected)return;
    readLocal(s);display(s);
    if(s.mode==='server'&&!s.tx.dirty){
      let changed;
      try{changed=JSON.parse(event.newValue);}catch{}
      if(changed?.slot&&!changed.dirty&&!changed.pending){clearTimeout(s.peerTimer);s.peerTimer=setTimeout(()=>refresh(s),200);}
    }
  });
  window.addEventListener('online',()=>{
    for(const s of sessions.values())if(s.mode==='server'&&s.tx.dirty)flush(s,s.tx).catch(()=>{});
    if(visible)refresh(visible);
  });
  document.addEventListener('visibilitychange',()=>{
    if(document.visibilityState==='visible'&&visible?.ui?.host.isConnected&&visible.mode==='server')refresh(visible);
  });
  root.workbenchTriage={mount};
})(typeof window==='undefined'?globalThis:window);
