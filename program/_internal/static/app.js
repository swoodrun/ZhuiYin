(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const labels = {commit_response_lost:'提交后响应丢失',stale_read:'读取过期状态',truncate_response:'响应内容缺失',latency:'工具调用延迟',precommit_timeout:'写入前超时',ack_without_commit:'未提交却返回成功',corrupt_payload:'业务内容损坏'};
  const strategyLabels = {baseline:'原始',idempotency_only:'仅幂等',verification_only:'仅核验',verified:'幂等＋核验'};
  const defaultFaults=['commit_response_lost','stale_read','truncate_response','latency'];
  const phaseLabels = {baseline:'复现业务错误',minimize:'精简故障组合',minimization:'精简故障组合',minimizing:'精简故障组合',fixed:'验证幂等＋核验',regression:'检查其他故障组合与正常流程',ablation:'运行四组对比',expanded_regression:'检查内置用例',done:'实验完成',running:'实验正在执行',starting:'准备实验环境'};
  let currentRun = null, result = null, polling = false, traceMode = 'baseline', startedAt = 0, singleBusy = false, externalBusy = false;
  const lazyPanels=new Map(), traceExpanded={baseline:false,fixed:false};
  const detailIds=['comparison-panel','ablation','counterexample','execution','regression'];
  let ruleCheckRunning=false;
  function focusResults(){
    const rules=(result?.mode||result?.config?.agent_mode)==='rules'||(!result&&ruleCheckRunning);
    const overview=$('result-overview'),details=$('experiment-details');
    if(rules){$('workbench-return-footer').classList.add('hidden');$('rule-full-results').append(overview,details);$('usage-rules').open=true;window.ZhuiyinUI?.showPage?.('usage');overview.classList.remove('hidden');details.classList.remove('hidden');}
    else{$('page-workbench').insertBefore(overview,$('config-panel'));$('page-workbench').append(details,$('workbench-return-footer'));window.ZhuiyinUI?.focusResults?.();}
  }
  function showWorkbench(){window.ZhuiyinUI?.showPage?.('workbench');}
  function returnToWorkbench(){
    if(singleBusy||polling)return false;
    document.querySelectorAll('#result-overview details,#experiment-details details').forEach(panel=>panel.open=false);
    $('result-overview').classList.add('hidden');$('experiment-details').classList.add('hidden');$('workbench-return-footer').classList.add('hidden');
    $('page-workbench').classList.remove('has-results');$('config-panel').open=true;$('experiment-source').classList.add('hidden');
    if(result)hideError();showWorkbench();$('workbench-home-title').focus({preventScroll:true});return true;
  }
  function deferPanel(id,render){lazyPanels.set(id,render);if($(id)?.open)renderPanel(id);}
  function renderPanel(id){const render=lazyPanels.get(id);if(!render)return;lazyPanels.delete(id);render();}
  function paginate(container,items,renderPage){
    if(window.ZhuiyinUI?.pager)return window.ZhuiyinUI.pager({container,items,pageSize:10,renderPage});
    let page=1;const draw=()=>{renderPage(items.slice((page-1)*10,page*10),page);if(items.length<=10)return;const controls=node('div',null,'pagination'),previous=node('button','上一页','button button-light button-small'),next=node('button','下一页','button button-light button-small');previous.type=next.type='button';previous.disabled=page===1;next.disabled=page*10>=items.length;previous.addEventListener('click',()=>{page--;draw();});next.addEventListener('click',()=>{page++;draw();});controls.append(previous,node('span','第 '+page+' / '+Math.ceil(items.length/10)+' 页'),next);container.append(controls);};draw();
  }
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const display = value => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value,null,2) : String(value);
  const modeName = value => value==='llm'?'模型模式':value==='rules'?'规则模式':'模式未记录';
  const policyName = value => value==='neutral'?'基础任务指令':value==='recovery'?'任务指令＋恢复建议':'提示层未记录';
  function node(tag, text, className) { const element=document.createElement(tag); if(text!=null) element.textContent=text; if(className) element.className=className; return element; }
  function replace(id, children) { $(id).replaceChildren(...children); }
  function faultText(faults) { return (faults||[]).map(id=>labels[id]||id).join('、') || '无故障'; }
  function statusPill(text, kind) { return node('span',text,'status-pill '+(kind||'')); }
  function setBadge(id,text,kind) { $(id).className='status-pill '+(kind||''); $(id).textContent=text; }
  async function request(path, options={}, timeout=25000) {
    const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),timeout);
    try { const response=await fetch(path,{...options,signal:controller.signal}); let data; try { data=await response.json(); } catch { throw new Error('服务返回了无法读取的结果（HTTP '+response.status+'）。'); } if(!response.ok) throw new Error(typeof data.error==='string'?data.error:typeof data.detail==='string'?data.detail:'请求未成功（HTTP '+response.status+'）。'); return data; }
    catch(error) { if(error.name==='AbortError') throw new Error('服务响应超时。可以稍后恢复读取本次实验。'); if(error instanceof TypeError) throw new Error('无法连接实验服务，请确认本地服务仍在运行。'); throw error; }
    finally { clearTimeout(timer); }
  }
  const post = (path,body,timeout) => request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)},timeout);
  function showError(message,resumable=false) { $('error-text').textContent=message; $('error-box').classList.remove('hidden'); $('resume-btn').classList.toggle('hidden',!resumable); }
  function hideError() { $('error-box').classList.add('hidden'); }
  function refreshBusyControls() { const busy=singleBusy||externalBusy;document.querySelectorAll('[data-workbench-home]').forEach(button=>{button.disabled=singleBusy||polling;button.title=button.disabled?'实验正在运行，请等待结束；需要中止请使用停止实验。':'返回任务配置，已保存记录仍可在历史查看。';});$('run-btn').disabled=busy; $('control-btn').disabled=busy; $('reset-btn').disabled=busy; $('run-label').textContent=externalBusy?'其他任务运行中':singleBusy?'运行中':'运行 Agent 实验'; $('run-icon').className=busy?'spinner':''; $('run-icon').textContent=busy?'':'▶'; document.querySelectorAll('#experiment-form input,#experiment-form select').forEach(input=>input.disabled=busy);updateScenarioFields(); $('check-model-btn').disabled=busy;$('cancel-btn').classList.toggle('hidden',!singleBusy);$('cancel-btn').disabled=false;$('cancel-btn').textContent='停止实验';window.dispatchEvent(new CustomEvent('zhuiyin:busy',{detail:{busy}})); }
  function updateScenarioFields() {const ticket=$('scenario').value==='ticket',busy=singleBusy||externalBusy;$('equipment-fields').classList.toggle('hidden',ticket);$('ticket-fields').classList.toggle('hidden',!ticket);['equipment','stock','quantity'].forEach(id=>$(id).disabled=busy||ticket);['ticket-title','ticket-priority'].forEach(id=>$(id).disabled=busy||!ticket);}
  function setBusy(busy) { singleBusy=busy;refreshBusyControls(); }
  function countFaults() { $('fault-count').textContent=document.querySelectorAll('input[name="faults"]:checked').length+' 项已选'; }
  function modelConfig() { return {base_url:$('base-url').value.trim(),model:$('model').value.trim(),api_key:$('api-key').value.trim()}; }
  function validateModel(config) { if(!config.base_url||!config.model) throw new Error('请填写 API 地址和模型名称。'); let url; try{url=new URL(config.base_url);}catch{throw new Error('API 地址格式不正确，请填写完整的模型 API 基础地址。');}if(url.username||url.password||url.search||url.hash)throw new Error('请填写不含账号、查询参数的模型 API 基础地址。');if(url.protocol!=='https:'&&!(url.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(url.hostname)))throw new Error('模型地址需要 HTTPS；本机模型可使用 HTTP。');if(/[^\x20-\x7e]/.test(config.api_key||''))throw new Error('API 密钥包含不支持的字符，请重新输入。'); }
  let billingSupported=false;
  const billingIds=['max-requests','billing-max-cost','billing-cache-hit-price','billing-cache-miss-price','billing-output-price'];
  const billingFields=[['billing-max-cost','max_cost_cny','费用保护额度'],['billing-cache-hit-price','input_cache_hit_price_per_million','缓存命中单价'],['billing-cache-miss-price','input_cache_miss_price_per_million','缓存未命中单价'],['billing-output-price','output_price_per_million','输出单价']];
  function getBillingSettings(options={}) {
    const validate=options.validate!==false,max_requests=Number($('max-requests').value),billing={};
    if(validate&&(!Number.isInteger(max_requests)||max_requests<1||max_requests>400))throw new Error('请在工作台把每次测试请求上限填写为 1—400 的整数。');
    for(const[id,key,label]of billingFields){const raw=$(id).value.trim();if(raw==='')continue;const parsed=Number(raw),decimal=/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(raw),valid=decimal&&Number.isFinite(parsed)&&parsed>=0&&parsed<=1000000&&(!raw.includes('.')||raw.split('.')[1].replace(/0+$/,'').length<=6)&&(key!=='max_cost_cny'||parsed>0);if(!valid){if(validate)throw new Error('请在工作台检查'+label+'：'+(key==='max_cost_cny'?'须大于 0 且不超过 1000000，最多 6 位有效小数。':'须为 0—1000000 的十进制数字，最多 6 位有效小数。'));continue;}billing[key]=parsed;}
    if(validate&&billing.max_cost_cny!==undefined&&(billing.input_cache_miss_price_per_million===undefined||billing.output_price_per_million===undefined))throw new Error('启用费用保护额度后，请在工作台填写缓存未命中单价和输出单价。缓存命中单价可留空。');
    if(validate&&Object.keys(billing).length&&!billingSupported)throw new Error('当前服务尚未确认支持新版预算计费，请更新或重启追因服务，再刷新页面后开始测试。');
    if(Object.keys(billing).length)billing.currency='CNY';return {max_requests,billing};
  }
  function getBillingSummary(){
    try{const{max_requests,billing}=getBillingSettings(),parts=[max_requests+' 次请求',billing.max_cost_cny===undefined?'未设费用保护额度':'费用保护额度 '+billing.max_cost_cny+' 元'];if(billing.input_cache_hit_price_per_million===undefined&&billing.input_cache_miss_price_per_million===undefined&&billing.output_price_per_million===undefined)parts.push('未填单价，费用未知');else parts.push('单价（元 / 百万 token）：命中 '+(billing.input_cache_hit_price_per_million??(billing.input_cache_miss_price_per_million===undefined?'未填':'按未命中价保守计'))+' / 未命中 '+(billing.input_cache_miss_price_per_million??'未填')+' / 输出 '+(billing.output_price_per_million??'未填'));return parts.join('；')+'。';}catch(error){return '预算设置待完善：'+error.message;}
  }
  function billingChanged(){const summary=getBillingSummary();$('billing-settings-summary').textContent=summary;window.dispatchEvent(new CustomEvent('zhuiyin:billing',{detail:{summary}}));}
  const knownNumber=value=>typeof value==='number'&&Number.isFinite(value);
  const amount=value=>knownNumber(value)?value.toFixed(6)+' 元':'未知';
  function describeUsage(usage={},runBudget=null){
    const budget=runBudget||usage.run_budget,estimated=usage.estimated_cost_cny??usage.estimated_cost,parts=['模型请求 '+(usage.requests??'未记录')+' 次'];
    if(knownNumber(estimated))parts.push((usage.cost_complete===false?'已知部分估算费用 ':'估算费用 ')+amount(estimated));else parts.push('估算费用未知');
    if(knownNumber(usage.known_cost_cny)&&!knownNumber(estimated))parts.push('已知部分费用 '+amount(usage.known_cost_cny));
    parts.push('输入 token '+(usage.prompt_tokens??'未记录')+'（缓存命中 '+(usage.prompt_cache_hit_tokens??'未记录')+' / 未命中 '+(usage.prompt_cache_miss_tokens??'未记录')+'）','输出 token '+(usage.completion_tokens??'未记录'));
    if(usage.cost_complete===false||Number(usage.usage_unknown_requests)>0)parts.push('用量或计费信息不完整，未知部分未按零计费');
    if(budget&&typeof budget==='object'){if(knownNumber(budget.max_cost_cny))parts.push('本次费用保护额度 '+amount(budget.max_cost_cny),'预算已计入 '+amount(budget.accounted_cost_cny),'保守预留 '+amount(budget.reserved_cost_cny),'剩余额度 '+amount(budget.remaining_cost_cny));if(budget.assumption)parts.push('预算说明：'+display(budget.assumption));}
    return parts.join('；')+'。';
  }
  function setMode(useLLM) { $('use-llm').checked=Boolean(useLLM); }
  function config(forceRules=false) { const body={scenario:$('scenario').value,ticket_title:$('ticket-title').value.trim(),ticket_priority:$('ticket-priority').value,equipment:$('equipment').value.trim(),stock:Number($('stock').value),quantity:Number($('quantity').value),request_id:$('request-id').value.trim(),seed:Number($('seed').value),max_requests:forceRules?400:getBillingSettings().max_requests,faults:[...document.querySelectorAll('input[name="faults"]:checked')].map(input=>input.value),prompt_policy:$('prompt-policy').value,agent_mode:forceRules?'rules':'llm'}; if(!body.request_id||body.scenario==='equipment'&&!body.equipment)throw new Error('请填写业务请求 ID 与当前场景必需的参数。');if(body.scenario==='ticket'&&!body.ticket_title)throw new Error('请填写工单标题。');if(body.agent_mode==='llm'){body.model_config=modelConfig();validateModel(body.model_config);body.billing=getBillingSettings().billing;}const snapshot=window.ZhuiyinProduct?.getTemplateSnapshot?.();if(snapshot)body.template_snapshot=snapshot;return body; }
  function setProgress(state) {
    const phase=state.phase||'running';
    let progress=number(state.progress); if(progress>0&&progress<=1)progress*=100; if(state.status==='done')progress=100; $('progress-bar').style.width=Math.min(100,Math.max(0,progress))+'%';
    const completed=state.status==='done',failed=['error','interrupted'].includes(state.status);setBadge('run-status',completed?'已完成':failed?'执行未完成':'运行中',completed?'success':failed?'failed':'running');
    const last=(state.events||[]).at(-1); const eventMessage=typeof last==='string'?last:last?.message||last?.detail;
    $('progress-text').textContent=eventMessage||phaseLabels[phase]||'实验正在执行，请稍候。';
  }
  async function runExperiment(rulesCheck=false) {
    rulesCheck=rulesCheck===true;ruleCheckRunning=rulesCheck;
    if(polling||singleBusy||externalBusy)return; if(!rulesCheck&&!$('experiment-form').reportValidity())return;
    hideError(); let body; try{body=config(rulesCheck);}catch(error){showError(error.message);return;}
    setBusy(true); result=null; resetResults();$('metric-regression').closest('.metric-card').classList.toggle('hidden',!rulesCheck); $('export-btn').textContent='↓ 导出实验报告';$('export-btn').classList.add('disabled');$('export-btn').setAttribute('aria-disabled','true'); currentRun=null; startedAt=Date.now(); setProgress({status:'running',phase:'starting',progress:0});focusResults();
    try { const data=await post(rulesCheck?'/api/run':'/api/run-agent',body); if(!data.run_id)throw new Error('服务没有返回实验编号。');currentRun=data.run_id; await poll(); }
    catch(error){showError(error.message,Boolean(currentRun));setBadge('run-status','等待','warn');$('progress-text').textContent='还没拿到结果。'+(currentRun?'可以恢复读取，无需重复运行。':'请检查配置后重新运行。');}
    finally{polling=false;setBusy(false);}
  }
  function resetResults(){
    lazyPanels.clear();traceExpanded.baseline=traceExpanded.fixed=false;
    detailIds.forEach(id=>{if($(id)?.tagName==='DETAILS')$(id).open=false;});
    $('model-identity').classList.add('hidden');
    $('result-billing').classList.add('hidden');$('result-billing-summary').textContent='';$('result-billing-content').textContent='';$('model-identity').textContent='';$('result-mode').textContent='等待运行';$('result-provenance').classList.add('hidden');$('result-provenance').open=false;$('result-provenance-content').textContent='';$('minimal-target').textContent='';
    ['comparison-mode','ablation-mode','counterexample-mode','execution-mode','regression-mode'].forEach(id=>$(id).textContent='等待运行');
    ['metric-original','metric-minimal','metric-regression'].forEach(id=>$(id).textContent='—');
    $('metric-reduction').textContent='等待结果';$('metric-regression-note').textContent='规则回归';
    ['baseline','fixed'].forEach(prefix=>{setBadge(prefix+'-badge','等待','');['reservations','stock','writes','reported','actual','business-check','report-check'].forEach(key=>$(prefix+'-'+key).textContent='—');replace(prefix+'-violations',[node('p','等待检查','placeholder')]);});
    $('result-summary').className='result-summary';$('result-summary').lastElementChild.textContent='正在运行实验';
    $('minimal-status').textContent='等待结果';replace('minimal-faults',[node('span','等待结果','empty-tag')]);$('minimization-reason').textContent='';$('minimization-reason').classList.add('hidden');
    ['minimization-content','trace-content','regression-content','ablation-content'].forEach(id=>{$(id).className='empty-state compact';replace(id,[node('p','等待结果')]);});
    $('ablation-interpretation').textContent='';$('expanded-scope').textContent='?';$('expanded-regression-content').replaceChildren();$('artifact-links').classList.add('hidden');$('regression-status').textContent='规则模式';
  }
  async function poll() {
    polling=true;
    while(currentRun) {
      if(Date.now()-startedAt>15*60*1000)throw new Error('读取已达到 15 分钟，请使用“恢复读取本次实验”继续检查结果。');
      const path='/api/runs/'+encodeURIComponent(currentRun),state=await request(path+'?lite=1');setProgress(state);
      if(['done','error','interrupted'].includes(state.status)){
        const saved=await request(path);setProgress(saved);
        if(saved.status==='done'){if(!saved.result)throw new Error('实验已完成，但服务未返回结果。');result=saved.result;renderResult(result);return;}
        const partial=saved.result||saved.partial_result;
        if(partial?.baseline){result=partial;renderResult(result);setBadge('run-status','失败','failed');$('progress-text').textContent=display(saved.error||'实验中断，已保留部分结果。');return;}
        throw new Error(display(saved.error||'实验执行中断，请检查参数和模型连接。'));
      }
      await new Promise(resolve=>setTimeout(resolve,700));
    }
  }
  function renderTrial(trial,prefix) {
    const view=window.ZhuiyinOutcomes.trial(trial);
    const records=trial.business_records||trial.reservations||[],reservations=records.filter(record=>!['cancelled','canceled','closed','deleted'].includes(String(record.status).toLowerCase()));const ticket=(trial.scenario||result?.config?.scenario)==='ticket';
    $(prefix+'-reservations').previousElementSibling.textContent=ticket?'有效工单记录':'有效预约';$(prefix+'-stock').parentElement.classList.toggle('hidden',ticket);
    if(view.notRun){['reservations','stock','writes'].forEach(key=>$(prefix+'-'+key).textContent='—');['reported','actual','business-check','report-check'].forEach(key=>$(prefix+'-'+key).textContent='未运行');setBadge(prefix+'-badge','未运行','warn');replace(prefix+'-violations',[node('p',trial.stopped_reason==='request_budget_exhausted'?'请求预算不足，本组未执行。':trial.stopped_reason==='cancelled'?'实验已停止，本组未执行。':'本组未执行。','placeholder')]);return;}
    $(prefix+'-reservations').textContent=reservations.length+' 条';$(prefix+'-stock').textContent=display(trial.stock_remaining);$(prefix+'-writes').textContent=display(trial.stats?.physical_writes);
    $(prefix+'-reported').textContent=view.reportLabel;$(prefix+'-actual').textContent=view.actualLabel;$(prefix+'-business-check').textContent=view.businessLabel;$(prefix+'-report-check').textContent=view.assessmentLabel;
    setBadge(prefix+'-badge',view.badgeLabel,view.badgeKind);
    const violations=(trial.violations||[]).map(item=>node('p',item.message||item.code||display(item)));
    replace(prefix+'-violations',violations.length?violations:[node('p',view.modern?(trial.reported_outcome==='unknown'?'没有记录违规；报告结论仍无法确认。':trial.failed===false?'未发现违规；报告评价见上方。':'结果未完整检查'):(trial.failed===false?'检查通过（原口径）':'结果未完整检查'),trial.failed===false?'pass':'placeholder')]);
  }
  function renderResult(data) {
    window.dispatchEvent(new CustomEvent('zhuiyin:result',{detail:data}));
    const baseline=data.baseline||{},fixed=data.fixed||{},mini=data.minimization||{},reg=data.regression||{};
    ['ablation','regression'].forEach(id=>{$('rule-supplement').append($(id));$(id).classList.toggle('hidden',data[id]?.skipped===true);});
    $('metric-regression').closest('.metric-card').classList.toggle('hidden',data.execution_profile==='agent-only-v1');
    const mode=baseline.agent_mode||data.mode||data.config?.agent_mode,identity=data.model_identity,provenance=data.provenance;
    $('result-mode').textContent=mode==='llm'?'Agent 实验 · 模型模式':'规则检查 · 固定程序';
    $('comparison-mode').textContent=modeName(mode);
    $('counterexample-mode').textContent=modeName(mode);
    $('execution-mode').textContent=modeName((data[traceMode]||{}).agent_mode||mode);
    $('ablation-mode').textContent=modeName(data.ablation?.agent_mode||'rules');
    $('regression-mode').textContent=modeName(reg.agent_mode||'rules');
    const identityParts=[];
    if(mode==='llm')identityParts.push('模型：'+(identity?.model||'未记录'),'服务：'+(identity?.api_origin||identity?.origin||'未记录'));
    identityParts.push('程序版本：'+(provenance?.app_version||'未记录'));
    if(mode==='llm'&&provenance?.prompt_version)identityParts.push('提示词版本：'+provenance.prompt_version);
    identityParts.push('评价协议：'+(data.evaluation_protocol||provenance?.evaluation_protocol||'原口径（未记录）'),mode==='llm'?policyName(data.config?.prompt_policy):'不调用模型，不使用模型指令');
    $('model-identity').textContent=identityParts.join(' · ');$('model-identity').classList.remove('hidden');
    const usage=data.model_usage||{},runBudget=data.run_budget||usage.run_budget;$('result-billing').classList.toggle('hidden',mode!=='llm');if(mode==='llm'){$('result-billing-summary').textContent=describeUsage(usage,runBudget);$('result-billing-content').textContent=JSON.stringify({model_usage:usage,run_budget:runBudget??null},null,2);}
    const hasProvenance=provenance&&typeof provenance==='object'&&!Array.isArray(provenance);
    $('result-provenance').classList.toggle('hidden',!hasProvenance);$('result-provenance-content').textContent=hasProvenance?JSON.stringify(provenance,null,2):'';
    const original=number(mini.original_count??data.config?.faults?.length),final=number(mini.final_count??data.minimized_faults?.length),incomplete=mini.completed===false;
    $('metric-original').textContent=original;$('metric-minimal').textContent=baseline.failed&&mini.stable&&!incomplete?final:'—';
    $('metric-reduction').textContent=incomplete?'精简提前停止':!baseline.failed?'未复现错误':!mini.stable?'复现不稳定':original?'缩减 '+Math.round((original-final)/original*100)+'%':'无故障也出错';
    replace('metric-regression',[document.createTextNode(display(reg.fixed_failures)),node('span',' / '+display(reg.total))]);$('metric-regression-note').textContent='幂等＋核验 · 规则模式';
    const summary=$('result-summary');summary.className='result-summary '+(!incomplete&&baseline.failed?(fixed.failed===true?'is-failure':fixed.failed===false&&fixed.reported_outcome!=='unknown'?'is-success':''):'');
    let summaryText;
    if(fixed.completed===false&&!fixed.skipped)summaryText='幂等＋核验未完成，执行记录已保存';
    else if(incomplete)summaryText='精简提前停止，保留 '+final+' 项故障'+(fixed.skipped?'；幂等＋核验未执行':fixed.failed===false?'；幂等＋核验通过':fixed.failed===true?'；幂等＋核验仍失败':'');
    else if(fixed.skipped)summaryText='原始记录已保存，幂等＋核验未执行';
    else if(baseline.failed&&fixed.failed===false)summaryText=mini.stable?'已复现错误，精简至 '+final+' 项，幂等＋核验通过':'错误复现不稳定，幂等＋核验通过';
    else if(baseline.failed)summaryText=fixed.failed===true?'已复现错误，幂等＋核验仍失败':'已复现错误，等待幂等＋核验检查结果';
    else summaryText=baseline.failed===false?'本次未复现目标业务错误':'实验未完成，请检查执行记录';
    if(fixed.reported_outcome==='unknown')summaryText=summaryText.replaceAll('幂等＋核验通过','幂等＋核验未发现违规');
    if(baseline.reported_outcome==='unknown'||fixed.reported_outcome==='unknown')summaryText+='；'+(baseline.reported_outcome==='unknown'?'原始':'')+(baseline.reported_outcome==='unknown'&&fixed.reported_outcome==='unknown'?'及':'')+(fixed.reported_outcome==='unknown'?'幂等＋核验':'')+'报告无法确认，单独看业务检查';
    summary.lastElementChild.textContent=summaryText;
    deferPanel('comparison-panel',()=>{renderTrial(baseline,'baseline');renderTrial(fixed,'fixed');});
    deferPanel('counterexample',()=>{
      $('minimal-status').textContent=incomplete?'提前停止 · 当前保留的组合':!baseline.failed?'未复现错误':mini.stable?(mini.one_minimal?'每项都已检查':'重复结果一致'):'复现不稳定';
      replace('minimal-faults',(incomplete||baseline.failed&&mini.stable)&&(data.minimized_faults||[]).length?data.minimized_faults.map(id=>node('span',labels[id]||id,'fault-tag')):[node('span',incomplete?'无已保存组合':!baseline.failed?'未复现错误':!mini.stable?'复现不稳定':'无故障也出错','empty-tag')]);
      $('minimization-reason').textContent='';$('minimization-reason').classList.add('hidden');
      $('counterexample-title').removeAttribute('title');
      renderMinimization(mini,baseline.failed,baseline.failure_fingerprint);
    });
    deferPanel('execution',renderTrace);deferPanel('regression',()=>{renderRegression(reg);renderExpanded(data.expanded_regression);});deferPanel('ablation',()=>renderAblation(data.ablation));
    const limitations=(data.limitations||[]).map(item=>typeof item==='string'?item:display(item));
    if(mode==='llm'){
      const identityParts=[],origin=identity?.api_origin||identity?.origin;
      if(typeof identity?.model==='string'&&identity.model.trim())identityParts.push('本次实验模型：'+identity.model);
      if(typeof origin==='string'&&origin.trim())identityParts.push('来源：'+origin);
      if(identityParts.length)limitations.unshift(identityParts.join('；')+'。');
      limitations.push('四组对比和回归使用规则模式；模型模式需要单独验证。');
    }
    if(mini.reason)limitations.push(mini.reason);
    if(mini.one_minimal&&!incomplete)limitations.push('“每项都已检查”表示逐项删除后都无法保留同一错误；仍可能存在其他更小组合。');
    if(incomplete)limitations.push('故障精简提前停止；还有故障没有完成逐项删除检查。'+(mini.stopped_reason==='request_budget_exhausted'?'模型请求预算不足。':mini.stopped_reason==='cancelled'?'用户已停止实验。':''));
    if(data.ablation?.interpretation)limitations.push(data.ablation.interpretation);
    if(data.expanded_regression?.scope)limitations.push(data.expanded_regression.scope);
    window.ZhuiyinUI?.setLimitations?.('experiment',limitations.length?limitations:['只覆盖本次测试和内置检查规则。']);
    $('export-btn').href='/api/runs/'+encodeURIComponent(currentRun)+'/export';$('export-btn').classList.remove('disabled');$('export-btn').setAttribute('aria-disabled','false');$('artifact-links').classList.add('hidden');
    ['baseline','fixed'].forEach(name=>{const link=$(name+'-db'),url=data.artifacts?.[name];link.classList.toggle('hidden',!url);if(url)link.href=url;});if(data.artifacts?.baseline||data.artifacts?.fixed)$('artifact-links').classList.remove('hidden');
    if(mode)setMode(mode==='llm');focusResults();
  }
  function table(headers,rows,hint='') {
    const wrap=node('div',null,'table-wrap'),table=node('table',null,'data-table'),head=node('thead'),headerRow=node('tr');
    headers.forEach((text,index)=>{const th=node('th',text);if(index===0&&hint){const help=node('span','?','help-tip');help.title=hint;help.setAttribute('aria-label',hint);help.tabIndex=0;th.append(document.createTextNode(' '),help);}headerRow.append(th);});head.append(headerRow);table.append(head);
    const body=node('tbody');rows.forEach(cells=>{const row=node('tr');cells.forEach(cell=>{const td=node('td');if(cell instanceof Node)td.append(cell);else td.textContent=display(cell);row.append(td);});body.append(row);});table.append(body);wrap.append(table);return wrap;
  }
  function renderMinimization(mini,failed,baselineFingerprint) {
    const steps=mini.steps||[],content=$('minimization-content');content.className='';
    const target=mini.failure_fingerprint||baselineFingerprint;
    $('minimal-target').textContent='本次目标指纹：'+(target||'未记录');
    content.replaceChildren();
    if(mini.final_revalidation){const check=mini.final_revalidation;content.append(node('p','精简后独立复核：'+(check.completed===true?'已完成':check.completed===false?'未完成':'状态未记录')+' · 同一错误 '+display(check.same_error_count)+' / '+display(check.repeats)+' 次 · '+display(check.status),'subtle-label'));}
    if(Array.isArray(mini.initial_runs)||Array.isArray(mini.final_runs)){const evidence=node('details',null,'source-detail');evidence.append(node('summary','完整复现与末轮复核记录'));evidence.addEventListener('toggle',()=>{if(evidence.open&&!evidence.dataset.rendered){evidence.dataset.rendered='true';evidence.append(node('pre',JSON.stringify({target_fingerprint:target,initial_runs:mini.initial_runs??null,final_runs:mini.final_runs??null,final_revalidation:mini.final_revalidation??null},null,2)));}});content.append(evidence);}
    if(!steps.length){content.append(node('p',failed?'没有可删的故障':'未复现错误'));return;}
    const pages=node('div');content.append(pages);
    paginate(pages,steps,(page,pageIndex)=>{
      const rows=page.map((step,index)=>{
        const outcome=window.ZhuiyinOutcomes.describe(step,target),counts=outcome.counts;
        const details=node('details');details.append(node('summary',outcome.businessOutcomeLabel+' · 指纹与记录'),node('pre',JSON.stringify({target_fingerprint:target??null,observed_fingerprints:Array.isArray(step.fingerprints)?step.fingerprints:null,rerun_faults:step.candidate,outcome_counts:counts,attempted:step.attempted??null,completed:step.completed??null,remaining_faults:step.remaining,trial_evidence:step.trial_evidence??null},null,2)));
        return [(pageIndex-1)*10+index+1,node('span',step.removed_fault?(labels[step.removed_fault]||step.removed_fault):'未记录','candidate'),statusPill(outcome.sameErrorLabel,outcome.sameErrorKind),(counts.same_error??'未知')+' / '+(step.repeats??'未知'),counts.other_error??'未知',counts.no_error??'未知',counts.infra_error??'未知',step.accepted?'删除':step.completed===false?'保留 · 未完成':'保留',details];
      });
      pages.replaceChildren(table(['步骤','尝试删除的故障','同一错误','复现次数','其他错误','无错误','运行错误','处理','详情'],rows,'删除后重复出现相同指纹，才删去该故障。其他错误、无错误和运行错误分别计数。共检查 '+display(mini.trials)+' 次。'));
    });
  }
  function renderTrace() {
    if(!result)return;const trial=result[traceMode]||{},events=trial.events||[],content=$('trace-content');content.className='';
    $('execution-mode').textContent=modeName(trial.agent_mode||result.mode||result.config?.agent_mode);
    if(!events.length){content.className='empty-state compact';content.replaceChildren(node('p','无执行记录'));return;}
    const list=node('div',null,'trace-list'),visible=traceExpanded[traceMode]?events:events.slice(0,10);
    visible.forEach((event,index)=>{const item=node('details',null,'trace-item '+(String(event.kind||'').match(/error|fault|timeout/)?'error':'')),summary=node('summary');summary.append(node('span',String(event.step??index+1).padStart(2,'0'),'trace-step'),node('span',event.tool||'业务检查','trace-tool'),node('span',display(event.action||event.effect||'执行步骤')));item.append(summary);
      item.addEventListener('toggle',()=>{if(!item.open||item.dataset.rendered)return;item.dataset.rendered='true';const details=node('div',null,'trace-details'),dl=node('dl');[['助手看到',event.observed],['数据库实际',event.actual],['状态变化',event.effect],['事件类型',event.kind]].forEach(([key,value])=>{if(value!=null){dl.append(node('dt',key));const dd=node('dd');dd.append(node('pre',display(value)));dl.append(dd);}});details.append(dl);item.append(details);});list.append(item);
    });
    content.replaceChildren(list);
    if(events.length>10){const expand=node('button',traceExpanded[traceMode]?'收起为前 10 步':'展开全部 '+events.length+' 步','button button-light button-small');expand.type='button';expand.setAttribute('aria-expanded',String(traceExpanded[traceMode]));expand.addEventListener('click',()=>{traceExpanded[traceMode]=!traceExpanded[traceMode];renderTrace();});content.append(expand);}
    const businessRecords=trial.business_records||trial.reservations||[],records=node('details',null,'reservation-records');records.append(node('summary','业务记录 · '+businessRecords.length+' 条'));records.addEventListener('toggle',()=>{if(records.open&&!records.dataset.rendered){records.dataset.rendered='true';records.append(node('pre',JSON.stringify(businessRecords,null,2)));}});content.append(records);
  }
  function renderAblation(ablation) {
    const arms=ablation?.arms||[],content=$('ablation-content');content.className='';$('ablation-interpretation').textContent='';
    if(!arms.length){content.className='empty-state compact';content.replaceChildren(node('p','无对比数据'));return;}
    const rows=arms.map(arm=>{const trial=arm.result||{},records=trial.business_records||trial.reservations||[],active=records.filter(record=>!['cancelled','canceled','closed','deleted'].includes(String(record.status).toLowerCase())),counts=Object.create(null);active.forEach(record=>counts[record.request_id||'request']=(counts[record.request_id||'request']||0)+1);const duplicates=Object.values(counts).reduce((sum,count)=>sum+Math.max(0,count-1),0),mechanism=node('div');mechanism.append(node('strong',strategyLabels[arm.strategy]||arm.label||arm.strategy),node('small','幂等 '+(arm.idempotency?'开':'关')+' · 独立检查 '+(arm.verification?'开':'关'),'ablation-mechanism'));
      const details=node('details',null,'ablation-records');details.append(node('summary',records.length+' 条'));details.addEventListener('toggle',()=>{if(details.open&&!details.dataset.rendered){details.dataset.rendered='true';details.append(node('pre',JSON.stringify({records,violations:trial.violations||[]},null,2)));}});
      const calls=trial.stats?.verification_calls??(Array.isArray(trial.events)?trial.events.filter(event=>/verif|proof|receipt/.test(event.tool||'')).length:null);
      const view=window.ZhuiyinOutcomes.trial(trial);mechanism.append(node('small',policyName(arm.prompt_policy||trial.prompt_policy||result?.config?.prompt_policy),'ablation-mechanism'));
      return [mechanism,view.actualLabel,view.businessLabel,view.reportLabel,view.assessmentLabel,statusPill(view.badgeLabel,view.badgeKind),display(trial.stats?.physical_writes),duplicates,display(calls),details];});
    content.replaceChildren(table(['策略 / 提示层','数据库实际完成','业务检查','报告结论','报告核对','综合状态','真实写入','重复条数','检查调用','记录'],rows,'在同一提示层下比较幂等与独立检查能力，业务检查与报告结论分别显示。'));
    const verification=arms.find(arm=>arm.strategy==='verification_only')?.result,combined=arms.find(arm=>arm.strategy==='verified')?.result;
    if(verification&&combined&&typeof verification.failed==='boolean'&&typeof combined.failed==='boolean'&&typeof verification.actual_success==='boolean'&&typeof combined.actual_success==='boolean'&&typeof verification.failure_fingerprint==='string'&&typeof combined.failure_fingerprint==='string'){
      const equal=['failed','actual_success','failure_fingerprint'].every(key=>verification[key]===combined[key]);
      content.append(node('p',equal?'本次仅核验与幂等＋核验的检查结果、实际完成和错误指纹相同。':'本次仅核验与幂等＋核验的结果不同，可展开记录比较。','subtle-label'));
    }
  }
  function renderExpanded(expanded) {
    const content=$('expanded-regression-content');$('expanded-scope').textContent='?';$('expanded-scope').title='查看各用例在四组策略下的运行结果。';const cases=expanded?.cases||[];
    if(!cases.length){content.replaceChildren(node('p','无扩展数据'));return;}
    const strategies=['baseline','idempotency_only','verification_only','verified'],rows=cases.map(item=>[item.name,(item.scenario==='ticket'?'工单':'设备')+' · '+faultText(item.faults),...strategies.map(strategy=>{const trial=item.strategies?.[strategy];const view=trial?window.ZhuiyinOutcomes.trial(trial):null;return view?statusPill(view.badgeLabel,view.badgeKind):statusPill('未运行','warn');})]);
    content.replaceChildren(table(['用例','场景 / 故障','原始','仅幂等','仅核验','幂等＋核验'],rows,'每行对应一个用例，横向比较四组策略的结果。'));
  }
  function renderRegression(reg) {
    const cases=reg.cases||[],content=$('regression-content');content.className='';
    if(!cases.length){content.className='empty-state compact';content.replaceChildren(node('p','无回归数据'));return;}
    paginate(content,cases,(page,pageIndex)=>{const rows=page.map((item,index)=>[(pageIndex-1)*10+index+1,item.name||faultText(item.faults),statusPill(item.baseline_failed?'有违规':'未见违规',item.baseline_failed?'failed':'success'),statusPill(item.fixed_failed?'有违规':'未见违规',item.fixed_failed?'failed':'success')]);content.replaceChildren(table(['编号','故障条件','原始','幂等＋核验'],rows,'逐行比较规则裁判是否发现违规；未见违规不等于业务已经完成。'));});
    $('regression-status').textContent=display(reg.fixed_failures)+' / '+display(reg.total)+' 组失败';
  }
  $('experiment-form').addEventListener('submit',event=>{event.preventDefault();runExperiment();});
  document.querySelectorAll('[data-workbench-home]').forEach(button=>button.addEventListener('click',returnToWorkbench));
  $('control-btn').addEventListener('click',()=>{document.querySelectorAll('input[name="faults"]').forEach(input=>input.checked=false);countFaults();runExperiment();});
  $('reset-btn').addEventListener('click',()=>{ $('scenario').value='equipment';$('ticket-title').value='教学楼门禁故障';$('ticket-priority').value='normal';$('equipment').value='单反相机';$('stock').value='3';$('quantity').value='1';$('request-id').value='req-demo-001';$('seed').value='42';$('prompt-policy').value='neutral';document.querySelectorAll('input[name="faults"]').forEach(input=>input.checked=defaultFaults.includes(input.value));countFaults();updateScenarioFields();$('experiment-source').classList.add('hidden');billingChanged();});
  $('scenario').addEventListener('change',updateScenarioFields);
  document.querySelectorAll('input[name="faults"]').forEach(input=>input.addEventListener('change',countFaults));
  $('use-llm').addEventListener('change',()=>setMode($('use-llm').checked));
  $('trace-baseline').addEventListener('click',()=>selectTrace('baseline'));$('trace-fixed').addEventListener('click',()=>selectTrace('fixed'));
  function selectTrace(mode){traceMode=mode;['baseline','fixed'].forEach(name=>{const button=$('trace-'+name);button.classList.toggle('selected',name===mode);button.setAttribute('aria-pressed',String(name===mode));});if($('execution')?.open)renderTrace();else deferPanel('execution',renderTrace);}
  $('export-btn').addEventListener('click',event=>{if(!result)event.preventDefault();});
  $('resume-btn').addEventListener('click',async()=>{if(!currentRun||polling||singleBusy||externalBusy)return;hideError();setBusy(true);startedAt=Date.now();try{await poll();}catch(error){showError(error.message,true);}finally{polling=false;setBusy(false);}});
  $('cancel-btn').addEventListener('click',async()=>{if(!currentRun)return;const button=$('cancel-btn');button.disabled=true;button.textContent='正在停止…';try{await post('/api/runs/'+encodeURIComponent(currentRun)+'/cancel',{});$('progress-text').textContent='已请求停止；最多等待当前模型请求结束（约 35 秒）。';}catch(error){showError(error.message,true);button.disabled=false;button.textContent='重试停止';}});
  $('check-model-btn').addEventListener('click',async()=>{const button=$('check-model-btn'),output=$('model-check-result');output.className='connection-result';output.textContent='';try{const cfg=modelConfig();validateModel(cfg);button.disabled=true;button.textContent='正在测试连接…';const settings=getBillingSettings();const data=await post('/api/check-model',{model_config:cfg,billing:settings.billing},60000);const ok=data.ok!==false&&data.available!==false&&data.success!==false;output.className='connection-result '+(ok?'success':'error');output.textContent=(ok?'✓ ':'')+(data.message||data.error||(ok?'模型连接成功，可运行 Agent。':'模型连接未通过。'))+(data.model_usage?' '+describeUsage(data.model_usage,data.run_budget):'');}catch(error){output.className='connection-result error';output.textContent=error.message;}finally{button.disabled=false;button.textContent='测试模型连接';}});
  function applyScenario(scenario,label='') {
    if(singleBusy||externalBusy)return false;showWorkbench();
    const values={scenario:scenario.scenario??'equipment','ticket-title':scenario.ticket_title??'教学楼门禁故障','ticket-priority':scenario.ticket_priority??'normal',equipment:scenario.equipment??'单反相机',stock:scenario.stock??3,quantity:scenario.quantity??1,'request-id':scenario.request_id??'req-imported-001',seed:scenario.seed??42,'prompt-policy':['neutral','recovery'].includes(scenario.prompt_policy)?scenario.prompt_policy:'neutral'};
    Object.entries(values).forEach(([id,value])=>$(id).value=String(value));const faults=Array.isArray(scenario.faults)?scenario.faults:[];document.querySelectorAll('input[name="faults"]').forEach(input=>input.checked=faults.includes(input.value));countFaults();updateScenarioFields();$('use-llm').checked=true;setMode(true);hideError();
    $('experiment-source').textContent=label?'已载入：'+label:'已载入场景';$('experiment-source').classList.remove('hidden');window.dispatchEvent(new Event('zhuiyin:config'));return true;
  }
  window.Zhuiyin={getBillingSettings,getBillingSummary,describeUsage,getModelConfig(){const cfg=modelConfig();validateModel(cfg);return cfg;},applyScenario,runExperiment,runRuleCheck:()=>runExperiment(true),getConfig(options={}){const form=$('experiment-form'),rulesOnly=options?.rulesOnly===true,fields=rulesOnly?[...form.querySelectorAll('input,select')].filter(input=>!['base-url','model','api-key',...billingIds].includes(input.id)):null,invalid=fields?.find(input=>!input.checkValidity());if(rulesOnly?Boolean(invalid):!form.checkValidity()){showWorkbench();$('config-panel').open=true;if(rulesOnly)invalid.reportValidity();else form.reportValidity();throw new Error('请检查工作台参数。');}return config(rulesOnly);},isBusy:()=>singleBusy||externalBusy,request,post,showError,setExternalBusy(value){externalBusy=Boolean(value);refreshBusyControls();},showResult(data,runId,name='',context='batch') {if(singleBusy||externalBusy)return false;hideError();if(data.config)applyScenario(data.config,name);result=data;currentRun=runId;resetResults();renderResult(data);setProgress({status:'done',phase:'done',progress:100,events:[{message:'查看已保存的实验详情；未重新运行。'}]});$('export-btn').textContent=context==='batch'?'↓ 导出本批次报告':'↓ 导出历史报告';$('experiment-source').textContent=(context==='batch'?'批量记录':'历史记录')+(name?' · '+name:'');$('experiment-source').classList.remove('hidden');return true;}};
  ['ablation','regression'].forEach(id=>$('rule-supplement').append($(id)));
  detailIds.forEach(id=>$(id)?.addEventListener('toggle',()=>{if($(id).open)renderPanel(id);}));
  billingIds.forEach(id=>['input','change'].forEach(name=>$(id).addEventListener(name,billingChanged)));billingChanged();
  window.dispatchEvent(new Event('zhuiyin:ready'));
  async function health(){try{const data=await request('/api/health',{},8000);billingSupported=data.workbench_billing==='run-billing-v1';billingChanged();$('health-dot').className='dot';$('health-label').textContent='服务已就绪';}catch(error){billingSupported=false;billingChanged();$('health-dot').className='dot error';$('health-label').textContent='实验服务未连接';showError(error.message);}}
  health();
})();
