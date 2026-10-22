(() => {
  'use strict';
  if (window.__zhuiyinTestCenterBootstrapped) return;
  window.__zhuiyinTestCenterBootstrapped = true;
  const $ = id => document.getElementById(id);
  function initialize() {
    if (window.__zhuiyinTestCenterInitialized || document.readyState === 'loading' || !window.Zhuiyin || !$('page-testing')) return;
    window.__zhuiyinTestCenterInitialized = true;
    window.removeEventListener('zhuiyin:ready', initialize);
    const api = window.Zhuiyin;
    const STORE_KEY = 'zhuiyin.test-center.last-run.v1';
    const PAGE_SIZE = 25, MiB = 1024 * 1024;
    const strategyNames = {baseline:'原始', idempotency_only:'仅幂等', verification_only:'仅核验', verified:'幂等＋核验'};
    const strategies = Object.keys(strategyNames);
    const state = {files:[], plan:null, report:null, runId:null, preparing:false, reading:false, restoring:false, pending:false, cancelling:false, cancelRequested:false, previewPage:1, resultPage:1, revision:null};
    const list = value => Array.isArray(value) ? value : [];
    const text = value => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
    const num = value => Number.isFinite(Number(value)) ? Number(value) : 0;
    function node(tag, content, className) {const element = document.createElement(tag); if (content != null) element.textContent = text(content); if (className) element.className = className; return element;}
    function hidden(id, value) {$(id).classList.toggle('hidden', value);}
    function pill(label, kind='warn') {return node('span', label, 'status-pill ' + kind);}
    function message(content='', kind='info', focus=false) {const box=$('test-message'); box.className='import-message' + (content ? ' ' + kind : ' hidden'); box.textContent=content; if (focus && content) box.focus();}
    function status(label, kind='warn') {$('test-status').className='status-pill ' + kind; $('test-status').textContent=label;}
    function saveRunId(runId) {try {localStorage.setItem(STORE_KEY, runId);} catch { /* Reading and running remain available without browser storage. */ }}
    let rulesMode=false;
    function profile() {if(!rulesMode)return 'agent'; return document.querySelector('input[name="test-profile"]:checked')?.value === 'full' ? 'full' : 'quick';}
    function runnable(plan) {return list(plan?.cases || plan?.items).filter(item => item.status === 'ready' && ['scenario', 'trace'].includes(item.kind)).length;}
    function hasRecords(plan) {return list(plan?.cases || plan?.items).length > 0;}
    function updateActions() {
      const locked = state.preparing || state.reading || state.restoring || state.pending || api.isBusy();
      $('test-files').disabled=locked; $('test-preview-btn').disabled=locked || !state.files.length;
      $('test-preview-btn').textContent=state.preparing ? '正在预览…' : '重新预览';
      $('test-drop-zone').setAttribute('aria-disabled', String(locked)); $('test-drop-zone').setAttribute('aria-busy', String(state.preparing));
      document.querySelectorAll('input[name="test-profile"]').forEach(input => {input.disabled=locked;});
      $('test-start-btn').disabled=locked || !state.plan?.suite_id || !hasRecords(state.plan);
      $('test-start-btn').textContent=state.reading ? '正在测试…' : state.plan && !runnable(state.plan) && hasRecords(state.plan) ? '保存导入记录' : '开始全部测试';
      $('test-resume-btn').disabled=state.reading || state.restoring || api.isBusy();
      $('test-cancel-btn').disabled=state.cancelling || state.cancelRequested || !state.pending;
      $('test-cancel-btn').textContent=state.cancelling ? '正在请求停止…' : state.cancelRequested ? '已请求停止' : '停止测试';
      const count=runnable(state.plan);
      $('test-budget-summary').textContent=api.getBillingSummary?.()||'请在工作台设置本次预算。';
      $('test-start-note').textContent=state.pending ? '本次测试尚未结束；可停止或恢复读取' : state.plan ? count ? '将处理 ' + count + ' 条独立用例 · ' + (profile()==='agent' ? '真实 Agent · 四种防护共用请求上限' : profile()==='full' ? '完整规则自检' : '快速规则自检') : hasRecords(state.plan) ? '没有待运行的独立用例；可保存错误与跳过记录并导出' : '没有测试内容，请检查文件' : '先选择文件并确认预览';
    }
    function origin(item) {const source=item.origin || {}; return [source.filename, source.member, source.index != null ? '第 ' + source.index + ' 项' : ''].filter(Boolean).join(' › ') || '来源未记录';}
    function caseName(item) {const box=node('div'); box.append(node('strong', item.name || item.request_id || item.case_id || '未命名用例', 'test-case-name'), node('span', origin(item), 'test-case-source')); if (item.request_id) box.append(node('span', item.request_id, 'test-case-identity')); return box;}
    function caseKind(item) {return {scenario:'场景', trace:'静态日志', import_error:'导入错误'}[item.kind] || '未知类型';}
    function caseStatus(item) {
      const labels={ready:'待运行', duplicate:'重复 · 跳过', conflict:'请求冲突 · 跳过', import_error:'导入错误', running:'运行中', completed:'执行完成', partial:'部分完成', execution_error:'执行异常', not_run:'未运行', cancelled:'已停止'};
      return pill(labels[item.status] || '状态未记录', ['import_error','execution_error'].includes(item.status) ? 'failed' : item.status === 'running' ? 'running' : 'warn');
    }
    function countCards(id, counts, result=false) {
      const fields = result ? [['处理总数', counts.cases], ['场景记录', counts.scenarios], ['日志记录', counts.traces], ['重复', counts.duplicates], ['导入错误', counts.import_errors], ['请求冲突', counts.conflicts]] : [['独立待运行', runnable(state.plan)], ['场景记录', counts.scenarios], ['日志记录', counts.traces], ['重复', counts.duplicates], ['导入错误', counts.import_errors], ['请求冲突', counts.conflicts]];
      $(id).replaceChildren(...fields.map(([label,value]) => {const box=node('div'); box.append(node('span', label), node('strong', value == null ? '—' : value)); return box;}));
    }
    function table(headers, rows, label) {const wrap=node('div', null, 'table-wrap'), element=node('table', null, 'data-table test-table'), head=node('thead'), heading=node('tr'), body=node('tbody'); element.setAttribute('aria-label',label); headers.forEach(label => {const th=node('th', label); th.setAttribute('scope','col'); heading.append(th);}); head.append(heading); element.append(head); rows.forEach(cells => {const row=node('tr'); cells.forEach(content => {const td=node('td'); if (content instanceof Node) td.append(content); else td.textContent=text(content); row.append(td);}); body.append(row);}); element.append(body); wrap.append(element); return wrap;}
    function paginate(id, page, count, action) {
      const host=$(id), pages=Math.max(1,Math.ceil(count/PAGE_SIZE)); host.replaceChildren();
      if (count <= PAGE_SIZE) return;
      const nav=node('nav', null, 'pagination'); nav.setAttribute('aria-label', id==='test-preview-pagination' ? '导入预览分页' : '测试结果分页');
      const previous=node('button','上一页'), next=node('button','下一页'), label=node('span','第 ' + page + ' / ' + pages + ' 页'); previous.type=next.type='button'; previous.disabled=page===1; next.disabled=page===pages; label.setAttribute('aria-live','polite');
      previous.addEventListener('click',() => action(page-1)); next.addEventListener('click',() => action(page+1)); nav.append(previous,label,next); host.append(nav);
    }
    function previewRows() {
      const items=list(state.plan?.cases || state.plan?.items), page=state.previewPage;
      const rows=items.slice((page-1)*PAGE_SIZE,page*PAGE_SIZE).map(item => [caseName(item),caseKind(item),caseStatus(item),item.duplicate_of ? '与 ' + item.duplicate_of + ' 相同；保留来源，跳过重复执行' : item.error || (item.kind==='trace' ? '只检查日志证据，不重放业务写入' : item.status==='conflict' ? '同一请求参数冲突，保留记录并跳过' : '按当前文件内容执行')]);
      $('test-preview-list').replaceChildren(rows.length ? table(['用例与来源','类型','导入状态','处理方式'],rows,'导入测试预览') : node('p','未发现可处理的测试内容。','empty-state test-empty'));
      paginate('test-preview-pagination',page,items.length,value => {state.previewPage=value; previewRows();});
    }
    function renderPreview(plan) {
      state.plan=plan; state.previewPage=1; hidden('test-preview',false);
      $('test-preview-details').open=false;
      $('test-preview-detail-label').textContent='查看逐项导入明细（' + list(plan.cases || plan.items).length + ' 条）';
      $('test-preview-version').textContent=(plan.workflow_version || '流程版本未记录') + ' · ' + list(plan.files).length + ' 个文件项';
      countCards('test-preview-counts',plan.counts || plan.summary || {});
      const warnings=list(plan.warnings), ignored=num((plan.counts || plan.summary || {}).ignored_files), box=$('test-preview-warnings'); box.replaceChildren();
      if (ignored) box.append(node('p','已忽略 ' + ignored + ' 个辅助文件项；不会自动转换为新用例。','test-help'));
      if (warnings.length) {const details=node('details',null,'source-detail'), entries=node('ul',null,'test-notice-list'); warnings.forEach(item => entries.append(node('li',typeof item==='string' ? item : item.message || text(item)))); details.append(node('summary','导入提醒（' + warnings.length + '）'),entries); box.append(details);}
      previewRows(); updateActions();
    }
    async function readFile(file) {
      if (/\.zip$/i.test(file.name)) {const base64=await new Promise((resolve,reject) => {const reader=new FileReader(); reader.onload=() => resolve(String(reader.result).split(',')[1]); reader.onerror=() => reject(new Error('无法读取文件：' + file.name)); reader.readAsDataURL(file);}); return {filename:file.name,content_base64:base64};}
      return {filename:file.name,content:await file.text()};
    }
    async function prepare(files=state.files) {
      if (state.preparing || state.reading || state.pending || api.isBusy()) return;
      const selected=Array.from(files || []); if (!selected.length) return;
      state.plan=null; state.files=selected; hidden('test-preview',true);
      $('test-selection-label').textContent=selected.length + ' 个文件 · ' + (selected.reduce((sum,file)=>sum+file.size,0)/MiB).toFixed(2) + ' MiB · ' + selected.slice(0,3).map(file=>file.name).join('、') + (selected.length>3 ? '…' : '');
      try {if (selected.length>200) throw new Error('最多一次选择 200 个文件；可将数据整理为一个 ZIP。'); if (selected.reduce((sum,file)=>sum+file.size,0)>20*MiB) throw new Error('上传文件合计不能超过 20 MiB。'); const invalid=selected.find(file=>!/\.(zip|json|csv|jsonl)$/i.test(file.name)); if (invalid) throw new Error('不支持的文件类型：' + invalid.name + '。请选择 ZIP、JSON、CSV 或 JSONL。'); const large=selected.find(file=>!/\.zip$/i.test(file.name)&&file.size>2*MiB); if (large) throw new Error('数据文件不能超过 2 MiB：' + large.name);}
      catch(error) {message(error.message,'error',true); updateActions(); return;}
      state.preparing=true; api.setExternalBusy(true); updateActions(); message('正在解析和静态检查文件；此步骤不执行场景。');
      try {const uploads=await Promise.all(selected.map(readFile)), plan=await api.post('/api/test-suites/preview',{files:uploads},60000); if (!plan.suite_id) throw new Error('服务没有返回预览编号，请重新预览。'); renderPreview(plan); message('预览已准备好。确认计数和处理方式后，点击“开始全部测试”。'); $('test-preview').focus();}
      catch(error) {message(error.message || '预览失败，请检查文件内容。','error',true);}
      finally {state.preparing=false; api.setExternalBusy(false); updateActions();}
    }
    function trialView(trial) {
      if (!trial) return {badgeLabel:'未运行',badgeKind:'warn',notRun:true};
      const evidence=trial.static_evaluation && typeof trial.static_evaluation==='object' ? {...trial,...trial.static_evaluation} : trial;
      const view=window.ZhuiyinOutcomes?.trial(evidence) || {badgeLabel:'结果未记录',badgeKind:'warn'};
      if (view.badgeKind==='success' && (evidence.reported_outcome==='unknown' || !Object.hasOwn(evidence,'reported_outcome') || evidence.status==='not_run' || evidence.skipped || evidence.completed===false)) return {...view,badgeLabel:evidence.reported_outcome==='unknown' ? '报告无法确认' : evidence.skipped || evidence.status==='not_run' ? '未运行' : '需检查记录',badgeKind:'warn'};
      return view;
    }
    function trials(item) {return item.kind==='trace' ? [item.analysis || {}] : strategies.map(key=>item.strategies?.[key]).filter(Boolean);}
    function flags(item) {
      const values=trials(item), views=values.map(trialView), duplicate=['duplicate','conflict'].includes(item.status);
      const unrunStrategy=item.kind==='scenario' && !duplicate && item.status!=='import_error' && strategies.some(key=>!item.strategies?.[key] || trialView(item.strategies[key]).notRun);
      const notRun=['not_run','cancelled','ready'].includes(item.status) || unrunStrategy;
      const failed=views.some(view=>view.badgeKind==='failed')||values.some(trial=>trial.template_contract?.status==='fail'), executionError=['execution_error','import_error'].includes(item.status), uncertain=values.some((trial,index)=>!views[index].notRun && (trial.template_contract?.status==='unknown' || trial.reported_outcome==='unknown' || ['uncertain','unverifiable','unreported'].includes(trial.report_assessment) || trial.completed===false || views[index].badgeKind==='warn'));
      return {failed,execution_error:executionError,uncertain,not_run:notRun,duplicate,attention:failed || executionError || uncertain || notRun || duplicate};
    }
    function strategyCell(item, key) {
      if (item.kind!=='scenario') {if (key!=='baseline') return node('span','—','test-unknown'); const cell=node('div'); if(item.kind==='trace' && !['not_run','duplicate','conflict','import_error'].includes(item.status)){const view=trialView(item.analysis);cell.append(pill(view.badgeLabel,view.badgeKind),node('small','静态证据检查'));} else cell.append(node('span','—','test-unknown')); return cell;}
      if (['duplicate','conflict','import_error','not_run'].includes(item.status)) return pill(item.status==='duplicate' ? '重复 · 未运行' : item.status==='conflict' ? '冲突 · 未运行' : '未运行');
      const trial=item.strategies?.[key], view=trialView(trial), cell=node('div'); cell.append(pill(view.badgeLabel,view.badgeKind)); if (trial && !view.notRun) cell.append(node('small','数据库实际完成：' + (view.actualLabel || '未知')),node('small','报告：' + (view.reportLabel || '未记录')));if(trial?.template_contract)cell.append(pill('模板规则：'+({pass:'符合',fail:'不符',unknown:'无法核对'}[trial.template_contract.status]||'未记录'),trial.template_contract.status==='fail'?'failed':trial.template_contract.status==='pass'?'success':'warn')); return cell;
    }
    function detail(item) {
      const details=node('details',null,'test-row-detail'); details.append(node('summary','查看记录'));
      details.addEventListener('toggle',() => {if(!details.open || details.dataset.rendered) return; details.dataset.rendered='true'; const body=node('div'); if(item.error) body.append(node('p',item.error,'test-help')); if(item.duplicate_of) body.append(node('p','与 ' + item.duplicate_of + ' 重复，未独立执行。','test-help'));
        if(item.kind==='scenario') strategies.forEach(key => {const trial=item.strategies?.[key]; if(!trial) return; const view=trialView(trial); body.append(node('h3',strategyNames[key])); [['执行检查',view.badgeLabel],['数据库实际完成',view.actualLabel],['业务检查',view.businessLabel],['任务报告',view.reportLabel],['报告核对',view.assessmentLabel],['真实写入',trial.stats?.physical_writes]].forEach(([label,value]) => {const row=node('div',null,'state-row'); row.append(node('span',label),node('strong',value??'未记录')); body.append(row);});if(trial.template_contract){const rules=node('details',null,'source-detail');rules.append(node('summary','模板规则检查：'+({pass:'符合规则',fail:'不符合规则',unknown:'证据不足'}[trial.template_contract.status]||'未记录')),node('pre',JSON.stringify(trial.template_contract,null,2)));body.append(rules);}});
        const raw=node('details',null,'source-detail'); raw.append(node('summary','来源与完整记录'),node('pre',JSON.stringify(item,null,2))); body.append(raw); details.append(body);
      }); return details;
    }
    function resultRows() {
      const query=$('test-search').value.trim().toLocaleLowerCase(), filter=$('test-filter').value, all=list(state.report?.cases || state.report?.items);
      const items=all.filter(item => {if(query && ![item.name,item.case_id,item.request_id,origin(item),item.error].map(text).join(' ').toLocaleLowerCase().includes(query)) return false; return filter==='all' || Boolean(flags(item)[filter]);});
      const pages=Math.max(1,Math.ceil(items.length/PAGE_SIZE)); state.resultPage=Math.min(state.resultPage,pages); const page=state.resultPage;
      $('test-filter-count').textContent=items.length + ' / ' + all.length + ' 条';
      const rows=items.slice((page-1)*PAGE_SIZE,page*PAGE_SIZE).map(item=>[caseName(item),node('div'),...strategies.map(key=>strategyCell(item,key)),detail(item)]);
      rows.forEach((row,index)=>{const item=items[(page-1)*PAGE_SIZE+index];row[1].append(caseStatus(item),node('small',caseKind(item)));});
      $('test-result-list').replaceChildren(rows.length ? table(['用例与来源','执行状态',...Object.values(strategyNames),'记录'],rows,'批量测试逐项结果') : node('p',all.length ? '没有符合筛选条件的记录。' : '等待第一批保存记录；无需停留在本页面。','empty-state test-empty'));
      paginate('test-result-pagination',page,items.length,value=>{state.resultPage=value; resultRows();});
    }
    function artifact(id, url) {const link=$(id); hidden(id,true); link.removeAttribute('href'); if(!url || !state.runId) return; try {const parsed=new URL(url,window.location.origin), prefix='/api/runs/' + encodeURIComponent(state.runId) + '/'; if(parsed.origin!==window.location.origin || !parsed.pathname.startsWith(prefix) || parsed.username || parsed.password) return; link.href=parsed.pathname + parsed.search; hidden(id,false);} catch { /* Ignore unsafe artifact addresses. */ }}
    function renderReport(report, partial=false) {
      state.report=report; hidden('test-results',(report.profile==='agent')===rulesMode); $('test-run-id').textContent='测试编号：' + (state.runId || '未记录'); $('test-run-profile').textContent=report.profile==='agent' ? '真实 Agent · '+(report.model_identity?.model||'模型未记录')+' · 已用 '+text(report.model_usage?.requests)+' / '+text(report.model_usage?.limit)+' 次请求 · '+(api.describeUsage?.(report.model_usage||{},report.run_budget||report.model_usage?.run_budget)||'费用信息见保存记录') : report.profile==='full' ? '完整规则自检 · 不调用模型' : '快速规则自检 · 不调用模型';
      const progress=report.progress || {}, completed=num(progress.completed), total=num(progress.total), percent=total>0 ? completed/total*100 : num(progress.percent); updateProgress(percent, total ? '已处理 ' + completed + ' / ' + total + ' 条 · 记录已自动保存' : '已保存当前记录');
      countCards('test-results-counts',report.counts || report.summary || {},true); resultRows();
      artifact('test-download-zip',report.artifacts?.bundle); artifact('test-download-csv',report.artifacts?.summary_csv);
      if(report.status==='completed' && !partial) {status('执行完成'); updateProgress(100,total ? '已处理 ' + completed + ' / ' + total + ' 条 · 全部记录已保存' : '全部记录已保存');}
      else if(report.status==='cancelled') status('已停止 · 记录已保留');
      else if(report.status==='running') status('运行中','running');
      else if(partial) status('执行未完成 · 记录已保留');
      window.ZhuiyinUI?.setLimitations?.('test-suite',['批量测试只覆盖本次导入内容及已有检查规则。日志采用静态证据检查，不重放业务写入。规则策略结果不能代替真实模型测试。']);
    }
    function updateProgress(percent, label) {const value=Math.min(100,Math.max(0,num(percent))); $('test-progress').style.width=value+'%'; $('test-progress-track').setAttribute('aria-valuenow',String(Math.round(value))); if(label) $('test-progress-text').textContent=label;}
    function readError(error) {status('读取中断 · 可恢复'); message((error.message || '读取中断。') + (state.runId ? ' 已保存的记录仍可查看；恢复读取不会重复运行测试。' : ' 请检查历史中是否已有本次任务后再重试。'),'error',true); hidden('test-resume-btn',!state.runId); hidden('test-cancel-btn',!state.pending);}
    async function poll() {
      if(state.reading || !state.runId) return; state.reading=true; let lastFull=0; const began=Date.now(); updateActions();
      while(state.runId) {
        if(Date.now()-began>30*60*1000) throw new Error('已持续读取 30 分钟，可稍后恢复读取。');
        const path='/api/runs/' + encodeURIComponent(state.runId), lite=await api.request(path+'?lite=1'), terminal=['done','error','interrupted','cancelled'].includes(lite.status);
        let percent=num(lite.progress); if(percent>0&&percent<=1) percent*=100; const event=list(lite.events).at(-1); updateProgress(percent,typeof event==='string' ? event : event?.message || '测试正在执行，结果持续保存。');
        const changed=lite.has_partial_result && lite.result_revision!==state.revision;
        if(terminal || changed || Date.now()-lastFull>=4000) {const job=await api.request(path); lastFull=Date.now(); state.revision=job.result_revision??lite.result_revision; const report=job.result || job.partial_result; if(report?.kind==='test_suite') renderReport(report,job.status!=='done' || report.status!=='completed');
          if(['done','error','interrupted','cancelled'].includes(job.status)) {state.pending=false; hidden('test-resume-btn',true); hidden('test-cancel-btn',true); if(!report || report.kind!=='test_suite') throw new Error('测试已结束，但服务没有返回可读取的批量记录。'); if(job.status==='done' && report.status==='completed') {status('执行完成'); message('测试流程已结束，全部记录已保存。可下载全部结果 ZIP 或汇总 CSV。');} else {status(report.status==='cancelled'?'已停止 · 记录已保留':'执行未完成 · 记录已保留'); message(job.error || report.error || '测试提前结束。已完成、失败、跳过及未运行记录均已保留，可查看和导出。','warning');} return;}
        }
        status(state.cancelRequested ? '正在停止 · 等待保存' : '运行中','running'); await new Promise(resolve=>setTimeout(resolve,850));
      }
    }
    async function start() {
      if(api.isBusy() || state.pending || !state.plan?.suite_id || !hasRecords(state.plan)) return;
      state.runId=null; state.report=null; state.revision=null; state.resultPage=1; state.cancelRequested=false; state.pending=true; api.setExternalBusy(true); hidden('test-results',false); hidden('test-resume-btn',true); hidden('test-cancel-btn',true); hidden('test-download-zip',true); hidden('test-download-csv',true); $('test-result-list').replaceChildren(node('p','正在启动测试…','empty-state')); $('test-result-pagination').replaceChildren(); $('test-results-counts').replaceChildren(); status('正在启动','running'); updateProgress(0,profile()==='agent'?'正在准备 Agent 任务；整批共用请求上限。':'正在准备规则自检；不会发送模型请求。'); $('test-results').focus(); updateActions(); message('正在启动 ' + runnable(state.plan) + ' 条独立用例。');
      try {const body={suite_id:state.plan.suite_id,profile:profile()};
        if(body.profile==='agent'){const settings=api.getBillingSettings();body.agent_config={model_config:api.getModelConfig(),prompt_policy:$('prompt-policy').value,max_requests:settings.max_requests,billing:settings.billing};}
        const job=await api.post('/api/test-suites/start',body); if(!job.run_id) throw new Error('服务没有返回测试编号。'); state.runId=job.run_id; saveRunId(state.runId); hidden('test-cancel-btn',false); $('test-run-id').textContent='测试编号：'+state.runId; await poll();}
      catch(error) {if(!state.runId) state.pending=false; readError(error);}
      finally {state.reading=false; api.setExternalBusy(false); updateActions(); window.ZhuiyinResearch?.refreshHistory?.();}
    }
    async function resume() {
      if(!state.runId || state.reading || state.restoring || api.isBusy()) return;
      state.pending=true; api.setExternalBusy(true); hidden('test-resume-btn',true); hidden('test-cancel-btn',false); message('正在恢复读取已保存任务，不会重新运行测试。');
      try {await poll();} catch(error) {readError(error);} finally {state.reading=false; api.setExternalBusy(false); updateActions(); window.ZhuiyinResearch?.refreshHistory?.();}
    }
    async function cancel() {
      if(!state.runId || !state.pending || state.cancelling || state.cancelRequested) return;
      state.cancelling=true; updateActions(); try {await api.post('/api/runs/'+encodeURIComponent(state.runId)+'/cancel',{}); state.cancelRequested=true; message('已请求停止。正在等待当前步骤完成保存；已完成和未运行的记录都会保留。','warning'); if(!state.reading && !api.isBusy()) await resume();} catch(error) {message(error.message || '停止请求未发送成功。','error',true);} finally {state.cancelling=false; updateActions();}
    }
    function showSuite(report,runId,partial=false) {
      if(state.reading || state.preparing) return false;
      state.runId=runId; state.pending=report.status==='running'; state.cancelRequested=false; state.revision=null; state.resultPage=1; renderReport(report,partial); hidden('test-resume-btn',!state.pending); hidden('test-cancel-btn',!state.pending); window.ZhuiyinUI?.showPage?.(report.profile==='agent'?'testing':'rule-batch'); message(state.pending ? '已读取运行中的测试。可恢复读取进度，或停止本次测试。' : '已读取保存的测试记录；没有重新运行测试。'); updateActions(); $('test-results').focus(); return true;
    }
    async function restore() {
      let runId; try {runId=localStorage.getItem(STORE_KEY);} catch {return;}
      if(!runId || typeof runId!=='string' || runId.length>160 || api.isBusy() || state.plan || state.runId) return;
      state.restoring=true; updateActions();
      try {const job=await api.request('/api/runs/'+encodeURIComponent(runId)), saved=job.result || job.partial_result; if(saved?.kind!=='test_suite' && job.kind!=='test_suite') return; const report=saved ? ['error','interrupted','cancelled'].includes(job.status) ? {...saved,status:job.status} : saved : {kind:'test_suite',status:job.status==='running'?'running':'interrupted',cases:[],counts:{},progress:{}}; state.runId=runId; state.pending=job.status==='running'; state.cancelRequested=false; renderReport(report,job.status!=='done'||report.status!=='completed'); hidden('test-resume-btn',!state.pending); hidden('test-cancel-btn',!state.pending); message((report.profile==='agent')===rulesMode ? '上次测试属于另一种模式；Agent 结果在批量测试查看，规则结果在使用方法中的批量规则检查查看。' : state.pending ? '已找回上次测试。恢复读取即可继续查看进度，无需再次选择文件或开始。' : '已找回上次测试结果；可直接查看与下载。');}
      catch(error) {message('上次测试暂时无法读取：' + error.message + ' 可在“历史”中查找已保存记录。','warning');}
      finally {state.restoring=false; updateActions();}
    }
    $('test-drop-zone').addEventListener('click',event=>{if(event.target!==$('test-files') && !$('test-files').disabled) $('test-files').click();});
    $('test-drop-zone').addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();if(!$('test-files').disabled) $('test-files').click();}});
    ['dragenter','dragover'].forEach(name=>$('test-drop-zone').addEventListener(name,event=>{event.preventDefault();if(!$('test-files').disabled) $('test-drop-zone').classList.add('drag-over');}));
    ['dragleave','drop'].forEach(name=>$('test-drop-zone').addEventListener(name,event=>{event.preventDefault();$('test-drop-zone').classList.remove('drag-over');}));
    $('test-drop-zone').addEventListener('drop',event=>{if(!$('test-files').disabled) prepare(event.dataTransfer?.files);});
    $('test-files').addEventListener('change',()=>{const files=Array.from($('test-files').files || []); $('test-files').value=''; if(files.length) prepare(files);});
    $('test-preview-btn').addEventListener('click',()=>prepare()); $('test-start-btn').addEventListener('click',start); $('test-resume-btn').addEventListener('click',resume); $('test-cancel-btn').addEventListener('click',cancel);
    document.querySelectorAll('input[name="test-profile"]').forEach(input=>input.addEventListener('change',updateActions));
    $('test-search').addEventListener('input',()=>{state.resultPage=1;resultRows();}); $('test-filter').addEventListener('change',()=>{state.resultPage=1;resultRows();});
    function describeMode(){
      $('test-mode-label').textContent=rulesMode?'固定规则 · 不调用模型':'真实模型 Agent';
      $('testing-title').textContent=rulesMode?'批量规则自检':'批量测试';
      $('test-agent-settings').hidden=rulesMode;$('test-rule-profiles').hidden=!rulesMode;
      $('test-mode-intro').textContent=rulesMode?'上传场景检查实验装置；使用固定程序，不是 Agent 效果测试。日志只做静态检查。':'上传多个任务，让内置 Agent 逐个执行；每个任务测试无额外防护、防重复提交、独立核验、两者同时启用四种方式。日志只做静态检查，不运行 Agent。';
      $('test-agent-context').textContent='被测 Agent：按每条任务选择内置设备借用 / 工单创建 Agent；模型：'+($('model').value.trim()||'尚未配置')+'；指令：'+($('prompt-policy').value==='recovery'?'任务指令＋恢复建议':'基础任务指令')+'。';
      if(state.report){const otherMode=(state.report.profile==='agent')===rulesMode;hidden('test-results',otherMode);if(otherMode&&!state.pending)message('上次记录属于另一种模式。Agent 结果在批量测试查看；规则结果在使用方法的批量规则检查中查看。');}
      updateActions();
    }
    window.addEventListener('zhuiyin:page',event=>{if(event.detail.page==='testing'||event.detail.page==='usage'){rulesMode=event.detail.batchMode==='rules';describeMode();}});
    window.addEventListener('zhuiyin:config',describeMode);window.addEventListener('zhuiyin:billing',()=>{$('test-budget-summary').textContent=api.getBillingSummary?.()||'请在工作台设置本次预算。';});
    rulesMode=['#rule-batch','#usage','#concurrency'].includes(window.location.hash);describeMode();
    window.addEventListener('zhuiyin:busy',updateActions); window.ZhuiyinTestCenter={showSuite,showPrepared(plan){if(api.isBusy()||state.pending||state.reading||state.preparing)throw new Error('请先等待当前测试完成或停止测试。');state.files=[];state.runId=null;state.report=null;state.pending=false;rulesMode=false;hidden('test-results',true);window.ZhuiyinUI?.showPage?.('testing');renderPreview(plan);$('test-selection-label').textContent='来自历史记录的任务重测 · '+(plan.retest_source?.unique_tasks??0)+' 个任务';message('已生成重测预览，尚未执行。确认工作台模型、指令与整批预算后，再点击开始。');updateActions();$('test-preview').focus();return true;}}; updateActions(); restore();
  }
  document.addEventListener('DOMContentLoaded',initialize); window.addEventListener('zhuiyin:ready',initialize); initialize();
})();
