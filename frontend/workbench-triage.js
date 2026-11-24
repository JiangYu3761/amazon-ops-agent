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
      for (const candidate of candidates) {
        const key = JSON.stringify([quote,candidate.page]);
        if (!seen.has(key)) {
          seen.add(key);
          rows.push({...candidate, quote});
        }
      }
    }
    return rows;
  }
  if (typeof module !== 'undefined' && module.exports) {module.exports = {classify}; return;}
  let catalog, cache;
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
  function mount() {
    const el=document.getElementById('wb-routing');
    if (!el || el.childNodes.length) return;
    const identity=mode==='demo'?'demo':window.processUI?.currentUser()?.id;
    if (!identity) {cache=null;return;}
    const scope=identity+'|'+productId;
    if (!cache || cache.scope!==scope) cache={scope, note:'', rows:[]};
    const session=cache;
    const details=elem('details',null,{className:'panel wb-routing-panel'});
    details.append(elem('summary','把一段运营记录分给对应岗位'));
    const area=elem('textarea',null,{value:session.note,maxLength:6000,placeholder:'例如：SKU A 的在途货件缺 ETA。新主图已交二版，待审核。',ariaLabel:'待分派的运营记录'});
    const scopeSelect=elem('select',null,{ariaLabel:'选择办理产品'});
    scopeSelect.append(new Option('选择办理产品',''));
    ws.products.forEach(p=>scopeSelect.append(new Option(p.model,p.id)));
    scopeSelect.value=productId==='all'?'':productId;
    const button=elem('button','识别并预览',{type:'button',className:'btn'});
    const results=elem('div',null,{'ariaLive':'polite'});
    details.append(area,scopeSelect,button,results); el.append(details);
    area.oninput=()=>{session.note=area.value;session.rows=[];results.replaceChildren();};
    async function renderRows(){
      results.replaceChildren();
      if (!session.rows.length) return;
      let defs;
      try {defs=await load();} catch {results.textContent='加载办理目录失败，请重新识别。';return;}
      if (!el.isConnected || session!==cache) return;
      for (const row of session.rows){
        const box=elem('div',null,{className:'wb-routing-row'});
        const quote=elem('blockquote',row.quote);
        const choice=elem('select',null,{ariaLabel:'建议办理板块'});
        choice.append(new Option('选择板块',''));
        defs.forEach(p=>choice.append(new Option(p.title,p.id)));
        choice.value=row.page||'';
        const section=elem('select',null,{ariaLabel:'办理子页'});
        function update(){
          section.replaceChildren();
          const page=defs.find(p=>p.id===choice.value);
          page?.sections.forEach(s=>section.append(new Option(s.title,s.id)));
        }
        choice.onchange=update;update();
        const accept=elem('button','打开办理草稿',{type:'button',className:'btn'});
        accept.onclick=()=>{
          const p=ws.products.find(p=>p.id===scopeSelect.value);
          if (!p || !choice.value || !section.value) {toast('请选择产品、板块和办理子页');return;}
          window.workbenchUI.openSignal({page_id:choice.value,section_id:section.value,product_id:p.id,
            site:p.marketplace||'',object_ref:'',title:row.quote.slice(0,110),reason:row.quote});
        };
        box.append(quote,elem('small',row.terms.length?'识别依据：'+row.terms.join('、'):'没有匹配的业务词，请人工分派'),choice,section,accept);
        results.append(box);
      }
    }
    button.onclick=()=>{session.rows=classify(area.value);if(!session.rows.length)results.textContent='先填写运营记录。';else renderRows();};
    if(session.rows.length)renderRows();
  }
  root.workbenchTriage={mount};
})(typeof window==='undefined'?globalThis:window);
