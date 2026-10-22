/* Offline DOM-contract checks. No browser rendering or network is performed. */
'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert'),crypto=require('crypto'),path=require('path');
const root=path.resolve(__dirname,'..'),html=fs.readFileSync(path.join(root,'答辩演示.html'),'utf8');
const dataMatch=html.match(/<script id="evidence" type="application\/json">([\s\S]*?)<\/script>/);
assert(dataMatch,'Embedded evidence exists');
const evidence=JSON.parse(dataMatch[1]);
const program=Array.from(html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)).map(x=>x[1]).at(-1);
const unescape=text=>text.replace(/&(?:amp|lt|gt|quot|#39);/g,x=>({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"}[x]));
let document;
class Element{
  constructor(tag,attrs={}){this.tagName=tag;this.attrs=attrs;this.children=[];this.text='';this.listeners={};this.dataset={};for(const [k,v]of Object.entries(attrs))if(k.startsWith('data-'))this.dataset[k.slice(5)]=v;this.hidden='hidden'in attrs;this.disabled='disabled'in attrs;this.tabIndex=Number(attrs.tabindex||0);}
  set innerHTML(value){this.html=value;const fragment=parse(value);this.children=fragment.children;this.text=fragment.text;}
  get innerHTML(){return this.html||'';}
  set textContent(value){this.text=String(value);this.children=[];}
  get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  setAttribute(key,value){this.attrs[key]=String(value);}
  getAttribute(key){return this.attrs[key]??null;}
  addEventListener(key,fn){this.listeners[key]=fn;}
  click(){if(!this.disabled){this.listeners.click?.({});this.onclick?.({});}}
  focus(){document.activeElement=this;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  querySelectorAll(selector){const matches=[];const test=node=>{if(selector[0]==='.')return(node.attrs.class||'').split(/\s+/).includes(selector.slice(1));const attribute=selector.match(/^\[([^=\]]+)(?:=['"]?([^'"\]]*)['"]?)?\]$/);if(attribute)return attribute[2]===undefined?attribute[1]in node.attrs:node.attrs[attribute[1]]===attribute[2];return node.tagName===selector;};const walk=node=>{for(const child of node.children){if(test(child))matches.push(child);walk(child);}};walk(this);return matches;}
}
function parse(source){const root=new Element('root'),stack=[root];for(const match of source.matchAll(/<!--[\s\S]*?-->|<![^>]*>|<\/?([\w-]+)([^>]*)>|([^<]+)/g)){const value=match[0];if(value.startsWith('<!'))continue;if(match[3]){stack.at(-1).text+=unescape(match[3]);continue;}if(value.startsWith('</')){if(stack.length>1)stack.pop();continue;}const attrs={};for(const attr of(match[2]||'').matchAll(/([\w-]+)(?:="([^"]*)"|='([^']*)'|=([^\s>]+))?/g))attrs[attr[1]]=unescape(attr[2]??attr[3]??attr[4]??'');const node=new Element(match[1].toLowerCase(),attrs);stack.at(-1).children.push(node);if(!['meta','link','br','hr','img','input'].includes(node.tagName)&&!value.endsWith('/>'))stack.push(node);}return root;}
const tree=parse(html.replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/g,''));
const nodes=new Map();const visit=node=>{if(node.attrs.id){assert(!nodes.has(node.attrs.id),'IDs must be unique');nodes.set(node.attrs.id,node);}node.children.forEach(visit);};visit(tree);
nodes.set('evidence',new Element('script'));nodes.get('evidence').textContent=dataMatch[1];
document={getElementById:id=>nodes.get(id)||null,querySelectorAll:selector=>tree.querySelectorAll(selector),activeElement:null};
vm.runInNewContext(program,{document},{timeout:1000});
const checks=[];function check(name,fn){fn();checks.push({check:name,passed:true});}
check('self_contained_no_external_dependencies',()=>{assert(!/<script[^>]+src=|<link[^>]+href=|<iframe/i.test(html));assert(html.includes("connect-src 'none'"));assert(!/\b(fetch|XMLHttpRequest|WebSocket|sendBeacon)\s*\(/.test(program));});
check('explicit_replay_and_zero_new_calls',()=>{assert.equal(evidence.replay_only,true);assert.equal(evidence.current_new_model_requests,0);assert(html.includes('切换与翻页不会调用模型'));});
check('four_tabs_and_initial_visibility',()=>{assert.equal(tree.querySelectorAll('[role=tab]').length,4);assert.equal(nodes.get('mini').hidden,false);assert.equal(nodes.get('confirm').hidden,true);});
check('tabs_switch_all_panels',()=>{const tabs=tree.querySelectorAll('[role=tab]');for(const tab of tabs){tab.click();for(const other of tabs)assert.equal(nodes.get(other.dataset.tab).hidden,other!==tab);assert.equal(tab.getAttribute('aria-selected'),'true');}});
check('keyboard_tabs_move_focus',()=>{const tabs=tree.querySelectorAll('[role=tab]');tabs[0].listeners.keydown({key:'ArrowRight',preventDefault(){}});assert.equal(document.activeElement,tabs[1]);assert.equal(nodes.get('confirm').hidden,false);});
check('minimization_data_has_independent_final_runs',()=>{assert.equal(evidence.cases.v06.minimization.final_runs.length,2);assert.equal(evidence.cases.v06.minimization.final_revalidation.same_error_count,2);assert.equal(nodes.get('mini-flow').querySelectorAll('.flow').length,6);});
check('unknown_case_never_labeled_reduction_success',()=>{const report=evidence.cases.v061;assert.equal(report.baseline.reported_outcome,'unknown');assert.equal(report.fixed.reported_outcome,'success');assert.equal(report.minimization.trials,0);assert.equal(evidence.verifications.new_zip.reports[0].same_error_reduction,false);});
check('trace_next_previous_and_arm_switch',()=>{for(const id of['trace-mini','trace-confirm']){const host=nodes.get(id),first=host.querySelector('.raw').textContent;assert(host.querySelector('[data-step="-1"]').disabled);host.querySelector('[data-step="1"]').click();assert.notEqual(host.querySelector('.raw').textContent,first);host.querySelector('[data-step="-1"]').click();assert.equal(host.querySelector('.raw').textContent,first);host.querySelector('[data-arm="fixed"]').click();assert.equal(host.querySelector('[data-arm="fixed"]').getAttribute('aria-pressed'),'true');assert(host.querySelector('[data-step="-1"]').disabled);}});
check('negative_copies_have_one_and_three_semantic_errors',()=>{assert.equal(evidence.verifications.A.errors.length,1);assert.equal(evidence.verifications.B.errors.length,3);for(const key of['A','B']){const report=evidence.verifications[key].reports[0];assert.equal(report.conclusion_status,'unverified');assert.equal(report.observed_counterexample,null);assert.equal(report.same_error_reduction,null);}});
check('original_archives_remain_passed',()=>{assert(evidence.verifications.old_zip.passed);assert(evidence.verifications.new_zip.passed);});
check('coverage_and_limitations_are_bound_to_actual_output',()=>{assert.equal(evidence.coverage.length,6);assert.equal(evidence.not_covered.length,7);assert.equal(nodes.get('coverage').querySelectorAll('.kv').length,6);});
check('budget_and_research_status_match_current_evidence',()=>{assert.equal(evidence.design.planned_trials,16);if(evidence.real_study){assert.equal(evidence.budget.requests_made,301);assert.equal(evidence.budget.max_requests,374);assert.equal(evidence.readiness.available_requests,73);assert.equal(evidence.real_study.accounting.new_requests,71);assert.equal(evidence.real_study.accounting.cumulative_accounted_cost_cny,'0.809685');assert(nodes.get('plan-status').textContent.includes('16/16'));assert(nodes.get('tab-plan').textContent.includes('真实八组对照'));assert(!nodes.get('plan-intro').textContent.includes('尚无八组真实效果'));assert(nodes.get('budget-note').textContent.includes('没有观察到组合的额外收益'));}else{assert.equal(evidence.budget.requests_made,230);assert.equal(evidence.readiness.available_requests,130);assert.equal(evidence.readiness.request_gap,14);assert.equal(evidence.readiness.proposal_active,false);}});
check('eight_arm_table_preserves_policy_layers_and_counts',()=>{if(!evidence.real_study)return;assert.equal(evidence.real_study.arms.length,8);assert.equal(evidence.real_study.trials.length,16);const wanted={'neutral:baseline':0,'neutral:idempotency_only':1,'neutral:verification_only':2,'neutral:verified':2,'recovery:baseline':1,'recovery:idempotency_only':2,'recovery:verification_only':2,'recovery:verified':2};for(const arm of evidence.real_study.arms){assert.equal(arm.valid_trials,2);assert.equal(arm.confirmed_completions,wanted[arm.arm_id]);assert.equal(arm.successes,2);}assert.equal(nodes.get('arm-matrix').querySelectorAll('tr').length,2);});
check('all_source_files_match_embedded_hashes',()=>{for(const source of evidence.sources){assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(root,source.path))).digest('hex'),source.sha256);}});
tree.querySelectorAll('[role=tab]')[0].click();
console.log(JSON.stringify({passed:true,checks,html_sha256:crypto.createHash('sha256').update(html).digest('hex'),scope:'Offline DOM contracts only; no browser rendering, screen capture, or model calls.'},null,2));
