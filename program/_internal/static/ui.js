(() => {
  'use strict';
  const sources = new Map();
  const baseBoundary = document.getElementById('limitations-text').textContent;
  function showPage(name, updateHash = true) {
    const tool = name==='rule-batch' ? 'testing' : name==='concurrency' ? 'concurrency' : null;
    const batch=document.getElementById('page-testing');
    if(tool==='testing'||name==='usage'){document.getElementById('rule-batch-host').append(batch);batch.hidden=false;}
    else if(name==='testing'){document.querySelector('main').insertBefore(batch,document.getElementById('page-usage'));}
    if(tool) name='usage';
    const valid = ['workbench', 'import', 'testing', 'statistics', 'concurrency', 'history', 'usage'];
    if (!valid.includes(name)) name = 'workbench';
    document.querySelectorAll('main > [data-page]').forEach(page => { page.hidden = page.dataset.page !== name; });
    document.querySelectorAll('[data-page-link]').forEach(link => {
      if (link.dataset.pageLink === name) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    if(tool) document.getElementById('usage-'+tool).open=true;
    if (updateHash && location.hash !== '#' + (tool==='testing'?'rule-batch':name)) history.pushState(null, '', '#' + (tool==='testing'?'rule-batch':name));
    window.scrollTo({ top: 0, behavior: 'instant' });
    document.title = '追因 · ' + ({workbench:'实验工作台',import:'导入数据',testing:'批量测试',statistics:'对比测试',concurrency:'并发对照',history:'实验历史',usage:'使用方法'}[name]);
    window.dispatchEvent(new CustomEvent('zhuiyin:page', {detail:{page:name, batchMode:name==='usage'?'rules':'agent'}}));
  }
  function route() {
    const hash = location.hash.slice(1);
    const aliases = {top:'workbench','import-workspace':'import','study-workspace':'statistics','history-workspace':'history'};
    const details = ['ablation','counterexample','execution','regression'];
    showPage(aliases[hash] || (details.includes(hash) ? 'workbench' : hash), false);
    if (details.includes(hash)) {
      const target = document.getElementById(hash);
      if (target && !target.closest('[hidden]')) target.open = true;
    }
  }
  function focusResults() {
    showPage('workbench');
    document.getElementById('result-overview').classList.remove('hidden');
    document.getElementById('experiment-details').classList.remove('hidden');
    document.getElementById('config-panel').open = false;
    document.getElementById('page-workbench').classList.add('has-results');
    document.getElementById('workbench-return-footer').classList.remove('hidden');
  }
  function setLimitations(source, items) {
    sources.set(source, Array.isArray(items) ? items.filter(item => typeof item === 'string' && item.trim()) : []);
    const target = document.getElementById('limitations-text');
    const unique = [...new Set([...sources.values()].flat())];
    const intro = document.createElement('div'); intro.textContent = baseBoundary;
    const detail = document.createElement('ul');
    unique.forEach(text => { const item = document.createElement('li'); item.textContent = text; detail.append(item); });
    target.replaceChildren(intro, ...(unique.length ? [detail] : []));
  }
  function pager({container, items, pageSize=10, renderPage}) {
    let page = 1;
    const pages = Math.max(1, Math.ceil(items.length / pageSize));
    function render() {
      renderPage(items.slice((page - 1) * pageSize, page * pageSize), page);
      if (items.length <= pageSize) return;
      const controls = document.createElement('nav'); controls.className = 'pager'; controls.setAttribute('aria-label', '数据分页');
      const previous = document.createElement('button'); previous.type = 'button'; previous.textContent = '上一页'; previous.disabled = page === 1;
      const label = document.createElement('span'); label.textContent = '第 ' + page + ' / ' + pages + ' 页'; label.setAttribute('aria-live', 'polite');
      const next = document.createElement('button'); next.type = 'button'; next.textContent = '下一页'; next.disabled = page === pages;
      previous.addEventListener('click', () => go(page - 1)); next.addEventListener('click', () => go(page + 1));
      controls.append(previous, label, next); container.append(controls);
    }
    function go(value) { page = Math.max(1, Math.min(pages, Number(value) || 1)); render(); }
    render();
    return {reset:()=>go(1), go};
  }
  window.ZhuiyinUI = {showPage, focusResults, setLimitations, pager};
  document.querySelectorAll('a[href^="#"]').forEach(link => {
    const name = link.getAttribute('href').slice(1);
    if (!['workbench','import','testing','statistics','concurrency','history','usage','rule-batch'].includes(name)) return;
    link.addEventListener('click', event => {event.preventDefault(); showPage(name);});
  });
  document.addEventListener('click', event => {
    document.querySelectorAll('.action-menu[open]').forEach(menu => {
      if (!menu.contains(event.target) || event.target.closest('button,a')) menu.open = false;
    });
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') document.querySelectorAll('.action-menu[open]').forEach(menu=>menu.open=false);
  });
  document.getElementById('experiment-form').addEventListener('invalid', event => {
    event.target.closest('details')?.setAttribute('open', '');
    document.getElementById('config-panel').open = true;
  }, true);
  window.addEventListener('hashchange', route); window.addEventListener('popstate', route);
  route();
})();
