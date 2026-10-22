(() => {
  'use strict';
  if(window.__zhuiyinUploadsBootstrapped)return;
  window.__zhuiyinUploadsBootstrapped=true;
  const $ = id => document.getElementById(id);
  function initialize() {
  if(window.__zhuiyinUploadsInitialized||document.readyState==='loading')return;
  const bridge=window.Zhuiyin;
  if(!bridge||!$('import-workspace')) {
    const waiting=$('import-message');if(waiting){waiting.className='import-message warning';waiting.textContent='等待工作台';waiting.dataset.waitingForBridge='true';}
    return;
  }
  window.__zhuiyinUploadsInitialized=true;
  window.removeEventListener('zhuiyin:ready',initialize);
  const waiting=$('import-message');if(waiting?.dataset.waitingForBridge==='true'){waiting.className='import-message hidden';waiting.textContent='';delete waiting.dataset.waitingForBridge;}
  const MAX_BYTES = 2 * 1024 * 1024;
  const faultLabels = {commit_response_lost:'提交后响应丢失',stale_read:'读取过期状态',truncate_response:'响应内容缺失',latency:'工具调用延迟',precommit_timeout:'写入前超时',ack_without_commit:'未提交却返回成功',corrupt_payload:'业务内容损坏'};
  const state = {data:null,importing:false,batchId:null,batchPolling:false,batchPending:false,batchStartedAt:0,batchSource:null};
  const text = value => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value,null,2) : String(value);
  function node(tag, content, className) {const element=document.createElement(tag);if(content!=null)element.textContent=text(content);if(className)element.className=className;return element;}
  const array = value => Array.isArray(value) ? value : [];
  const faultsText = faults => array(faults).map(id=>faultLabels[id]||text(id)).join('、')||'无故障';
  function pill(content,kind='') {return node('span',content,'status-pill '+kind);}
  function button(label,handler,className='button button-light button-small') {const element=node('button',label,className);element.type='button';element.addEventListener('click',handler);return element;}
  function localLink(label,url,className='button button-light button-small') {try{const parsed=new URL(url,window.location.origin);if(parsed.origin!==window.location.origin||!parsed.pathname.startsWith('/api/'))return null;const link=node('a',label,className);link.href=parsed.pathname+parsed.search;return link;}catch{return null;}}
  function message(content,kind='info') {const box=$('import-message');box.className='import-message '+kind;box.textContent=content;}
  function showPage(name) {window.ZhuiyinUI?.showPage(name);}
  function help(content) {const element=node('span','?','help-tip');element.title=content;element.setAttribute('aria-label',content);element.tabIndex=0;return element;}
  function addLimitations(items) {
    if(window.ZhuiyinUI?.setLimitations){window.ZhuiyinUI.setLimitations('import',items);return;}
    const host=$('limitations-text');if(!host)return;
    let block=host.querySelector('[data-limitation-source="import"]');
    if(!block){block=node('div');block.dataset.limitationSource='import';host.append(block);}
    block.replaceChildren();const unique=[...new Set(items.filter(item=>typeof item==='string'&&item))];
    if(unique.length){const list=node('ul');unique.forEach(item=>list.append(node('li',item)));block.append(list);}
  }
  function paginate(container,items,pageSize,renderPage) {return window.ZhuiyinUI.pager({container,items,pageSize,renderPage});}
  function updateActions() {
    const busy=bridge.isBusy();const blocked=state.importing||busy;
    $('import-file').disabled=blocked;$('parse-paste-btn').disabled=blocked;$('paste-format').disabled=blocked;$('paste-content').disabled=blocked;
    $('file-drop-zone').classList.toggle('is-disabled',blocked);$('file-drop-zone').setAttribute('aria-disabled',String(blocked));$('file-drop-zone').setAttribute('aria-busy',String(state.importing));
    document.querySelectorAll('[data-scenario-action]').forEach(element=>element.disabled=blocked);
    const batchButton=$('run-all-scenarios');if(batchButton)batchButton.disabled=blocked||state.batchPending||!array(state.data?.scenarios).length;
    $('resume-batch-btn').disabled=busy||state.batchPolling;$('cancel-batch-btn').disabled=!state.batchPending;
    document.querySelectorAll('[data-batch-detail]').forEach(element=>element.disabled=busy||element.dataset.resultAvailable==='false');
    $('parse-paste-btn').textContent=state.importing?'正在解析…':'解析粘贴内容';
  }
  function makeTable(headers,rows,label) {
    const wrap=node('div',null,'table-wrap'),table=node('table',null,'data-table imported-table');table.setAttribute('aria-label',label);
    const thead=node('thead'),heading=node('tr');headers.forEach(content=>heading.append(node('th',content)));thead.append(heading);table.append(thead);
    const tbody=node('tbody');rows.forEach(cells=>{const tr=node('tr');cells.forEach(content=>{const td=node('td');if(content instanceof Node)td.append(content);else td.textContent=text(content);tr.append(td);});tbody.append(tr);});table.append(tbody);wrap.append(table);return wrap;
  }
  function ruleConfig(config={}) {return {scenario:config.scenario??'equipment',ticket_title:config.ticket_title??'教学楼门禁故障',ticket_priority:config.ticket_priority??'normal',equipment:config.equipment??'单反相机',stock:config.stock??3,quantity:config.quantity??1,request_id:config.request_id??'req-imported-001',seed:config.seed??42,faults:array(config.faults),prompt_policy:['neutral','recovery'].includes(config.prompt_policy)?config.prompt_policy:'neutral',agent_mode:'rules'};}
  function showSource(data) {
    $('import-preview').classList.remove('hidden');$('import-filename').textContent=data.filename||'粘贴内容';$('import-kind').textContent=data.kind==='scenarios'?'场景数据':'静态日志';
    $('import-sha').textContent=data.source_sha256||data.source?.sha256||'未返回文件标识';
    const warnings=array(data.warnings);$('import-warnings').classList.toggle('hidden',warnings.length===0);
    if(warnings.length){const title=node('strong','导入提醒（'+warnings.length+'）'),list=node('ul');warnings.forEach(item=>list.append(node('li',item)));$('import-warnings').replaceChildren(title,list);}
  }
  async function performImport(filename,readContent) {
    if(state.importing||bridge.isBusy()){message('请等待实验结束。','warning');return;}
    state.importing=true;updateActions();message('正在解析…');
    try {
      const content=typeof readContent==='function'?await readContent():readContent;
      if(!String(content).trim())throw new Error('数据内容为空，请选择文件或粘贴内容。');
      if(new TextEncoder().encode(content).length>MAX_BYTES)throw new Error('文件超过 2 MB，请减少数据量后重新导入。');
      const data=await bridge.post('/api/import',{filename,content},45000);
      if(!['scenarios','trace'].includes(data.kind))throw new Error('服务未识别出可支持的数据类型。');
      state.data=data;showSource(data);if(data.kind==='scenarios')renderScenarios(data);else renderAnalyses(data);
      message(data.kind==='scenarios'?'已解析 '+array(data.scenarios).length+' 个场景':'已完成日志检查','info');
    }catch(error){message(error.message||'导入未成功，请核对模板格式。','error');}
    finally{state.importing=false;updateActions();$('import-file').value='';}
  }
  async function importFile(file) {
    if(!file)return;
    if(file.size>MAX_BYTES){message('文件超过 2 MB，请减少数据量后重新导入。','error');return;}
    if(!/\.(csv|json|jsonl)$/i.test(file.name)){message('当前只接受 CSV、JSON 或 JSONL 数据文件，不执行源码。','error');return;}
    await performImport(file.name,()=>file.text());
  }
  function scenarioAction(scenario,run=true) {
    if(bridge.isBusy()){message('请先等待正在运行的验证结束。','warning');return;}
    const config=ruleConfig(scenario.config||scenario);
    if(!bridge.applyScenario(config,scenario.name||'上传场景'))return;
    showPage('workbench');
    if(run)bridge.runExperiment();
  }
  function renderScenarios(data) {
    const scenarios=array(data.scenarios);$('import-count').textContent=scenarios.length+' 个场景';addLimitations(['导入只读取数据，上传的工具、SQL 和代码都不会执行。','场景载入后默认使用规则模式；模型模式需在工作台手动选择。']);
    const heading=node('div',null,'preview-heading'),description=node('h3','场景预览');description.append(help('查看参数后载入工作台，或批量运行所有场景。'));
    const runAll=button('批量验证',startBatch,'button button-primary');runAll.id='run-all-scenarios';runAll.disabled=scenarios.length===0;heading.append(description,runAll);
    const pages=node('div',null,'paged-data');$('import-data-content').replaceChildren(heading,pages);
    paginate(pages,scenarios,10,(slice,page)=>{
    const rows=slice.map((scenario,offset)=>{const index=(page-1)*10+offset,config=scenario.config||scenario;const name=node('div',null,'imported-scenario-name');name.append(node('strong',scenario.name||'场景 '+(index+1)));if(scenario.description)name.title=scenario.description;
      const params=node('div',null,'imported-params');params.append(node('strong',config.scenario==='ticket'?config.ticket_title:config.equipment),node('small',config.scenario==='ticket'?'工单优先级 '+text(config.ticket_priority):'库存 '+text(config.stock)+' / 借用 '+text(config.quantity)),node('small','请求 '+text(config.request_id)));
      const action=button('载入并运行',()=>scenarioAction(scenario));action.dataset.scenarioAction='run';action.title='载入工作台，以规则模式运行';return [index+1,name,params,faultsText(config.faults),action];});
    pages.replaceChildren(rows.length?makeTable(['序号','场景','参数','故障','操作'],rows,'上传场景预览'):node('p','暂无场景','empty-state'));updateActions();
    });
  }
  function renderAnalyses(data) {
    const analyses=array(data.analyses);$('import-count').textContent=analyses.length+' 份检查';addLimitations(analyses.flatMap(analysis=>array(analysis.limitations)));
    const toolbar=node('div',null,'preview-heading');const intro=node('h3','日志检查');intro.append(help('检查规则、最终状态和原始事件见下方详情。'));toolbar.append(intro);
    const download=localLink('下载报告',data.export_url);if(download)toolbar.append(download);
    const pages=node('div',null,'paged-data'),contents=[toolbar,pages];if(data.source_report?.status==='partial'||data.source_report?.completed===false)contents.splice(1,0,node('p','来源报告未全部完成；各组状态分别列出。','subtle-label'));
    if(data.scenario){const suggestion=node('div',null,'trace-scenario-action preview-heading');const explanation=node('strong','使用记录中的参数');const config=data.scenario.config||data.scenario;const load=button('载入工作台',()=>scenarioAction({name:'日志中的场景参数',config},false));load.dataset.scenarioAction='load';load.title='将场景参数填入工作台';suggestion.append(explanation,load);contents.push(suggestion);}
    $('import-data-content').replaceChildren(...contents);
    paginate(pages,analyses,1,(slice,page)=>pages.replaceChildren(...(slice.length?slice.map(analysis=>analysisCard(analysis,page-1)):[node('p','暂无记录','empty-state')])));
  }
  function analysisCard(analysis,index) {
    const verdicts={violation:{label:'发现违规',kind:'failed',title:'静态证据检查发现违规'},inconclusive:{label:'证据不足',kind:'warn',title:'记录不足，无法判定'},no_violation_found:{label:'未发现违规',kind:'success',title:'在当前检查范围内未发现违规'}};
    const execution=analysis.execution_status;const verdict=execution==='not_run'?{label:'未运行',kind:'warn',title:'本组没有执行，结果未知'}:execution==='incomplete'?{label:'未完成',kind:'warn',title:'本组执行未完成，结果未知'}:analysis.reported_outcome==='unknown'&&analysis.verdict!=='violation'?{label:'报告无法确认',kind:'warn',title:'模型已结束；无法确认不自动判定报告正确或错误'}:verdicts[analysis.verdict]||verdicts.inconclusive;
    const card=node('article',null,'trace-analysis analysis-card '+(analysis.verdict==='violation'?'has-violation':'evidence-neutral'));
    const heading=node('div',null,'analysis-title'),status=pill(verdict.label,verdict.kind);status.title=verdict.title;heading.append(node('h3',analysis.name||'执行记录 '+(index+1)),status);card.append(heading);if(execution==='not_run'||execution==='incomplete')card.append(node('p',execution==='not_run'?'本组未运行，保留未知结果。':'本组未完成，保留已有记录。','subtle-label'));
    const stats=analysis.stats||{},metrics=node('div',null,'analysis-stats');[['事件',stats.events],['请求',stats.requests],['写入记录',stats.effects]].forEach(([label,value])=>{metrics.append(node('span',label+' '+text(value)));});card.append(metrics);
    if(Object.hasOwn(analysis,'reported_outcome')||analysis.evaluation_protocol||analysis.static_evaluation){
      const imported=window.ZhuiyinOutcomes.imported({...analysis,skipped:execution==='not_run'}),view=imported.staticView,facts=node('div',null,'analysis-stats');
      facts.append(node('span','评价协议 '+(analysis.evaluation_protocol||'未记录')),node('span',(imported.hasStatic?'静态判断完成 ':'记录完成 ')+view.actualLabel),node('span','业务检查 '+view.businessLabel),node('span','模型报告 '+view.reportLabel),node('span','报告核对 '+view.assessmentLabel));card.append(facts);
      if(imported.hasStatic){const scope=node('p','静态核验范围：'+imported.scopeLabel+'；业务判定证据：'+imported.evidenceLabel+'。来源标签单独保留，不作为核验结论。','subtle-label');card.append(scope);}
      if(imported.sourceView){const source=imported.sourceView,claims=node('div',null,'analysis-stats');claims.append(node('span','来源声明（仅保留，不作判定依据）'),node('span','协议 '+imported.sourceProtocol),node('span','完成 '+source.actualLabel),node('span','业务检查 '+source.businessLabel),node('span','报告核对 '+source.assessmentLabel));card.append(claims);}
    }
    const findings=array(analysis.findings);
    if(findings.length){const list=node('div',null,'finding-list');findings.forEach(finding=>{const severity=['error','warning','info'].includes(finding.severity)?finding.severity:'info';const details=node('details',null,'finding '+severity);const summary=node('summary');summary.append(node('span',severity==='error'?'!':severity==='warning'?'△':'i','finding-symbol'),node('span',finding.message||finding.code||'检查发现'));if(finding.code)summary.append(node('code',finding.code));details.append(summary);const body=node('div',null,'finding-body');const steps=array(finding.steps);if(steps.length)body.append(node('p','相关步骤：'+steps.map(text).join('、')));body.append(node('pre',finding.evidence==null?'暂无详细数据':text(finding.evidence)));details.append(body);list.append(details);});card.append(list);}else{card.append(node('p',analysis.verdict==='inconclusive'?'记录不足':'暂无异常发现','analysis-no-findings'));}
    const raw=node('details',null,'uploaded-raw-events');raw.append(node('summary','原始记录'));const body=node('div',null,'evidence-detail-body');raw.append(body);let loaded=false;raw.addEventListener('toggle',()=>{if(raw.open&&!loaded){if(analysis.summary)body.append(node('p',analysis.summary));body.append(node('pre',JSON.stringify({task:analysis.task??null,contracts:analysis.contracts??null,source_evaluation:analysis.source_evaluation??null,static_evaluation:analysis.static_evaluation??null,final_state:analysis.final_state??null,events:array(analysis.events)},null,2)));loaded=true;}});card.append(raw);
    return card;
  }
  async function startBatch() {
    if(bridge.isBusy()||state.batchPending||state.importing)return;
    const data=state.data;if(!data||data.kind!=='scenarios'||!array(data.scenarios).length)return;
    state.batchId=null;state.batchStartedAt=Date.now();state.batchPending=true;state.batchSource={filename:data.filename,sha256:data.source_sha256};bridge.setExternalBusy(true);updateActions();
    $('batch-results').classList.remove('hidden');$('batch-summary').replaceChildren();$('batch-cases').replaceChildren();$('batch-export-btn').classList.add('hidden');$('resume-batch-btn').classList.add('hidden');$('cancel-batch-btn').classList.remove('hidden');$('batch-progress').style.width='0%';setBatchStatus('运行中','running');$('batch-progress-text').textContent='正在验证 '+data.scenarios.length+' 个场景';
    try {const payload={scenarios:data.scenarios.map(item=>({name:item.name,config:ruleConfig(item.config||item)})),source:state.batchSource};const response=await bridge.post('/api/run-batch',payload);if(!response.run_id)throw new Error('服务没有返回批量实验编号。');state.batchId=response.run_id;await pollBatch();}
    catch(error){batchReadError(error);}
    finally{state.batchPolling=false;bridge.setExternalBusy(false);updateActions();}
  }
  function setBatchStatus(label,kind) {$('batch-status').textContent=label;$('batch-status').className='status-pill '+(kind||'');}
  function batchReadError(error) {setBatchStatus('等待','warn');$('batch-progress-text').textContent=error.message||'读取中断';if(state.batchId){$('resume-batch-btn').classList.remove('hidden');message('读取中断，可恢复读取。','warning');}else{state.batchPending=false;$('cancel-batch-btn').classList.add('hidden');message(error.message||'批量验证未能启动。','error');}}
  async function pollBatch() {
    if(state.batchPolling)return;state.batchPolling=true;
    while(state.batchId){
      if(Date.now()-state.batchStartedAt>15*60*1000)throw new Error('已等待 15 分钟，可点击“恢复读取”继续检查同一批次。');
      const path='/api/runs/'+encodeURIComponent(state.batchId),job=await bridge.request(path+'?lite=1');const event=array(job.events).at(-1);let progress=Number(job.progress)||0;if(progress>0&&progress<=1)progress*=100;$('batch-progress').style.width=Math.max(0,Math.min(100,progress))+'%';$('batch-progress-text').textContent=typeof event==='string'?event:event?.message||'正在检查场景并保存数据库记录。';setBatchStatus('运行中','running');
      if(['done','error','interrupted'].includes(job.status)){
        const full=await bridge.request(path);
        if(full.status==='done'){if(full.result?.kind!=='batch')throw new Error('服务返回的结果不是批量场景结果。');renderBatch(full.result,false);state.batchPending=false;$('resume-batch-btn').classList.add('hidden');$('cancel-batch-btn').classList.add('hidden');return;}
        const partial=full.result||full.partial_result;
        if(partial?.kind==='batch'){renderBatch(partial,true);state.batchPending=false;$('resume-batch-btn').classList.add('hidden');$('cancel-batch-btn').classList.add('hidden');$('batch-progress-text').textContent=text(full.error||partial.error||'批量验证中断，已保留完成的结果。');message('批量验证中断，已完成的场景和记录仍可查看、导出。','warning');return;}
        state.batchPending=false;$('resume-batch-btn').classList.add('hidden');$('cancel-batch-btn').classList.add('hidden');setBatchStatus('失败','failed');$('batch-progress-text').textContent=text(full.error||'批量执行失败。');return;
      }
      await new Promise(resolve=>setTimeout(resolve,700));
    }
  }
  let detailReturn=null, detailScroll=0;
  function closeBatchDetail(restore=false) {
    $('import-detail').hidden=true;
    $('import-list').hidden=false;
    if(restore){window.scrollTo({top:detailScroll,behavior:'instant'});detailReturn?.focus({preventScroll:true});}
  }
  function showBatchDetail(item,trigger) {
    if(bridge.isBusy())return;
    const report=item.result, renderer=window.ZhuiyinHistory?.renderReport;
    if(!report){message('该场景还没有完整结果。','warning');return;}
    if(!renderer){message('详情尚未加载，请刷新页面。','warning');return;}
    const content=renderer(report);
    const diagnosis=node('details',null,'product-panel'),body=node('div');
    diagnosis.append(node('summary','查看故障诊断'),body);
    let drawn=false;
    diagnosis.addEventListener('toggle',()=>{if(diagnosis.open&&!drawn){drawn=true;window.ZhuiyinDiagnostics?.mount(body,report);}});
    $('import-detail-content').replaceChildren(...content,diagnosis);
    $('import-detail-title').textContent=item.name||'场景结果详情';
    $('import-detail-export').href='/api/runs/'+encodeURIComponent(state.batchId)+'/export';
    detailScroll=window.scrollY||0;detailReturn=trigger;
    $('import-list').hidden=true;$('import-detail').hidden=false;
    window.scrollTo({top:0,behavior:'instant'});$('import-detail-title').focus({preventScroll:true});
  }
  $('import-detail-back').addEventListener('click',()=>closeBatchDetail(true));
  window.addEventListener('zhuiyin:page',event=>{if(event.detail?.page==='import')closeBatchDetail();});
  function renderBatch(data,partial) {
    const cases=array(data.cases),summary=data.summary||{},completed=Number(data.completed??cases.length),total=Number(data.total??cases.length);
    setBatchStatus(partial?'失败':'成功',partial?'failed':'success');if(!partial)$('batch-progress').style.width='100%';
    $('batch-progress-text').textContent=(partial?'已保留 ':'已完成 ')+completed+' / '+total+' 个场景';
    const metrics=node('div',null,'batch-metrics');[['完成场景',completed+' / '+total],['原始失败',text(summary.baseline_failures)],['幂等＋核验失败',text(summary.fixed_failures)]].forEach(([label,value])=>{const item=node('div');item.append(node('span',label),node('strong',value));metrics.append(item);});
    const source=data.source||state.batchSource||{};const sourceText=node('span',text(source.filename),'batch-source');sourceText.title='本次导入的文件';$('batch-summary').replaceChildren(metrics,sourceText);addLimitations(array(data.limitations));
    const heading=node('div',null,'preview-heading');heading.append(node('h3','批量结果'),help('成功表示没有违反检查规则，也可能是正确拒绝了库存不足的请求。查看详情可打开已保存的结果。'));
    const pages=node('div',null,'paged-data'),children=[heading,pages];
    if(data.artifacts?.bundle){const bundle=localLink('下载数据库',data.artifacts.bundle);if(bundle){const row=node('div',null,'batch-downloads');row.append(bundle);children.push(row);}}
    $('batch-cases').replaceChildren(...children);
    paginate(pages,cases,10,(slice,page)=>{
    const rows=slice.map((item,offset)=>{const index=(page-1)*10+offset,cfg=item.config||item.result?.config||{};const title=node('div',null,'imported-scenario-name');title.append(node('strong',item.name||'场景 '+(index+1)),node('small',cfg.scenario==='ticket'?'工单 · '+text(cfg.ticket_title)+' / '+text(cfg.ticket_priority):text(cfg.equipment)+' · 库存 '+text(cfg.stock)+' / 借用 '+text(cfg.quantity)));
      const oldStatus=item.baseline_failed===true?pill('失败','failed'):item.baseline_failed===false?pill('成功','success'):pill('等待','warn');const newStatus=item.fixed_failed===true?pill('失败','failed'):item.fixed_failed===false?pill('成功','success'):pill('等待','warn');
      let minimal='—';const result=item.result;if(result?.baseline?.failed&&result?.minimization?.stable)minimal=faultsText(item.minimized_faults||result.minimized_faults);
      const view=button('查看详情',()=>{if(bridge.isBusy())return;if(!result){message('该场景还没有完整结果。','warning');return;}showBatchDetail(item,view);});view.dataset.batchDetail=String(index);view.dataset.resultAvailable=String(Boolean(result));view.disabled=!result||bridge.isBusy();return [index+1,title,oldStatus,newStatus,minimal,view];});
    pages.replaceChildren(rows.length?makeTable(['序号','场景','原始','幂等＋核验','保留故障','操作'],rows,'批量场景验证结果'):node('p','暂无结果','empty-state'));updateActions();
    });
    $('batch-export-btn').href='/api/runs/'+encodeURIComponent(state.batchId)+'/export';$('batch-export-btn').classList.remove('hidden');
  }
  const drop=$('file-drop-zone'),file=$('import-file');
  file.addEventListener('click',event=>event.stopPropagation());file.addEventListener('change',()=>importFile(file.files?.[0]));
  drop.addEventListener('click',()=>{if(!state.importing&&!bridge.isBusy())file.click();});drop.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();if(!state.importing&&!bridge.isBusy())file.click();}});
  ['dragenter','dragover'].forEach(name=>drop.addEventListener(name,event=>{event.preventDefault();if(!state.importing&&!bridge.isBusy())drop.classList.add('drag-over');}));drop.addEventListener('dragleave',()=>drop.classList.remove('drag-over'));drop.addEventListener('drop',event=>{event.preventDefault();drop.classList.remove('drag-over');const files=event.dataTransfer?.files;if(files?.length>1){message('请每次导入一个文件；一个场景文件内可以包含多条场景。','warning');return;}importFile(files?.[0]);});
  document.addEventListener('dragover',event=>{if(Array.from(event.dataTransfer?.types||[]).includes('Files'))event.preventDefault();});document.addEventListener('drop',event=>{if(Array.from(event.dataTransfer?.types||[]).includes('Files'))event.preventDefault();});
  $('parse-paste-btn').addEventListener('click',()=>performImport($('paste-format').value,$('paste-content').value));
  $('resume-batch-btn').addEventListener('click',async()=>{if(!state.batchId||state.batchPolling||bridge.isBusy())return;state.batchPending=true;state.batchStartedAt=Date.now();bridge.setExternalBusy(true);$('resume-batch-btn').classList.add('hidden');$('cancel-batch-btn').classList.remove('hidden');try{await pollBatch();}catch(error){batchReadError(error);}finally{state.batchPolling=false;bridge.setExternalBusy(false);updateActions();}});
  $('cancel-batch-btn').addEventListener('click',async()=>{if(!state.batchId)return;const btn=$('cancel-batch-btn');btn.disabled=true;btn.textContent='正在停止…';try{await bridge.post('/api/runs/'+encodeURIComponent(state.batchId)+'/cancel',{});$('batch-progress-text').textContent='已请求停止；已完成场景将保留，可查看和导出。';if(!state.batchPolling&&!bridge.isBusy()){$('resume-batch-btn').click();}}catch(error){message(error.message,'error');btn.disabled=false;}finally{btn.textContent='停止批量';}});
  window.addEventListener('zhuiyin:busy',updateActions);updateActions();
  window.ZhuiyinUploads={showImport(data){showPage('import');state.data=data;showSource(data);if(data.kind==='scenarios')renderScenarios(data);else renderAnalyses(data);message('已读取历史导入','info');updateActions();},showBatch(data,runId,partial=false){showPage('import');state.batchId=runId;state.batchSource=data.source||{};state.batchPending=false;$('batch-results').classList.remove('hidden');renderBatch(data,partial);$('resume-batch-btn').classList.add('hidden');$('cancel-batch-btn').classList.add('hidden');updateActions();}};
  }
  window.addEventListener('zhuiyin:ready',initialize);
  document.addEventListener('DOMContentLoaded',initialize,{once:true});
  initialize();
})();
