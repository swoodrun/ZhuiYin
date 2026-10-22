"""Build a standalone evidence-contract viewer; no network or model calls."""
import argparse
import html
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evidence_qualification import build_design

PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>追因 · 证据资格</title><style>
*{box-sizing:border-box}body{background:#f4f5f1;color:#203d48;margin:0;font:15px/1.8 "Segoe UI","Microsoft YaHei",sans-serif}main{max-width:1140px;padding:38px 25px;margin:auto}.brand{font-size:12px;letter-spacing:3px;color:#64777a}h1{font-weight:550;font-size:32px;line-height:1.4}p{max-width:900px}.intro{color:#63797e}.badge{display:inline-block;background:#e7eee9;padding:3px 10px;border-radius:5px;font-size:12px}nav{display:flex;gap:10px;flex-wrap:wrap;margin:26px 0}button{cursor:pointer;border:1px solid #ccd9d6;background:white;color:#244653;padding:11px 15px;border-radius:8px;font:inherit}button[aria-pressed=true]{background:#244e61;color:white;border-color:#244e61}.card{background:white;border:1px solid #dde5df;border-radius:14px;padding:24px;margin:20px 0}.columns{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.columns article{background:#f1f5f2;padding:18px;border-radius:9px}.columns h3{font-size:13px;font-weight:500;color:#617777;margin:0}.columns strong{font-size:23px;font-weight:550;display:block;margin:12px 0}h2{font-size:20px;font-weight:550;margin:0 0 14px}.scope{font-size:13px;color:#61777e}pre{max-height:280px;overflow:auto;font:12px/1.7 Consolas,monospace;background:#f0f3f1;padding:15px;border-radius:7px;white-space:pre-wrap;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;border-bottom:1px solid #e1e7e3;padding:11px 7px}a{color:#235f78}details{margin-top:18px}footer{font-size:12px;color:#688086}button:focus-visible,a:focus-visible{outline:3px solid #688f9c;outline-offset:3px}@media(max-width:700px){.columns{grid-template-columns:1fr}main{padding:26px 16px}h1{font-size:27px}table{font-size:12px}.card{padding:18px}}@media print{button{display:none}pre{max-height:none}}
</style><main><div class="brand">追因 / 证据资格研究</div><h1>一张回执，凭什么证明任务完成？</h1><p class="intro">看清回执属于谁、覆盖多完整、对应何时，再判断它能支持什么结论。这里将数据库真值、可见证据和模型报告并列。</p><span class="badge">__STATUS__</span><nav aria-label="证据条件"><button data-case="E0" aria-pressed="true">E0 可靠空快照</button><button data-case="E1" aria-pressed="false">E1 不完整空页</button><button data-case="E2" aria-pressed="false">E2 其他请求快照</button><button data-case="E3" aria-pressed="false">E3 可靠正记录</button></nav><section class="card" aria-live="polite"><h2 id="condition"></h2><div class="columns"><article><h3>数据库实际状态</h3><strong id="actual"></strong><span>仅供实验裁判，未发给模型</span></article><article><h3>可见证据支持</h3><strong id="support"></strong><span id="explanation"></span></article><article><h3>真实模型报告</h3><strong id="model"></strong><span id="model-note"></span></article></div><details><summary>查看实际核验回执</summary><pre id="receipt"></pre></details><details><summary>查看两种协议及逐格统计</summary><div id="results"></div></details></section><section class="card"><h2>怎样才算改善？</h2><p>E0 / E3 看有依据的明确判断；E1 / E2 看是否保留合理的 unknown。不能靠一律报告成功或一律 unknown 取得好的评价。</p><p class="scope">结构化协议单独核对引用消息与属性。引用另一请求的回执，可以抄录正确，但仍不适用于当前请求。引用准确、结论有依据、数据库已完成是三件事。</p><p class="scope">“证据未利用”仅描述输出未匹配证据足以支持的判断，不说明模型没看证据或不理解。合理 unknown 只评价断言依据，不评价停止行动最优；本轮冻结判断不等于自主恢复。</p></section><section class="card"><h2>证据与边界</h2><p><a href="证据资格对照实验协议.md">固定实验协议</a> · <a href="证据资格升级说明.md">本轮升级说明</a> · <a href="成本与收益对照.md">既有成本与收益</a> · <a href="故障与现实机制映射.md">现实机制映射</a></p><p class="scope">4条件 × 2协议 × 3重复，最多24次新增请求。新旧研究分开；单一模板只作描述性观察，不推断显著性、迁移或模型内部原因。材料核验不是来源认证。离线页面实屏验收仍待人工完成。</p></section><footer>本页离线可读，无密钥输入、网络请求或模型调用。旧30条原始证据保留。</footer></main><script id="study" type="application/json">__DATA__</script><script>
'use strict';const study=JSON.parse(document.getElementById('study').textContent);
const names={E0:'E0 · 可靠空快照',E1:'E1 · 不完整空页',E2:'E2 · 其他请求快照',E3:'E3 · 可靠正记录'},notes={E0:'请求、冻结时点、完整性与权威性都匹配，空快照支持当前未完成。',E1:'完整性为 false，空页允许遗漏正确记录。',E2:'回执属于 eq-other-001，不能否认当前请求。',E3:'适用的完整权威快照包含唯一正确工单。'};
function show(id){const c=study.cases.find(c=>c.condition_id===id);document.getElementById('condition').textContent=names[id];document.getElementById('actual').textContent=c.actual_success?'已完成':'当前未完成';document.getElementById('support').textContent=c.supported_outcome;document.getElementById('explanation').textContent=notes[id];document.getElementById('receipt').textContent=JSON.stringify(c.receipt,null,2);document.querySelectorAll('button[data-case]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.case===id)));const rows=study.groups.filter(g=>g.condition_id===id);document.getElementById('model').textContent=rows.length?rows.reduce((n,g)=>n+g.valid,0)+' 次有效报告':'尚未采集';document.getElementById('model-note').textContent=rows.length?'见下方逐协议统计；不合并旧实验。':'此处是合同与数据库演示，不能算模型行为观察。';const result=document.getElementById('results');result.replaceChildren();if(!rows.length){result.textContent='直接判断：outcome + reason；结构化引用：额外报告消息ID、请求ID、快照属性、记录ID与缺失字段。真实模型24次计划尚未执行。';return;}const table=document.createElement('table');const head=table.insertRow();['协议','已处理 / 有效','success / failure / unknown','明确正确 / 合理unknown','正确引用 / 提供引用'].forEach(t=>{const th=document.createElement('th');th.textContent=t;head.appendChild(th);});rows.forEach(g=>{const tr=table.insertRow();[g.protocol_id,g.attempted+' / '+g.valid,[g.outcomes.success,g.outcomes.failure,g.outcomes.unknown].join(' / '),g.qualified_correct+' / '+g.justified_unknown,g.citation?g.citation.reference_correct+' / '+g.citation.provided:'不适用'].forEach(t=>{tr.insertCell().textContent=t;});});result.appendChild(table);}
document.querySelectorAll('button[data-case]').forEach(b=>b.addEventListener('click',()=>show(b.dataset.case)));show('E0');
</script></html>'''


def build(output=None, collected=None, verification=None, offline=False):
    design = build_design()
    groups = []
    status = "离线合同演示 · 真实模型尚未采集"
    # A normal rebuild must retain the completed study. An explicitly selected
    # offline preview remains clearly separated from saved model observations.
    default_collected = ROOT / "data/evaluations/evidence-qualification-real-20261018"
    if collected is None and not offline and (default_collected / "study-report.json").is_file():
        collected = default_collected
    provenance = '<p>离线合同预览：尚无模型观察，不以预期答案充当实测结果。</p>'
    if collected is not None:
        # Never display unverified or ad-hoc model summaries as observations.
        from tools.verify_evidence_qualification import verify
        prepared = ROOT / "data/research/evidence-qualification-20261018-prepared"
        audit = verify(prepared, collected)
        if not audit["passed"]:
            raise ValueError("未通过独立核验的结果不得进入演示")
        report = json.loads((Path(collected) / "study-report.json").read_text(encoding="utf-8"))
        groups = report["summary"]["groups"]
        status = "真实采集已独立核对 · " + report["status"]
        budget = report["budget_after"]
        provided = sum(g["citation"]["provided"] for g in groups if g["citation"])
        exists = sum(g["citation"]["exists"] for g in groups if g["citation"])
        provenance = (
            f'<p>本阶段24次判断已完成，材料内部核对{audit["check_count"]}项通过。'
            f'结构化引用可定位 {exists}/{provided}；两协议在本模板中未显示最终判断增益。</p>'
            f'<p>截至该研究完成，专项累计 {budget["requests_made"]}/{budget["max_requests"]} 次，'
            f'{html.escape(budget["accounted_cost_cny"])} / 10 元保守记账；不是服务商账单。</p>'
            '<p><a href="当前交付口径.md">当前交付口径</a> · '
            '<a href="证据资格真实对照实验报告.md">真实对照报告</a> · '
            '<a href="data/research/evidence-qualification-real-20261018-verification/verification.json">独立核对明细</a> · '
            '<a href="data/evaluations/evidence-qualification-real-20261018/study-report.json">保存的原始研究汇总</a></p>'
            '<p class="scope">旧30条、八组自由执行及本阶段分开计数。核验不认证服务来源或模型身份；'
            '真实答辩机显示与缩放仍待人工验收。</p>')
    data = {"cases": [{k: c[k] for k in ("condition_id", "actual_success", "supported_outcome", "receipt")}
                       for c in design["cases"]], "groups": groups}
    serialized = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    output = Path(output or ROOT / "证据资格演示.html")
    rendered = PAGE.replace("__STATUS__", html.escape(status)).replace("__DATA__", serialized)
    rendered = rendered.replace('<footer>', '<section class="card" id="delivery-provenance"><h2>当前阶段与可核对来源</h2>' + provenance + '</section><footer>', 1)
    output.write_text(rendered, encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--collected")
    parser.add_argument("--offline", action="store_true", help="Explicitly build a contract-only preview without saved model results")
    args = parser.parse_args()
    if args.offline and args.collected:
        parser.error("--offline and --collected cannot be combined")
    print(build(args.output, args.collected, offline=args.offline))
