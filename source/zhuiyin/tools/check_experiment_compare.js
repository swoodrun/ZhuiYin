/* Synthetic DOM checks; these do not replace real-window inspection. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
class Element {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.attrs={};this.listeners={};this.dataset={};this._text='';this.value='';this.hidden=false;this.className='';this.classList={add:(...names)=>{this.className=[this.className,...names].filter(Boolean).join(' ');}};}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent||'').join('');}
  set innerHTML(value){throw new Error('HTML injection sink used');}
  get firstChild(){return this.children[0]||null;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this._text='';this.children=[...children];}
  setAttribute(name,value){this.attrs[name]=String(value);}
  addEventListener(name,listener){(this.listeners[name]??=[]).push(listener);}
  async fire(name){for(const listener of this.listeners[name]||[])await listener({preventDefault(){}});}
}
const descendants=node=>[node,...node.children.flatMap(descendants)];
const create=()=>new Element('div');
const fixture={comparison_version:'history-compare-v1',read_only:true,
  left:{run_id:'left',label:'<img src=x onerror=alert(1)>',kind:'experiment',records:1,usage:{total_tokens:null,estimated_cost:null}},
  right:{run_id:'right',label:'修改后',kind:'experiment',records:1,usage:{requests:2,total_tokens:40,estimated_cost:0.01,currency:'CNY',elapsed_ms:500}},
  warnings:['只读比较，不证明因果。'],configuration_differences:[{field:'config.prompt_policy',left:'neutral',right:'recovery',left_recorded:true,right_recorded:true},{field:'version.factor_design.instruction_policy',left:'neutral',right:'recovery',left_recorded:true,right_recorded:true}],
  groups:[{key:['llm','baseline','neutral','business-report-v2'],left:{records:1,valid:1,not_run:0,execution_errors:0,execution_unknown:0,metrics:{actual_success:{count:1,known:1,rate:1,missing:0},business_failed:{count:null,known:0,rate:null,missing:1}}},right:null}],
  paired:[{name:'工单',task:{request_id:'r1'},strategy:'baseline',status:'report_changed',condition_changed:true,left:{state:'valid',outcome:'unknown',prompt_policy:'neutral'},right:{state:'valid',outcome:'success',prompt_policy:'recovery'}}],
  paired_counts:{report_changed:1},matching_note:'按任务及条件对齐。',unmatched:{left:[],right:[]}};
const calls=[],window={Zhuiyin:{async request(url){calls.push({method:'GET',url});if(url.startsWith('/api/history'))return {items:[{run_id:'left',label:'基准'},{run_id:'right',label:'修改后'}],total:2};if(url.includes('/outside?lite=1'))return {run_id:'outside',label:'旧记录'};throw new Error('Unexpected request '+url);},async post(url,body){calls.push({method:'POST',url,body});assert.equal(url,'/api/compare');return fixture;}}};
const context={window,document:{createElement:tag=>new Element(tag)},Node:Element,fetch(){throw new Error('Unexpected real network');}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/experiment_compare.js'),'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function main(){
  const host=create();const control=window.ZhuiyinCompare.mount(host);await tick();
  assert.equal(calls.length,1);assert.equal(calls[0].method,'GET');assert(calls[0].url.startsWith('/api/history'));
  assert.equal(window.ZhuiyinCompare.mount(host),control);assert.equal(descendants(host).filter(n=>n.tagName==='FORM').length,1);
  await control.select('left','right');const selects=descendants(host).filter(n=>n.tagName==='SELECT');assert.equal(selects[0].value,'left');assert.equal(selects[1].value,'right');
  const form=descendants(host).find(n=>n.tagName==='FORM');await form.fire('submit');
  const post=calls.find(call=>call.method==='POST');assert(post);assert.equal(JSON.stringify(post.body),JSON.stringify({left_run_id:'left',right_run_id:'right'}));
  assert(host.textContent.includes('没有重新运行测试'));assert(host.textContent.includes('报告结论变化'));assert(host.textContent.includes('条件不同'));
  assert(host.textContent.includes('<img src=x onerror=alert(1)>'));assert(!descendants(host).some(n=>n.tagName==='IMG'));
  assert(host.textContent.includes('未记录')&&host.textContent.includes('报告无法确认')&&host.textContent.includes('基础任务指令')&&host.textContent.includes('单次实验'));assert(host.textContent.includes('业务违规'));assert(host.textContent.includes('不证明因果'));assert(descendants(host).some(n=>n.tagName==='TD'&&n.textContent.includes('无法确认 →')));
  assert(!descendants(host).some(n=>n.tagName==='TD'&&['neutral','recovery'].includes(n.textContent)));
  assert(!calls.some(call=>/run-agent|check-model|run-test-suite|\/api\/run$/.test(call.url)));
  await control.select('outside','right');assert.equal(selects[0].value,'outside');assert(calls.some(call=>call.url==='/api/runs/outside?lite=1'));
  const buttons=descendants(host).filter(n=>n.tagName==='BUTTON');const submit=buttons.find(n=>n.type==='submit');selects[0].value='';await form.fire('submit');assert(host.textContent.includes('请选择两份记录'));assert.equal(submit.disabled,false);
  const pageHost=create(),many={...fixture,paired:Array.from({length:25},(_,n)=>({...fixture.paired[0],name:'任务'+n}))};window.ZhuiyinCompare.renderReport(pageHost,many);
  assert(pageHost.textContent.includes('共 25 条'));assert(pageHost.textContent.includes('任务0'));assert(!pageHost.textContent.includes('任务24'));
  const next=descendants(pageHost).find(n=>n.tagName==='BUTTON'&&n.textContent==='下一页');await next.fire('click');assert(pageHost.textContent.includes('任务24'));assert(!pageHost.textContent.includes('任务0'));
  const css=fs.readFileSync(path.join(__dirname,'../static/experiment_compare.css'),'utf8');assert(css.includes('@media (max-width: 720px)'));assert(css.includes('overflow: auto'));
  console.log(JSON.stringify({passed:true,checks:20,scope:'synthetic DOM only',external_model_requests:0}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
