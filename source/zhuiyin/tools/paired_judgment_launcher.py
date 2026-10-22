"""One-shot local key intake for the already-authorized frozen judgment study."""
from __future__ import annotations

import argparse
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import paired_judgment_experiment as experiment
from tools.eight_arm_launcher import Handler as SafeHandler, LaunchState as BudgetReader

PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>追因 · 冻结状态判断</title><style nonce="__NONCE__">
*{box-sizing:border-box}body{margin:0;background:#f3f5f4;color:#203b46;font:15px/1.8 "Segoe UI","Microsoft YaHei",sans-serif}main{max-width:810px;margin:auto;padding:42px 24px}.brand{font-size:14px;letter-spacing:4px;color:#55747b}h1{font-weight:550;font-size:30px;line-height:1.45;margin:22px 0 10px}.intro{color:#63777b}.card{padding:26px;background:#fff;border:1px solid #dce5e2;border-radius:15px;margin:25px 0}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;padding-bottom:22px;border-bottom:1px solid #e2e9e6;margin-bottom:22px}.metrics b{display:block;font-size:27px;font-weight:550}.metrics span{font-size:12px;color:#6d7f82}label{display:block;font-size:13px;font-weight:600;margin-bottom:8px}input{width:100%;border:1px solid #c0cdcb;border-radius:7px;padding:12px;font:inherit}input:focus{outline:2px solid #467785;outline-offset:2px}button{border:0;border-radius:7px;background:#28566a;color:#fff;padding:12px 18px;font:inherit;margin-top:16px;cursor:pointer}button:disabled{opacity:.5;cursor:default}.note{font-size:12px;color:#708184}.status{background:#eef3f1;border-radius:8px;padding:16px;white-space:pre-wrap}progress{width:100%;height:9px;margin:16px 0}.scope{color:#64797f;font-size:13px}#error{color:#a14536}footer{font-size:11px;color:#7b8c8e}@media(max-width:500px){main{padding:26px 16px}.card{padding:20px}.metrics{gap:8px}.metrics b{font-size:23px}h1{font-size:26px}}
</style></head><body><main><div class="brand">追因 / 配对证据研究</div><h1>模型在什么证据下判断成功？</h1><p class="intro">从同一决策点，比较原有模糊信息、普通再查询与可靠核验。数据库保持冻结，模型只判断当前状态。</p><section class="card"><div class="metrics"><div><b>3 × 2 × 5</b><span>信息条件 × 世界 × 重复次数</span></div><div><b>30</b><span>计划请求，不另发连接测试</span></div><div><b>374 / 10元</b><span>已授权累计上限</span></div></div><form id="key-form"><label for="api-key">DeepSeek API 密钥</label><input id="api-key" type="password" autocomplete="off" spellcheck="false" required placeholder="只在本机填写，请勿发到聊天中"><button id="start" type="submit">开始已授权的30次判断探针</button><p class="note">密钥只保留在本次进程内存，不进入报告或台账；提交后清空输入框。</p></form><p id="error" role="alert"></p><div id="status" class="status" aria-live="polite">正在核对固定方案…</div><progress id="progress" max="30" value="0" aria-label="已尝试判断次数"></progress><p id="usage" class="note"></p></section><p class="scope">累计起点为301次、0.809685元。每个样本只请求一次，不执行模型提出的业务操作，不自动重跑。无报告、格式异常和服务错误单列；unknown只评价当前断言的依据，不表示停止行动最优。普通查询在本轮共同合同下只会漏读，不会伪造或串入记录；这一边界不代表所有缓存。</p><footer>仅限本机访问 · 结果产生前不作效果结论 · 关闭网页不会撤回已发出的请求</footer></main><script nonce="__NONCE__">
'use strict';const nonce='__NONCE__',form=document.getElementById('key-form'),input=document.getElementById('api-key'),button=document.getElementById('start'),status=document.getElementById('status'),error=document.getElementById('error');
form.addEventListener('submit',async e=>{e.preventDefault();button.disabled=true;error.textContent='';let key=input.value.trim();input.value='';try{const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key,nonce})});key='';const v=await r.json();if(!r.ok)throw Error(v.message||'未启动');form.hidden=true;status.textContent='已启动，正在逐次保存请求与判断。';}catch(err){key='';error.textContent=err.message;button.disabled=false;}});
async function update(){try{const r=await fetch('/api/status');if(!r.ok)throw Error();const s=await r.json();document.getElementById('progress').value=s.attempted;status.textContent=s.message+'\n已尝试 '+s.attempted+' / 30；有效报告 '+s.valid+'；无报告或异常 '+s.non_valid+'。';document.getElementById('usage').textContent='累计请求 '+s.budget.requests_made+' / '+s.budget.max_requests+'；累计保守记账 '+s.budget.accounted_cost_cny+' / 10 元。';if(s.started)form.hidden=true;if(!s.finished)setTimeout(update,2000);}catch(_){status.textContent='本机服务暂时不可读；已保存的证据仍保留在研究目录中。';}}update();
</script></body></html>'''


class LaunchState(BudgetReader):
    def __init__(self, prepared, output):
        self.prepared, self.output = Path(prepared).resolve(), Path(output).resolve()
        experiment.validate_prepared(self.prepared)
        self.policy = self.prepared / "campaign/campaign-policy.json"
        policy = json.loads(self.policy.read_text(encoding="utf-8"))
        self.ledger = Path(policy["ledger_path"])
        initial = json.loads((self.prepared / "campaign/initial-summary.json").read_text(encoding="utf-8"))
        self.expected_policy_sha256 = initial["policy_sha256"]
        if experiment.ledger_rows(self.ledger) != experiment.ledger_rows(self.prepared / "campaign/campaign-ledger-initial.sqlite"):
            raise ValueError("固定方案已经使用，不能重启")
        current = self.read_budget_status()
        if current["requests_made"] != 301 or current["max_requests"] != 374 or current["accounted_cost_cny"] != "0.809685":
            raise ValueError("累计预算起点不符")
        if self.output.exists():
            raise FileExistsError("已有实验结果，不能覆盖或重跑")
        self.nonce = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.started = self.finished = False
        self.phase = "ready"

    def _work(self, key):
        try:
            report = experiment.execute(self.prepared, self.output, key)
            self.phase = "finished" if report["status"] == "finished" else "partial"
        except BaseException:
            self.phase = "interrupted"
        finally:
            key = None
            self.finished = True

    def public(self):
        try:
            report = json.loads((self.output / "study-report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
        samples = report.get("samples", [])
        valid = sum(s["status"] == "valid" for s in samples)
        return {"started": self.started, "finished": self.finished, "phase": self.phase,
                "attempted": len(samples), "valid": valid, "non_valid": len(samples)-valid,
                "budget": self.read_budget_status(),
                "message": {"ready": "固定方案与累计容量检查通过，等待在本机填入密钥。",
                            "running": "正在按已固定顺序采集，不会执行业务工具或自动重试。",
                            "finished": "计划已执行完毕，等待独立复核；这不代表所有样本有效或核验更优。",
                            "partial": "计划提前停止，保留已有证据与未运行位置。",
                            "interrupted": "采集发生中断，保留已有文件，不自动重跑。"}[self.phase]}


class Handler(SafeHandler):
    def do_GET(self):
        if self.path == "/" and self.allowed():
            return self.send(200, PAGE.replace("__NONCE__", self.server.state.nonce), True)
        return super().do_GET()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    state = LaunchState(args.prepared, args.output)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.state = state
    print(json.dumps({"status": "ready", "url": f"http://127.0.0.1:{server.server_port}/", "model_calls": 0}), flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
