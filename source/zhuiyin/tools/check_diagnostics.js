/* Exercise evidence navigation without a browser or any network access. */
'use strict';
const assert=require('node:assert/strict');
const api=require('../static/diagnostics.js');
class Element{
  constructor(tag,doc){this.tagName=tag.toUpperCase();this.ownerDocument=doc;this.children=[];this.attrs={};this.listeners={};this.text='';this.className='';this.classList={add:name=>{this.className+=' '+name;},remove:name=>{this.className=this.className.split(/\s+/).filter(item=>item!==name).join(' ');}};}
  set innerHTML(_){throw Error('Untrusted evidence must be text');}
  set textContent(value){this.text=String(value);this.children=[];}
  get textContent(){return this.text+this.children.map(item=>item.textContent).join('');}
  append(...nodes){for(const node of nodes){node.parentElement=this;this.children.push(node);}}
  replaceChildren(...nodes){this.children=[];this.text='';this.append(...nodes);}
  setAttribute(key,value){this.attrs[key]=String(value);}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  click(){(this.listeners.click||[]).forEach(fn=>fn());}
  focus(){this.focused=true;}
  scrollIntoView(){this.scrolled=true;}
}
const doc={createElement:tag=>new Element(tag,doc)},host=new Element('div',doc);
const trial={completed:true,actual_success:false,reported_outcome:'success',report_assessment:'inconsistent',violations:[{code:'DUPLICATE_TICKET',message:'两条工单 <script>不可执行</script>'},{code:'REPORT_MISMATCH'}],business_records:[{id:1,status:'active'},{id:2,status:'active'}],events:[{kind:'fault',tool:'create_ticket',observed:{code:'COMMIT_UNKNOWN',api_key:'SECRET'},actual:{committed:true,work_orders:[{id:1,status:'active'}]}},{kind:'tool',tool:'create_ticket',actual:{work_orders:[{id:1,status:'active'},{id:2,status:'active'}]}},{kind:'report',tool:'finish_task',observed:{outcome:'success'}}]};
const result=api.mount(host,{config:{faults:['commit_response_lost','stale_read']},baseline:trial,fixed:{status:'not_run',skipped:true}});
const all=element=>[element,...element.children.flatMap(all)];
assert.equal(result.trials.length,2);assert(!host.textContent.includes('SECRET'));assert(host.textContent.includes('<script>不可执行</script>'));
assert(host.textContent.includes('配置：提交后响应丢失、读取过期状态'));
const buttons=all(host).filter(item=>item.tagName==='BUTTON');
const eventButton=buttons.find(item=>item.textContent==='定位事件 2');assert(eventButton);eventButton.click();
const eventTarget=all(host).find(item=>item.id?.endsWith('-event-1'));assert(eventTarget.open);assert(eventTarget.focused);assert(eventTarget.scrolled);assert(eventTarget.className.includes('diagnostics-highlight'));
const recordButton=buttons.find(item=>item.textContent==='定位记录 2');assert(recordButton);recordButton.click();
const recordTarget=all(host).find(item=>item.id?.endsWith('-record-1'));assert(recordTarget.open);assert(!eventTarget.className.includes('diagnostics-highlight'));
const select=all(host).find(item=>item.tagName==='SELECT');assert.equal(select.children[0].textContent,'原始');select.value='1';select.listeners.change[0]();assert(host.textContent.includes('本组未运行，没有可判断的业务结果。'));assert(!host.textContent.includes('两条工单'));
api.mount(host,{analysis:{actual_success:true,reported_outcome:'success'}});assert(host.textContent.includes('仅静态核对上传材料'));
api.mount(host,{samples:[{...trial,trial_index:1,strategy:'baseline',prompt_policy:'neutral'},{...trial,trial_index:2,strategy:'verified',prompt_policy:'recovery'}]});const sampleSelect=all(host).find(item=>item.tagName==='SELECT');assert.equal(sampleSelect.children[0].textContent,'第1轮 · 基础任务指令 · 原始');assert.equal(sampleSelect.children[1].textContent,'第2轮 · 任务指令＋恢复建议 · 幂等＋核验');
console.log(JSON.stringify({passed:true,checks:14,scope:'diagnostics-navigation-text-rendering'}));
