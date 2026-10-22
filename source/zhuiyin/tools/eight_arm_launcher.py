"""One-shot loopback credential intake for the already approved fixed study.

No key is written to disk, command arguments, public state, or request logs.
The only provider destination is enforced by the campaign's budget policy.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from decimal import Decimal
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import eight_arm_experiment as experiment


PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>追因 · 八组真实对照</title><style nonce="__NONCE__">
*{box-sizing:border-box}body{margin:0;background:#f3f6f5;color:#18313e;font:15px/1.8 "Segoe UI","Microsoft YaHei",sans-serif}.wrap{max-width:760px;padding:48px 24px;margin:auto}.brand{font-weight:600;letter-spacing:4px;color:#376479;font-size:18px}h1{font-size:30px;letter-spacing:-1px;font-weight:550;margin:24px 0 8px}.intro{color:#62747b}.card{background:white;border:1px solid #dce5e4;border-radius:16px;padding:28px;margin:24px 0}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;border-bottom:1px solid #e3e9e7;padding-bottom:23px;margin-bottom:23px}.metrics b{display:block;font-size:25px;font-weight:550}.metrics span{font-size:12px;color:#6e7e83}label{display:block;margin-bottom:8px;font-weight:600;font-size:13px}input{width:100%;padding:12px;border:1px solid #bacbcc;border-radius:8px;font:inherit}input:focus{outline:2px solid #39758a;outline-offset:2px}button{border:0;background:#224c60;color:white;font:inherit;border-radius:8px;padding:12px 20px;cursor:pointer;margin-top:18px}button:disabled{opacity:.5;cursor:default}.note{font-size:12px;color:#6e7e83;margin-top:13px}.status{padding:15px 18px;border-radius:9px;background:#eaf1f1;white-space:pre-wrap}.scope{font-size:13px;color:#667b82}#error{color:#a33e2e}progress{width:100%;height:10px;margin:15px 0}code{font-size:12px}footer{font-size:11px;color:#7b8a8f}@media(max-width:500px){.wrap{padding:24px 18px}.card{padding:20px}.metrics{gap:10px}.metrics b{font-size:22px}}
</style></head><body><main class="wrap"><div class="brand">追因 / 真实对照</div><h1>按固定方案执行八组研究</h1><p class="intro">两种提示 × 四种能力，每组两轮。填写一次密钥后开始；逐轮保存轨迹、数据库和调用用量。</p><section class="card"><div class="metrics"><div><b>16</b><span>计划业务执行</span></div><div><b>144</b><span>本研究请求上限</span></div><div><b>374</b><span>已授权累计请求上限</span></div></div><form id="key-form"><label for="api-key">DeepSeek API 密钥</label><input id="api-key" type="password" autocomplete="off" spellcheck="false" required placeholder="只在本机输入，请勿发到聊天中"><button id="start" type="submit">开始已授权的16轮实验</button><p class="note">密钥只进入本次进程内存，用于官方 DeepSeek 请求；不写入方案、报告或台账。不会另发连接测试。</p></form><p id="error" role="alert"></p><div id="status" class="status" aria-live="polite">正在核对准备状态…</div><progress id="progress" max="16" value="0" aria-label="已尝试业务轮次"></progress><p id="usage" class="note"></p></section><p class="scope">累计费用上限仍为10元，包含此前230次请求的0.612582元保守记账。失败、中断和未运行样本如实保留，不挑选性重跑。服务或费用保护可能使计划提前停止；本次不保证所有轮次有效。</p><footer>只允许本机访问 · 此页面不展示或回传已输入的密钥 · 关闭页面不会撤回已发出的模型请求</footer></main><script nonce="__NONCE__">
'use strict';const nonce='__NONCE__',form=document.getElementById('key-form'),input=document.getElementById('api-key'),button=document.getElementById('start'),status=document.getElementById('status'),error=document.getElementById('error');
form.addEventListener('submit',async event=>{event.preventDefault();button.disabled=true;error.textContent='';let key=input.value.trim();input.value='';try{const response=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key,nonce})});key='';const body=await response.json();if(!response.ok)throw new Error(body.message||'未启动，请保留已有记录。');form.hidden=true;status.textContent='研究已启动，正在保留逐轮证据。';}catch(err){key='';error.textContent=err.message;button.disabled=false;}});
async function update(){try{const response=await fetch('/api/status');if(!response.ok)return;const s=await response.json();document.getElementById('progress').value=s.attempted;status.textContent=s.message+'\n已尝试 '+s.attempted+' / 16 轮；有效 '+s.valid+' 轮；运行错误 '+s.infra_errors+' 轮。';document.getElementById('usage').textContent='累计请求 '+s.budget.requests_made+' / '+s.budget.max_requests+'；累计保守记账 '+s.budget.accounted_cost_cny+' / 10 元。';if(s.started){form.hidden=true;}if(!s.finished)setTimeout(update,2000);}catch(_){status.textContent='本机服务暂时无法读取，已有证据仍保存在研究目录中。';}}update();
</script></body></html>'''


class LaunchState:
    def __init__(self, prepared, output, policy):
        self.prepared, self.output, self.policy = Path(prepared).resolve(), Path(output).resolve(), Path(policy).resolve()
        design = json.loads((self.prepared / "fixed-design.json").read_text(encoding="utf-8"))
        lock = json.loads((self.prepared / "design-sha256.json").read_text(encoding="utf-8"))["sha256"]
        if hashlib.sha256(experiment.canonical(design)).hexdigest() != lock or design != experiment.fixed_design(policy_path=self.policy):
            raise ValueError("准备方案已变化")
        budget = experiment.readonly_budget(self.policy)
        if budget["max_requests"] != 374 or experiment.readiness(design, budget)["status"] != "capacity_check_passed":
            raise ValueError("已授权预算或完整容量检查未通过")
        self.expected_policy_sha256 = budget["policy_sha256"]
        self.ledger = Path(json.loads(self.policy.read_text(encoding="utf-8"))["ledger_path"])
        if self.output.exists():
            raise FileExistsError("已有研究输出，不能覆盖或续跑")
        self.nonce = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.started = self.finished = False
        self.phase = "ready"

    def start(self, key):
        with self.lock:
            if self.started:
                return False
            self.started, self.phase = True, "running"
            worker = threading.Thread(target=self._work, args=(key,), daemon=False)
            worker.start()
        return True

    def _work(self, key):
        previous = os.environ.get("DEEPSEEK_API_KEY")
        try:
            os.environ["DEEPSEEK_API_KEY"] = key
            key = None
            report = experiment.execute(self.prepared, self.output, "llm", policy_path=self.policy, execute_real_model=True)
            self.phase = "finished" if report["status"] == "finished" else "partial"
        except BaseException:
            self.phase = "interrupted"
        finally:
            key = None
            if previous is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = previous
            self.finished = True

    def public(self):
        report_path = self.output / "study-report.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
        except (OSError, ValueError):
            report = {}
        samples = report.get("samples", [])
        return {"started": self.started, "finished": self.finished, "phase": self.phase,
                "attempted": len(samples), "valid": sum(s.get("status") == "valid" for s in samples),
                "infra_errors": sum(s.get("status") == "infra_error" for s in samples),
                "budget": self.read_budget_status(),
                "message": {"ready": "方案已固定，等待在本机填入密钥。", "running": "正在按固定顺序执行。",
                            "finished": "计划已执行完毕，等待独立复核；这不代表每轮都有效或策略优越。",
                            "partial": "研究提前停止，保留已有记录；未运行样本保持缺失。",
                            "interrupted": "执行或采集中断，保留已有文件；不会自动重试。"}[self.phase]}

    def read_budget_status(self):
        # The worker owns process environment during the study. Polling must
        # never temporarily replace its campaign variable or disable the guard.
        with closing(sqlite3.connect(self.ledger.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            rows = list(db.execute("SELECT charged_micros FROM requests ORDER BY sequence"))
            db.rollback()
        if (metadata.get("policy_sha256") != self.expected_policy_sha256 or
                hashlib.sha256(metadata["policy_json"].encode()).hexdigest() != self.expected_policy_sha256):
            raise ValueError("累计政策已变化")
        policy = json.loads(metadata["policy_json"])
        if any(type(row[0]) is not int or row[0] < 0 for row in rows):
            raise ValueError("累计记账无法读取")
        return {"requests_made": len(rows), "max_requests": policy["max_requests"],
                "accounted_cost_cny": format(Decimal(sum(row[0] for row in rows)) / 1_000_000, ".6f")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def allowed(self, post=False):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origins = {"http://" + host for host in hosts}
        return self.headers.get("Host") in hosts and (self.headers.get("Origin") in origins if post
                else self.headers.get("Origin") is None or self.headers.get("Origin") in origins)

    def send(self, code, value, html=False):
        data = value.encode() if html else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        nonce = self.server.state.nonce
        self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.allowed():
            return self.send(403, {"message": "仅允许本机访问"})
        if self.path == "/":
            return self.send(200, PAGE.replace("__NONCE__", self.server.state.nonce), True)
        if self.path == "/api/status":
            try:
                return self.send(200, self.server.state.public())
            except Exception:
                return self.send(503, {"message": "累计台账暂时无法安全读取"})
        return self.send(404, {"message": "页面不存在"})

    def do_POST(self):
        if not self.allowed(post=True):
            return self.send(403, {"message": "仅允许本机页面提交"})
        if self.path != "/api/start":
            return self.send(404, {"message": "入口不存在"})
        value = None
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json" or not 0 < size <= 4096:
                raise ValueError()
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict) or set(value) != {"api_key", "nonce"} or not isinstance(value["nonce"], str):
                raise ValueError()
            if not secrets.compare_digest(value["nonce"], self.server.state.nonce):
                return self.send(403, {"message": "页面已失效，请使用当前本机页面"})
            key = value.pop("api_key")
            if not isinstance(key, str) or not key.strip() or len(key) > 2048:
                raise ValueError()
            started = self.server.state.start(key.strip())
            key = None
            return self.send(202 if started else 409, {"message": "已启动固定研究" if started else "本次研究已启动，不能重复执行"})
        except Exception:
            return self.send(400, {"message": "输入未通过检查，未重新启动研究"})
        finally:
            if isinstance(value, dict):
                value.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    state = LaunchState(args.prepared, args.output, args.policy)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.state = state
    print(json.dumps({"status": "ready", "url": f"http://127.0.0.1:{server.server_port}/", "model_requests_started": 0}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
