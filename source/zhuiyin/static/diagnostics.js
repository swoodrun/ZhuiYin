/* Read-only diagnostics: every explanation links to saved evidence. */
(function(root,factory){
  'use strict';
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.ZhuiyinDiagnostics=api;
})(typeof window==='object'?window:null,function(){
  'use strict';
  const faults={commit_response_lost:'提交后响应丢失',stale_read:'读取过期状态',truncate_response:'响应内容缺失',latency:'工具调用延迟',precommit_timeout:'写入前超时',ack_without_commit:'未提交却返回成功',corrupt_payload:'业务内容损坏'};
  const strategies={baseline:'原始执行',fixed:'幂等＋核验',verified:'幂等＋核验',idempotency_only:'仅幂等',verification_only:'仅核验'};
  const policyNames={neutral:'基础任务指令',recovery:'任务指令＋恢复建议',unknown:'指令未记录'};
  const recordStateNames={active:'有效',open:'未关闭',cancelled:'已取消',canceled:'已取消',closed:'已关闭',deleted:'已删除',pending:'待处理',inactive:'无效',completed:'已完成',done:'已完成',failed:'失败',unknown:'状态未知'};
  const suggestions={DUPLICATE_RESERVATION:'核对重复写入是否使用同一请求编号，并检查服务端幂等事务；普通查询为空不能单独保证重试安全。',DUPLICATE_TICKET:'核对重复写入是否使用同一请求编号，并检查服务端幂等事务；普通查询为空不能单独保证重试安全。',REPORT_MISMATCH:'对照最后的任务报告与已保存业务记录，确认报告依据；需要确认成功时补充适用的核验凭证。',PAYLOAD_MISMATCH:'逐项核对请求字段与落库字段，检查字段映射和默认值；检测到损坏不表示记录已经修复。',STOCK_INCONSISTENT:'核对初始库存、剩余库存与有效预约数量，检查扣减与写入是否在同一事务内。',NEGATIVE_STOCK:'检查库存更新的条件和事务约束，并用同一请求重复提交验证。',TASK_INCOMPLETE:'核对最终是否存在有效记录；结合提交结果和请求编号决定下一步取证或重试。',AGENT_DID_NOT_FINISH:'查看最后一个工具返回和停止原因，保留部分结果后另建实验检查终止条件。',MODEL_EXECUTION_ERROR:'检查本次连接、请求预算与执行状态；执行异常与业务判定分别处理。'};
  const object=value=>value&&typeof value==='object'&&!Array.isArray(value)?value:{};
  const list=value=>Array.isArray(value)?value:[];
  const norm=value=>typeof value==='string'?value.toLowerCase().replace(/[^a-z0-9]/g,''):'';
  const sensitiveNames=new Set(['apikey','key','authorization','proxyauthorization','password','passwd','accesstoken','refreshtoken','apptoken','authtoken','token','secret','clientsecret','modelconfig','cookie','setcookie','privatekey','credential','credentials']);
  const headerContexts=new Set(['headers','requestheaders','responseheaders','httpheaders','httprequestheaders','httpresponseheaders','inboundheaders','outboundheaders']);
  const sensitive=key=>sensitiveNames.has(norm(key))||['apikey','password','accesstoken','refreshtoken','clientsecret'].some(suffix=>norm(key).endsWith(suffix));
  function safe(value,context,depth=0){
    if(depth>24)return '层级过深，未展开';
    if(Array.isArray(value))return value.map(item=>safe(item,context,depth+1));
    if(value&&typeof value==='object'){
      const header=headerContexts.has(norm(context))&&Object.entries(value).some(([key,item])=>['name','key'].includes(norm(key))&&sensitive(item));
      return Object.fromEntries(Object.entries(value).filter(([key])=>!sensitive(key)&&!['sqlite_dump','sql_dump'].includes(key)&&!(header&&['value','values'].includes(norm(key)))).map(([key,item])=>[key,safe(item,key,depth+1)]));
    }
    return value;
  }
  function records(value){for(const key of ['business_records','reservations','work_orders','effects'])if(Array.isArray(object(value)[key]))return value[key].filter(item=>item&&typeof item==='object'&&!Array.isArray(item));return [];}
  const active=rows=>rows.filter(row=>!['cancelled','canceled','closed','deleted','inactive'].includes(String(row.status??'active').toLowerCase()));
  function eventFault(event){
    if(event.kind!=='fault')return null;
    const explicit=event.fault_id||event.fault;if(typeof explicit==='string'&&Object.hasOwn(faults,explicit))return explicit;
    const seen=object(event.observed),actual=object(event.actual),action=String(event.action||'');
    if(seen.code==='COMMIT_UNKNOWN'){if(actual.committed===true)return 'commit_response_lost';if(actual.committed===false)return 'precommit_timeout';}
    if(actual.committed===false&&seen.ok===true)return 'ack_without_commit';
    if(event.tool==='storage_boundary')return 'corrupt_payload';
    if(!records(seen).length&&records(actual).length&&['find_ticket','query_reservations'].includes(event.tool))return 'stale_read';
    if(Object.hasOwn(seen,'delay_ms')||action.includes('延迟'))return 'latency';
    if(['删除响应','缺失','截断'].some(text=>action.includes(text)))return 'truncate_response';
    return 'other_recorded_fault';
  }
  const isTrial=value=>value&&typeof value==='object'&&['actual_success','reported_outcome','report_assessment','failure_fingerprint','business_failed','static_evaluation'].some(key=>Object.hasOwn(value,key));
  function collect(value,path='report',label='本次执行',parentConfig={},staticOnly=false){
    value=object(value);const config={...object(parentConfig),...object(value.config)};
    for(const key of ['result','partial_result'])if(value[key]&&typeof value[key]==='object'&&!Array.isArray(value[key]))return collect(value[key],path+'.'+key,label,config,staticOnly);
    if(isTrial(value))return [{path,label,trial:value,config,staticOnly:staticOnly||Boolean(value.static_evaluation)}];
    if(value.baseline&&typeof value.baseline==='object')return ['baseline','fixed','verified'].filter(key=>value[key]&&typeof value[key]==='object').map(key=>({path:path+'.'+key,label:label==='本次执行'?strategies[key]:label+' · '+strategies[key],trial:value[key],config,staticOnly}));
    if(Array.isArray(value.samples))return value.samples.filter(sample=>sample&&typeof sample==='object').map((sample,index)=>({path:`${path}.samples[${index}]`,label:`第${sample.trial_index??index+1}轮 · ${sample.prompt_policy||'指令未记录'} · ${strategies[sample.strategy]||sample.strategy||'执行组'}`,trial:sample,config,staticOnly}));
    if(Array.isArray(value.cases))return value.cases.flatMap((caseValue,index)=>{
      const item=object(caseValue),source=`${path}.cases[${index}]`,name=String(item.name||item.case_id||`用例${index+1}`),cfg={...config,...object(item.config)};
      if(Object.keys(object(item.strategies)).length)return Object.entries(item.strategies).filter(([,trial])=>trial&&typeof trial==='object').map(([key,trial])=>({path:source+'.strategies.'+key,label:name+' · '+(strategies[key]||key),trial,config:cfg,staticOnly}));
      if(item.analysis&&typeof item.analysis==='object')return [{path:source+'.analysis',label:name+' · 静态核对',trial:item.analysis,config:cfg,staticOnly:true}];
      for(const key of ['result','partial_result','report'])if(item[key]&&typeof item[key]==='object'&&!Array.isArray(item[key]))return collect(item[key],source+'.'+key,name,cfg,staticOnly);
      return [{path:source,label:name,trial:item,config:cfg,staticOnly}];
    });
    if(value.analysis&&typeof value.analysis==='object')return collect(value.analysis,path+'.analysis',label+' · 静态核对',config,true);
    return [{path,label,trial:value,config,staticOnly}];
  }
  function diagnoseTrial(trial,config={},options={}){
    trial=object(trial);config=object(config);const effective={...trial,...object(trial.static_evaluation)},staticOnly=Boolean(options.staticOnly||trial.static_evaluation);
    const events=list(trial.events).filter(item=>item&&typeof item==='object'&&!Array.isArray(item)).map(item=>safe(item));
    const savedRecords=records(trial).length?records(trial):records(trial.final_state),rows=safe(savedRecords);
    const notRun=trial.skipped===true||['not_run','ready','duplicate','conflict','import_error'].includes(trial.status);
    const incomplete=!notRun&&(trial.completed===false||Boolean(trial.infra_error)||['infra_error','execution_error','cancelled','interrupted','error'].includes(trial.status)||list(effective.execution_violations).length>0);
    let outcome=effective.reported_outcome??null;if(outcome===null&&typeof effective.reported_success==='boolean')outcome=effective.reported_success?'success':'failure';
    const actual=staticOnly||notRun||incomplete?null:effective.actual_success??null;
    const executionStatus=notRun?'not_run':incomplete?'incomplete':trial.completed===true||['valid','completed'].includes(trial.status)||['success','failure','unknown'].includes(outcome)?'completed':'unknown';
    const status={business:staticOnly?'仅静态核对上传材料':notRun?'未运行':incomplete?'执行未完成，状态未确认':actual===true?'已实际完成':actual===false?'未实际完成':'实际状态未记录',report:notRun?'未运行':({success:'报告成功',failure:'报告失败',unknown:'模型报告无法确认'}[outcome]||'未提交或未记录报告'),assessment:({consistent:'与保存的判定一致',inconsistent:'与保存的判定不一致',uncertain:'模型主动无法确认',unreported:'未提交报告',unverifiable:'证据不足，无法核对'}[effective.report_assessment]||'未记录报告检查'),execution:({not_run:'未运行',incomplete:'未完成',completed:'执行已结束',unknown:'执行状态未记录'}[executionStatus])};
    const activated=[];let firstAnomaly=null;
    events.forEach((event,index)=>{const fault=eventFault(event);if(fault)activated.push({id:fault,label:faults[fault]||'其他已记录故障',event_index:index});if(firstAnomaly===null&&(fault||active(records(event.actual)).length>1))firstAnomaly=index;});
    const findings=[],known=new Set();
    for(const key of ['violations','business_violations','reporting_violations','execution_violations'])for(const raw of list(effective[key])){
      if(!raw||typeof raw!=='object')continue;const code=String(raw.code||'RECORDED_FINDING');if(known.has(code))continue;known.add(code);
      let indexes=[];if(code.startsWith('DUPLICATE_'))indexes=events.flatMap((event,index)=>active(records(event.actual)).length>1?[index]:[]);
      else if(['REPORT_MISMATCH','AGENT_DID_NOT_FINISH'].includes(code))indexes=events.flatMap((event,index)=>event.kind==='report'||event.tool==='finish_task'?[index]:[]);
      else if(['PAYLOAD_MISMATCH','STOCK_INCONSISTENT'].includes(code))indexes=activated.filter(item=>item.id==='corrupt_payload').map(item=>item.event_index);
      findings.push({code,message:String(raw.message||code),event_indexes:indexes,record_indexes:['AGENT_DID_NOT_FINISH','MODEL_EXECUTION_ERROR'].includes(code)?[]:rows.map((_,index)=>index),suggestion:suggestions[code]||'对照原始事件、业务约束与最终记录，补充能够独立核对的材料。'});
    }
    if(!notRun&&outcome==='unknown')findings.push({code:'UNCERTAIN_REPORT',message:'模型主动报告无法确认；是否合理需要适用的证据合同。',event_indexes:events.flatMap((event,index)=>event.kind==='report'||event.tool==='finish_task'?[index]:[]),record_indexes:[],suggestion:'检查是否已有完整且匹配当前请求的凭证；如材料不足，可在新实验中增加取证步骤。'});
    return {path:options.path||'report',label:options.label||'本次执行',scope:staticOnly?'static_import_only':'saved_execution',execution_status:executionStatus,actual_success:actual,reported_outcome:outcome,status,findings,events,records:rows,configured_faults:Array.isArray(config.faults)?config.faults.filter(item=>typeof item==='string').map(id=>({id,label:faults[id]||id})):null,activated_faults:activated,first_anomaly_index:firstAnomaly,note:'规则说明只描述保存材料；最早记录异常不等于根因。静态导入不代表独立读取外部业务系统。'};
  }
  function analyzeReport(value){
    const trials=collect(value).map(item=>diagnoseTrial(item.trial,item.config,item));
    const counts={total:trials.length,completed:0,incomplete:0,not_run:0,unknown:0};trials.forEach(trial=>counts[trial.execution_status]++);
    return {schema:'diagnostics-v1',trials,counts,note:'执行组分别展示，不合并不同模式或研究阶段的成功率。'};
  }
  let nextInstance=0;
  function displayTrialLabel(trial){
    // Translate presentation only; saved analysis labels remain unchanged.
    let label=String(trial.label||'本次执行');
    if(/\.samples\[\d+\]$/.test(trial.path||'')){
      const parts=label.split(' · ');
      if(parts.length>=3)parts[1]=policyNames[parts[1]]||(Object.values(policyNames).includes(parts[1])?parts[1]:'其他指令');
      label=parts.join(' · ');
    }
    return label.replace(/(^| · )原始执行$/,'$1原始');
  }
  function mount(host,value,options={}){
    if(!host||typeof host.replaceChildren!=='function')throw new TypeError('诊断容器不可用');
    const doc=host.ownerDocument||(typeof document==='object'?document:null);if(!doc)throw new TypeError('诊断页面不可用');
    const analysis=value?.schema==='diagnostics-v1'&&Array.isArray(value.trials)?value:analyzeReport(value),prefix='zyd-'+(++nextInstance);
    const node=(tag,text,cls)=>{const element=doc.createElement(tag);if(text!==undefined&&text!==null)element.textContent=String(text);if(cls)element.className=cls;return element;};
    const section=node('section',null,'diagnostics-workbench');section.setAttribute('aria-label',options.title||'诊断工作台');
    section.append(node('h2',options.title||'诊断工作台'),node('p','从结论定位保存的事件与业务记录，查看规则生成的检查建议。','diagnostics-intro'));
    const body=node('div',null,'diagnostics-body');
    if(analysis.trials.length>1){const controls=node('div',null,'diagnostics-selector'),label=node('label','查看执行组'),select=node('select');label.htmlFor=prefix+'-select';select.id=prefix+'-select';analysis.trials.forEach((trial,index)=>{const option=node('option',displayTrialLabel(trial));option.value=String(index);select.append(option);});controls.append(label,select,node('span',`${analysis.counts.total}组 · 已结束${analysis.counts.completed} · 未完成${analysis.counts.incomplete} · 未运行${analysis.counts.not_run}`,'diagnostics-muted'));select.addEventListener('change',()=>render(analysis.trials[Number(select.value)]));section.append(controls);}
    section.append(body,node('p',analysis.note,'diagnostics-muted'));host.replaceChildren(section);
    function render(trial){
      if(!trial){body.replaceChildren(node('p','没有可诊断的已保存记录。'));return;}
      body.replaceChildren();const targets=new Map();
      function targetButton(text,id){const button=node('button',text,'diagnostics-evidence-link');button.type='button';button.addEventListener('click',()=>{const element=targets.get(id);if(!element)return;for(const item of targets.values())item.classList.remove('diagnostics-highlight');element.classList.add('diagnostics-highlight');let parent=element;while(parent&&parent!==body){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}element.focus?.({preventScroll:true});element.scrollIntoView?.({behavior:'smooth',block:'center'});});return button;}
      const summary=node('dl',null,'diagnostics-summary');for(const [title,text] of [['实际业务',trial.status.business],['结果报告',trial.status.report],['报告检查',trial.status.assessment],['执行流程',trial.status.execution]]){const card=node('div');card.append(node('dt',title),node('dd',text));summary.append(card);}body.append(summary);
      const exposure=node('div',null,'diagnostics-exposure');exposure.append(node('h3','故障配置与实际触发'));
      exposure.append(node('p','配置：'+(trial.configured_faults===null?'未记录':trial.configured_faults.length?trial.configured_faults.map(item=>item.label).join('、'):'无故障')));
      if(trial.activated_faults.length){const links=node('div',null,'diagnostics-links');links.append(node('span','保存的触发事件：'));trial.activated_faults.forEach(item=>links.append(targetButton(item.label+' · 第'+(item.event_index+1)+'条事件','event-'+item.event_index)));exposure.append(links);}
      else exposure.append(node('p',trial.events.length?'保存轨迹中未记录故障触发；仅配置故障不能证明实际触发。':'未保存可核对的触发轨迹。','diagnostics-muted'));
      if(trial.first_anomaly_index!==null)exposure.append(targetButton('定位最早记录的异常 · 第'+(trial.first_anomaly_index+1)+'条事件','event-'+trial.first_anomaly_index));body.append(exposure);
      const findings=node('div',null,'diagnostics-findings');findings.append(node('h3','检查发现与下一步'));
      if(!trial.findings.length)findings.append(node('p',trial.execution_status==='not_run'?'本组未运行，没有可判断的业务结果。':trial.execution_status==='incomplete'?'执行未完成，请结合停止原因查看已保存部分。':'保存材料未列出检查发现；实际状态、报告和检查范围见上方。','diagnostics-muted'));
      for(const finding of trial.findings){const card=node('article',null,'diagnostics-finding');card.append(node('h4',finding.message),node('p',finding.suggestion));const links=node('div',null,'diagnostics-links');finding.event_indexes.forEach(index=>links.append(targetButton('定位事件 '+(index+1),'event-'+index)));finding.record_indexes.forEach(index=>links.append(targetButton('定位记录 '+(index+1),'record-'+index)));if(!finding.event_indexes.length)links.append(node('span','未保存可直接定位的相关事件。','diagnostics-muted'));card.append(links);findings.append(card);}body.append(findings);
      const evidence=node('div',null,'diagnostics-evidence'),timeline=node('section',null,'diagnostics-timeline');timeline.append(node('h3','操作轨迹'));
      if(!trial.events.length)timeline.append(node('p','这份材料没有保存原始操作轨迹。','diagnostics-muted'));
      trial.events.forEach((event,index)=>{const detail=node('details',null,'diagnostics-event');detail.id=prefix+'-event-'+index;detail.tabIndex=-1;targets.set('event-'+index,detail);detail.append(node('summary',`事件 ${index+1}${event.step!==undefined?' · 原步骤 '+event.step:''} · ${event.action||event.tool||'操作未记录'}`));const grid=node('div',null,'diagnostics-event-columns');for(const [title,key] of [['模型可见返回','observed'],['保存的实际状态','actual']]){const col=node('div');col.append(node('h4',title),node('pre',Object.hasOwn(event,key)&&event[key]!==null?JSON.stringify(event[key],null,2):'未记录'));grid.append(col);}detail.append(grid);if(event.effect)detail.append(node('p','事件说明：'+String(event.effect)));const raw=node('details',null,'diagnostics-raw');raw.append(node('summary','查看完整保存事件'));let rendered=false;raw.addEventListener('toggle',()=>{if(raw.open&&!rendered){rendered=true;raw.append(node('pre',JSON.stringify(event,null,2)));}});detail.append(raw);timeline.append(detail);});evidence.append(timeline);
      const recordPanel=node('section',null,'diagnostics-records');recordPanel.append(node('h3','保存的最终业务记录'));
      if(!trial.records.length)recordPanel.append(node('p','未保存记录或记录列表为空；是否完成需结合记录完整性与判定结果确认。','diagnostics-muted'));
      trial.records.forEach((record,index)=>{const detail=node('details',null,'diagnostics-record');detail.id=prefix+'-record-'+index;detail.tabIndex=-1;targets.set('record-'+index,detail);detail.append(node('summary',`记录 ${index+1} · 编号 ${record.id??record.effect_id??'未记录'} · ${recordStateNames[record.status]||(record.status?'其他状态':'状态未记录')}`),node('pre',JSON.stringify(record,null,2)));recordPanel.append(detail);});evidence.append(recordPanel);body.append(evidence,node('p',trial.note,'diagnostics-muted'));
    }
    render(analysis.trials[Math.min(Math.max(0,Number(options.selectedIndex)||0),analysis.trials.length-1)]);
    return analysis;
  }
  return {analyzeReport,analyze_report:analyzeReport,diagnoseTrial,eventFault,mount};
});
