/* Pure presentation of deletion outcomes, shared by the page and regressions. */
(function(root,factory){
  'use strict';
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.ZhuiyinOutcomes=api;
})(typeof window==='object'?window:null,function(){
  'use strict';
  const count=value=>Number.isInteger(value)&&value>=0?value:null;
  function describe(step={},target){
    const counts={same_error:count(step.same_error_count??step.outcome_counts?.same_error),other_error:count(step.other_error_count??step.outcome_counts?.other_error),no_error:count(step.no_error_count??step.outcome_counts?.no_error),infra_error:count(step.infra_error_count??step.outcome_counts?.infra_error)};
    if(Array.isArray(step.fingerprints)&&step.fingerprints.length&&step.fingerprints.every(value=>typeof value==='string')){
      const inferred={same_error:target?0:null,other_error:target?0:null,no_error:0,infra_error:0};
      step.fingerprints.forEach(fingerprint=>{if(fingerprint.split('|').some(code=>['MODEL_EXECUTION_ERROR','AGENT_DID_NOT_FINISH'].includes(code)))inferred.infra_error++;else if(!fingerprint)inferred.no_error++;else if(target){if(fingerprint===target)inferred.same_error++;else inferred.other_error++;}});
      Object.keys(counts).forEach(key=>{if(counts[key]===null)counts[key]=inferred[key];});
    }
    if(counts.same_error===null)counts.same_error=count(step.reproduced);
    const sameError=typeof step.same_error_reproduced==='boolean'?step.same_error_reproduced:counts.same_error!==null?counts.same_error>0:typeof step.failed==='boolean'?step.failed:null;
    const incomplete=step.completed===false||counts.infra_error>0;
    let businessOutcomeLabel='业务结果未知';
    if(counts.other_error>0)businessOutcomeLabel='出现其他业务错误';
    else if(counts.same_error>0)businessOutcomeLabel='同一错误已复现';
    else if(incomplete)businessOutcomeLabel='运行未完成';
    else if(counts.same_error===0&&counts.other_error===0&&counts.infra_error===0&&counts.no_error>0)businessOutcomeLabel='无业务错误';
    if(incomplete&&businessOutcomeLabel!=='运行未完成')businessOutcomeLabel+='；运行未完成';
    return {counts,sameError,sameErrorLabel:sameError===null?'未知':sameError?'已复现':'未复现',sameErrorKind:sameError===true?'failed':'warn',businessOutcomeLabel};
  }
  function trial(trial={}){
    if(trial.static_evaluation&&typeof trial.static_evaluation==='object')trial={...trial,...trial.static_evaluation};
    const modern=Object.hasOwn(trial,'reported_outcome')||Object.hasOwn(trial,'report_assessment');
    const notRun=trial.skipped===true||trial.status==='not_run';
    const outcome=modern?trial.reported_outcome:null;
    const assessment={consistent:'与证据一致',inconsistent:'与证据不一致',uncertain:'模型主动无法确认',unreported:'未提交',unverifiable:'证据不足，无法核对'};
    const actualLabel=notRun?'未运行':trial.actual_success===true?'是':trial.actual_success===false?'否':'未知';
    const reportLabel=notRun?'未运行':modern?({success:'成功',failure:'失败',unknown:'无法确认'}[outcome]||'未提交'):(trial.reported_success===true?'成功（原口径）':trial.reported_success===false?'未成功（原口径）':'未记录（原口径）');
    const businessLabel=notRun?'未运行':typeof trial.business_failed==='boolean'?(trial.business_failed?'有违规':'无违规'):modern?'证据不足，无法判断':'未拆分（原口径）';
    const assessmentLabel=notRun?'未运行':modern?(assessment[trial.report_assessment]||'未记录'):'未拆分（原口径）';
    let badgeLabel='未记录',badgeKind='warn';
    if(notRun)badgeLabel='未运行';
    else if(trial.completed===false||trial.infra_error){badgeLabel='未完成';}
    else if(modern){
      if(trial.business_failed===true){badgeLabel='业务违规';badgeKind='failed';}
      else if(trial.report_failed===true||trial.report_assessment==='inconsistent'){badgeLabel='报告不一致';badgeKind='failed';}
      else if(outcome==='unknown'||trial.report_assessment==='uncertain'){badgeLabel='报告无法确认';}
      else if(trial.report_assessment==='unverifiable'){badgeLabel='证据不足，无法核对';}
      else if(trial.failed===false&&trial.report_assessment==='consistent'){badgeLabel='检查一致';badgeKind='success';}
      else if(trial.failed===true){badgeLabel='业务违规';badgeKind='failed';}
    }else if(typeof trial.failed==='boolean'){badgeLabel=trial.failed?'失败（原口径）':'成功（原口径）';badgeKind=trial.failed?'failed':'success';}
    return {modern,notRun,actualLabel,reportLabel,businessLabel,assessmentLabel,badgeLabel,badgeKind};
  }
  function imported(analysis={}){
    const hasStatic=Boolean(analysis.static_evaluation&&typeof analysis.static_evaluation==='object');
    const source=analysis.source_evaluation&&typeof analysis.source_evaluation==='object'?analysis.source_evaluation:null;
    const scopes={quantity_and_expected_fields:'记录数量及期望字段',quantity_only:'仅记录数量（未覆盖全部字段）'};
    return {hasStatic,staticView:trial(analysis),sourceView:source?trial(source):null,
      sourceProtocol:source?.evaluation_protocol||'未记录',
      scopeLabel:scopes[analysis.static_evaluation?.scope]||'未记录',
      evidenceLabel:analysis.static_evaluation?.evidence_complete===true?'足够':analysis.static_evaluation?.evidence_complete===false?'不足':'未记录'};
  }
  return {describe,trial,imported};
});
