/* Targeted source-function and synthetic-DOM contracts for returning home.
 * No service, real history, dataset, credentials, engine or model is accessed.
 * This does not replace a real-browser layout and click check.
 */
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(__dirname,'..');
const app=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const ui=fs.readFileSync(path.join(root,'static/ui.js'),'utf8');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
function extract(source,name){
  const start=source.indexOf('function '+name+'(');assert(start>=0,'Missing function '+name);
  let i=source.indexOf('{',start),level=0,quote=null;
  for(;i<source.length;i++){
    const char=source[i];
    if(quote){if(char==='\\'){i++;continue;}if(char===quote)quote=null;continue;}
    if(char==='"'||char==="'"||char==='`'){quote=char;continue;}
    if(char==='{')level++;else if(char==='}'&&--level===0)return source.slice(start,i+1);
  }
  throw new Error('Unclosed function '+name);
}
class Element{
  constructor(id){this.id=id;this.open=false;this.disabled=false;this.hidden=false;this.value='';this.checked=false;this.parentElement=null;this.children=[];this.attrs={};this.classes=new Set();this.focused=0;this.style={};this.classList={add:(...names)=>names.forEach(name=>this.classes.add(name)),remove:(...names)=>names.forEach(name=>this.classes.delete(name)),contains:name=>this.classes.has(name),toggle:(name,force)=>{const next=force===undefined?!this.classes.has(name):Boolean(force);if(next)this.classes.add(name);else this.classes.delete(name);return next;}};}
  append(...nodes){for(const node of nodes){if(node.parentElement)node.parentElement.children=node.parentElement.children.filter(item=>item!==node);node.parentElement=this;this.children.push(node);}}
  insertBefore(node,target){if(node.parentElement)node.parentElement.children=node.parentElement.children.filter(item=>item!==node);node.parentElement=this;const index=this.children.indexOf(target);this.children.splice(index<0?this.children.length:index,0,node);}
  setAttribute(name,value){this.attrs[name]=value;}
  focus(){this.focused++;}
}
function environment(){
  const ids=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>[match[1],new Element(match[1])]));
  const $=id=>{assert(ids.has(id),'Missing DOM ID '+id);return ids.get(id);};
  const buttons=[$('return-workbench-btn'),$('return-workbench-bottom-btn')],panels=[$('comparison-panel'),$('execution'),$('counterexample')];
  const inputs=[$('model'),$('api-key'),$('stock'),$('quantity'),$('request-id')];
  inputs.forEach((input,index)=>input.value='SYNTHETIC_PRESERVED_'+index);
  $('page-workbench').append($('result-overview'),$('config-panel'),$('experiment-details'),$('workbench-return-footer'));
  const calls=[],pages=[],savedHistory={run_id:'SYNTHETIC_SAVED_RUN',result:{unchanged:true}},snapshot=JSON.stringify(savedHistory);
  const document={getElementById:$,querySelectorAll(selector){if(selector==='[data-workbench-home]')return buttons;if(selector==='#result-overview details,#experiment-details details')return panels;if(selector==='#experiment-form input,#experiment-form select')return inputs;throw new Error('Unexpected query '+selector);}};
  const context={document,singleBusy:false,polling:false,externalBusy:false,result:{saved:true},currentRun:'SYNTHETIC_SAVED_RUN',ruleCheckRunning:false,$,window:{dispatchEvent(){},ZhuiyinUI:{showPage:name=>pages.push(name)}},CustomEvent:class{constructor(type,options){this.type=type;this.detail=options?.detail;}},showWorkbench(){pages.push('workbench');},hideError(){$('error-box').classList.add('hidden');},updateScenarioFields(){},fetch(){calls.push('fetch');throw new Error('Returning home must not request a service');},post(){calls.push('POST');throw new Error('Returning home must not POST');},request(){calls.push('GET');throw new Error('Returning home must not fetch history');}};
  vm.createContext(context);
  vm.runInContext(extract(ui,'focusResults').replace('function focusResults','function uiFocusResults'),context);
  context.showPage=name=>pages.push(name);context.window.ZhuiyinUI.focusResults=context.uiFocusResults;
  vm.runInContext([extract(app,'returnToWorkbench'),extract(app,'focusResults'),extract(app,'refreshBusyControls')].join('\n'),context);
  const form=()=>JSON.stringify(inputs.map(input=>({id:input.id,value:input.value,checked:input.checked})));
  return{context,$,buttons,panels,calls,pages,form,initialForm:form(),savedHistory,snapshot};
}
const checks=[];function check(name,fn){fn();checks.push({name,passed:true});}
function assertHome(env){assert(env.$('result-overview').classList.contains('hidden'));assert(env.$('experiment-details').classList.contains('hidden'));assert(env.$('workbench-return-footer').classList.contains('hidden'));assert(!env.$('page-workbench').classList.contains('has-results'));assert.equal(env.$('config-panel').open,true);assert.equal(env.pages.at(-1),'workbench');}
check('explicit_top_and_bottom_buttons_are_non_submit_controls',()=>{for(const id of['return-workbench-btn','return-workbench-bottom-btn']){const match=html.match(new RegExp('<button\\b[^>]*\\bid="'+id+'"[^>]*>'));assert(match,'Missing '+id);assert(/\btype="button"/.test(match[0]));assert(/\bdata-workbench-home\b/.test(match[0]));}assert(app.includes("button.addEventListener('click',returnToWorkbench)"));});
check('agent_result_view_has_return_footer_after_details',()=>{const env=environment();env.context.focusResults();assert(env.$('page-workbench').classList.contains('has-results'));assert(!env.$('workbench-return-footer').classList.contains('hidden'));assert.equal(env.$('page-workbench').children.at(-1),env.$('workbench-return-footer'));});
check('completed_result_returns_home_and_closes_open_details',()=>{const env=environment();env.context.focusResults();env.panels.forEach(panel=>panel.open=true);env.$('experiment-source').classList.remove('hidden');assert.equal(env.context.returnToWorkbench(),true);assertHome(env);assert(env.panels.every(panel=>!panel.open));assert(env.$('experiment-source').classList.contains('hidden'));assert.equal(env.$('workbench-home-title').focused,1);});
check('return_preserves_form_values_and_synthetic_secret',()=>{const env=environment();env.context.focusResults();env.context.returnToWorkbench();assert.equal(env.form(),env.initialForm);assert.equal(env.$('api-key').value,'SYNTHETIC_PRESERVED_1');});
check('return_preserves_saved_result_and_run_identity',()=>{const env=environment(),result=env.context.result,run=env.context.currentRun;env.context.focusResults();env.context.returnToWorkbench();assert.equal(env.context.result,result);assert.equal(env.context.currentRun,run);assert.equal(JSON.stringify(env.savedHistory),env.snapshot);});
check('return_does_not_issue_requests_or_rerun',()=>{const env=environment();env.context.focusResults();env.context.returnToWorkbench();assert.deepEqual(env.calls,[]);});
check('single_run_disables_both_home_buttons_and_blocks_return',()=>{const env=environment();env.context.focusResults();env.context.singleBusy=true;env.context.refreshBusyControls();assert(env.buttons.every(button=>button.disabled));assert.equal(env.context.returnToWorkbench(),false);assert(env.$('page-workbench').classList.contains('has-results'));});
check('active_poll_blocks_return_even_if_single_busy_is_false',()=>{const env=environment();env.context.focusResults();env.context.polling=true;env.context.refreshBusyControls();assert(env.buttons.every(button=>button.disabled));assert.equal(env.context.returnToWorkbench(),false);assert(!env.$('result-overview').classList.contains('hidden'));});
check('terminal_cleanup_reenables_return_buttons',()=>{const env=environment();env.context.singleBusy=env.context.polling=true;env.context.refreshBusyControls();env.context.polling=false;env.context.singleBusy=false;env.context.refreshBusyControls();assert(env.buttons.every(button=>!button.disabled));assert.equal(env.context.returnToWorkbench(),true);assertHome(env);});
check('unrelated_external_job_does_not_block_read_only_return',()=>{const env=environment();env.context.focusResults();env.context.externalBusy=true;env.context.refreshBusyControls();assert(env.buttons.every(button=>!button.disabled));assert.equal(env.context.returnToWorkbench(),true);assertHome(env);assert(env.$('run-btn').disabled);});
check('error_without_result_can_return_while_preserving_recovery_message',()=>{const env=environment();env.context.result=null;env.context.focusResults();env.$('error-box').classList.remove('hidden');assert.equal(env.context.returnToWorkbench(),true);assertHome(env);assert(!env.$('error-box').classList.contains('hidden'));});
check('partial_or_completed_result_return_hides_stale_error',()=>{const env=environment();env.context.focusResults();env.$('error-box').classList.remove('hidden');env.context.returnToWorkbench();assert(env.$('error-box').classList.contains('hidden'));});
check('next_result_can_be_opened_and_closed_again',()=>{const env=environment();for(let index=0;index<2;index++){env.context.focusResults();assert(env.$('page-workbench').classList.contains('has-results'));assert(!env.$('result-overview').classList.contains('hidden'));assert.equal(env.context.returnToWorkbench(),true);assertHome(env);}assert.equal(env.form(),env.initialForm);assert.deepEqual(env.calls,[]);});
check('rules_results_keep_footer_hidden_and_can_return_without_mutating_data',()=>{const env=environment();env.context.result={mode:'rules'};env.context.focusResults();assert.equal(env.$('result-overview').parentElement,env.$('rule-full-results'));assert(env.$('workbench-return-footer').classList.contains('hidden'));assert.equal(env.pages.at(-1),'usage');assert.equal(env.context.returnToWorkbench(),true);assertHome(env);assert.equal(env.context.result.mode,'rules');assert.equal(env.form(),env.initialForm);});
console.log(JSON.stringify({passed:true,total:checks.length,checks,scope:'Actual extracted UI functions plus synthetic DOM; no browser layout, real history, credentials, service, engine, datasets, or model calls.'},null,2));
