/* Budget UI contracts: evaluate real UI functions with synthetic fields only. No API or dataset access. */
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(__dirname,'..'),read=name=>fs.readFileSync(path.join(root,'static',name),'utf8');
const app=read('app.js'),html=read('index.html'),research=read('research.js'),batch=read('testcenter.js');
const checks=[];
function check(name,body){body();checks.push({name,passed:true});}
function environment(supported=true){
  const fields={};for(const id of ['max-requests','billing-max-cost','billing-cache-hit-price','billing-cache-miss-price','billing-output-price','billing-settings-summary'])fields[id]={value:id==='max-requests'?'400':'',textContent:''};
  const events=[],context={$:id=>fields[id],display:value=>typeof value==='object'?JSON.stringify(value):String(value),window:{dispatchEvent:event=>events.push(event)},CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}}};
  const source=app.slice(app.indexOf('  let billingSupported='),app.indexOf('  function setMode('));
  vm.runInNewContext(source+'\nbillingSupported='+JSON.stringify(supported)+';\nthis.api={getBillingSettings,getBillingSummary,describeUsage,billingChanged};',context,{timeout:1000});
  return {fields,events,...context.api,set(id,value){fields[id].value=String(value);}};
}
check('one_editable_budget_form_and_three_distinct_prices',()=>{
  const form=html.slice(html.indexOf('<form id="experiment-form">'),html.indexOf('</form>',html.indexOf('<form id="experiment-form">')));
  for(const id of ['max-requests','billing-max-cost','billing-cache-hit-price','billing-cache-miss-price','billing-output-price']){assert.equal((html.match(new RegExp('id="'+id+'"','g'))||[]).length,1);assert(form.includes('id="'+id+'"'));}
  for(const removed of ['study-budget','test-agent-budget','study-input-price','study-output-price'])assert(!html.includes('id="'+removed+'"'));
  for(const id of ['study-budget-summary','test-budget-summary'])assert(html.includes('id="'+id+'"'));
  assert(html.includes('是否命中缓存由服务商决定'));assert(html.includes('不会按 0 费用处理'));assert(!form.includes('value="0.2"'));assert(html.includes('各自使用一份新预算'));
});
check('unconfigured_prices_remain_absent_not_zero',()=>{const e=environment(),settings=e.getBillingSettings();assert.equal(settings.max_requests,400);assert.deepEqual(JSON.parse(JSON.stringify(settings.billing)),{});assert(e.getBillingSummary().includes('未填单价，费用未知'));});
check('three_prices_and_amount_preserve_explicit_zero',()=>{const e=environment();e.set('max-requests',80);e.set('billing-max-cost','1.234567');e.set('billing-cache-hit-price','0');e.set('billing-cache-miss-price','0.8');e.set('billing-output-price','2.4');assert.deepEqual(JSON.parse(JSON.stringify(e.getBillingSettings())),{max_requests:80,billing:{max_cost_cny:1.234567,input_cache_hit_price_per_million:0,input_cache_miss_price_per_million:0.8,output_price_per_million:2.4,currency:'CNY'}});assert(e.getBillingSummary().includes('命中 0 / 未命中 0.8 / 输出 2.4'));});
check('amount_requires_miss_and_output_but_hit_can_be_unknown',()=>{const e=environment();e.set('billing-max-cost','10');assert.throws(()=>e.getBillingSettings(),/缓存未命中单价和输出单价/);e.set('billing-cache-miss-price','1');assert.throws(()=>e.getBillingSettings(),/缓存未命中单价和输出单价/);e.set('billing-output-price','2');const b=e.getBillingSettings().billing;assert(!Object.hasOwn(b,'input_cache_hit_price_per_million'));assert(e.getBillingSummary().includes('按未命中价保守计'));});
check('invalid_money_rejected_instead_of_coerced',()=>{for(const value of ['0','-1','1e-3','Infinity','NaN','1.1234567','<b>1</b>']){const e=environment();e.set('billing-max-cost',value);assert.throws(()=>e.getBillingSettings(),/费用保护额度/);}for(const value of ['-0.1','abc','1e2']){const e=environment();e.set('billing-output-price',value);assert.throws(()=>e.getBillingSettings(),/输出单价/);}for(const value of ['0','401','1.2','']){const e=environment();e.set('max-requests',value);assert.throws(()=>e.getBillingSettings(),/1—400/);}});
check('all_prices_and_amount_share_server_precision_and_upper_bound',()=>{for(const id of ['billing-max-cost','billing-cache-hit-price','billing-cache-miss-price','billing-output-price'])for(const value of ['1000000.000001','1000001','0.1234567']){const e=environment();e.set(id,value);assert.throws(()=>e.getBillingSettings(),/1000000|有效小数/);}const e=environment();e.set('billing-cache-hit-price','0.123456000');e.set('billing-cache-miss-price','1000000');e.set('billing-output-price','0');assert.equal(e.getBillingSettings().billing.input_cache_hit_price_per_million,0.123456);assert.equal(e.getBillingSettings().billing.input_cache_miss_price_per_million,1000000);});
check('fee_protection_explains_billing_limit_and_format',()=>{assert(html.includes('每次测试费用保护额度（元，可选）'));assert(html.includes('不能保证服务商账单绝对不超额'));assert(html.includes('最多 6 位有效小数'));assert(html.includes('末尾的 0 不计'));});
check('preview_can_read_invalid_setting_and_summarize_error_without_crashing',()=>{const e=environment();e.set('max-requests','401');assert.equal(e.getBillingSettings({validate:false}).max_requests,401);assert(e.getBillingSummary().includes('待完善'));e.billingChanged();assert.equal(e.events[0].type,'zhuiyin:billing');assert(e.fields['billing-settings-summary'].textContent.includes('待完善'));});
check('unknown_usage_cost_and_cache_do_not_become_zero',()=>{const text=environment().describeUsage({requests:2,prompt_tokens:40,completion_tokens:8,estimated_cost:null,usage_unknown_requests:1,cost_complete:false});assert(text.includes('估算费用未知'));assert(text.includes('缓存命中 未记录 / 未命中 未记录'));assert(text.includes('未知部分未按零计费'));assert(!text.includes('0.000000'));});
check('partial_cost_and_budget_reservation_are_distinct',()=>{const text=environment().describeUsage({requests:2,estimated_cost_cny:0.1,cost_complete:false,prompt_cache_hit_tokens:0,prompt_cache_miss_tokens:30},{max_cost_cny:1,accounted_cost_cny:0.2,reserved_cost_cny:0.1,remaining_cost_cny:0.8,assumption:'保守预留'});assert(text.includes('已知部分估算费用 0.100000 元'));assert(text.includes('缓存命中 0 / 未命中 30'));assert(text.includes('预算已计入 0.200000 元'));assert(text.includes('保守预留 0.100000 元'));assert(text.includes('剩余额度 0.800000 元'));});
check('all_llm_start_paths_use_workbench_settings_and_rule_path_bypasses_them',()=>{
  assert(app.includes('max_requests:forceRules?400:getBillingSettings().max_requests'));assert(app.includes('body.billing=getBillingSettings().billing'));
  assert(app.includes("post('/api/check-model',{model_config:cfg,billing:settings.billing},60000)"));
  assert(batch.includes('const settings=api.getBillingSettings();body.agent_config='));assert(batch.includes('max_requests:settings.max_requests,billing:settings.billing'));
  assert(research.includes('max_requests:api.getBillingSettings().max_requests'));assert(!research.includes("$('study-budget')"));assert(!batch.includes("$('test-agent-budget')"));assert(!research.includes("$('study-order').value=orderMode"));
  assert(research.includes("window.addEventListener('zhuiyin:billing',updateDesignNote)"));assert(batch.includes("window.addEventListener('zhuiyin:billing'"));
  assert(!app.includes("'max-requests':scenario.max_requests"));
});
check('missing_backend_capability_blocks_nonempty_billing_but_preserves_legacy_requests',()=>{const e=environment(false);assert.deepEqual(JSON.parse(JSON.stringify(e.getBillingSettings().billing)),{});e.set('billing-output-price','1');assert.throws(()=>e.getBillingSettings(),/新版预算计费.*重启/);assert.equal(e.getBillingSettings({validate:false}).billing.output_price_per_million,1);e.set('billing-max-cost','1');e.set('billing-cache-miss-price','1');assert.throws(()=>e.getBillingSettings(),/新版预算计费/);assert(app.includes("billingSupported=data.workbench_billing==='run-billing-v1'"));assert(app.includes("catch(error){billingSupported=false;billingChanged();"));});
check('study_options_execute_with_same_workbench_request_limit',()=>{
  const selected=[{value:'baseline'},{value:'verified'}],policies=[{value:'neutral'}],ids={'study-form':{reportValidity:()=>true},'study-repetitions':{value:'5'},'study-order':{value:'balanced'}};
  const context={$:id=>ids[id],document:{querySelectorAll:selector=>selector.includes('study_strategy')?selected:policies},api:{getBillingSettings:()=>({max_requests:37,billing:{max_cost_cny:1}})}};
  const source=research.slice(research.indexOf('    function studyOptions(){'),research.indexOf('    async function startStudy('));vm.runInNewContext(source+'\nthis.options=studyOptions();',context,{timeout:1000});assert.equal(context.options.max_requests,37);assert.equal(context.options.repetitions,5);assert(!Object.hasOwn(context.options,'input_price_per_million'));
});
(async()=>{
  // Reuse the established synthetic DOM, then execute the real Agent batch start path.
  const harnessFile=fs.readFileSync(path.join(__dirname,'check_testcenter.js'),'utf8');
  const harnessSource=harnessFile.slice(0,harnessFile.indexOf('const checks=[];')).replace("hash:'#rule-batch'","hash:'#testing'");
  const sandbox={require,__dirname,Buffer,URL,setTimeout,clearTimeout,setImmediate,console};
  vm.runInNewContext(harnessSource+'\nthis.harness={environment,fixture,drain};',sandbox,{timeout:1000});
  const {environment:batchEnvironment,fixture,drain}=sandbox.harness;
  const plan={suite_id:'synthetic-billing-suite',cases:[{case_id:'synthetic-ready',name:'Synthetic only',kind:'scenario',status:'ready',origin:{filename:'synthetic.json'}}],counts:{cases:1,scenarios:1,ready:1},files:[]};
  const report={...fixture(8),profile:'agent',model_usage:{requests:1}};
  const billing=environment();billing.set('max-requests','37');billing.set('billing-max-cost','1');billing.set('billing-cache-hit-price','0');billing.set('billing-cache-miss-price','1');billing.set('billing-output-price','2');
  const env=batchEnvironment({post:url=>url.endsWith('/preview')?plan:{run_id:'synthetic-run'},request:()=>({kind:'test_suite',status:'done',result:report,progress:1})});
  Object.assign(env.api,{getBillingSettings:billing.getBillingSettings,getBillingSummary:billing.getBillingSummary,getModelConfig:()=>({base_url:'https://synthetic.invalid',model:'synthetic-model',api_key:'SYNTHETIC_ONLY'}),describeUsage:billing.describeUsage});
  env.$('test-files').files=[{name:'synthetic.json',size:32,text:async()=>'{"synthetic":true}'}];await env.$('test-files').emit('change');await drain();await env.$('test-start-btn').click();await drain();
  const starts=env.calls.filter(call=>call.url.endsWith('/start'));assert.equal(starts.length,1);assert.equal(starts[0].body.profile,'agent');assert.equal(starts[0].body.agent_config.max_requests,37);assert.equal(starts[0].body.agent_config.billing.max_cost_cny,1);assert.equal(starts[0].body.agent_config.billing.input_cache_hit_price_per_million,0);assert(!Object.hasOwn(starts[0].body.agent_config,'equipment'));assert(!Object.hasOwn(starts[0].body.agent_config,'quantity'));checks.push({name:'real_agent_batch_start_uses_workbench_budget_without_business_overwrite',passed:true});
  console.log(JSON.stringify({passed:true,checks,scope:'Synthetic workbench fields and real UI helpers/batch start only; no credentials, datasets, server, or model calls.'},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
