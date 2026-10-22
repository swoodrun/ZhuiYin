"""Verify product features through a shipped Windows EXE with isolated data.

This checks the frozen backend and its shipped assets. Embedded-window rendering
is covered by check_embedded_desktop.py. All executions here use offline rules.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
import zipfile

from smoke_portable_release import OPENER, wait_job


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("diagnostics.js", "diagnostics.css", "experiment_compare.js", "experiment_compare.css",
          "task_templates.js", "task_templates.css", "product_workflows.js", "product_workflows.css")


def api(port, route, body=None, *, method=None, expected=200):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{route}",
        data=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
        method=method or ("GET" if body is None else "POST"))
    try:
        with OPENER.open(request, timeout=15) as response:
            status, content_type, raw = response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as response:
        status, content_type, raw = response.code, response.headers.get("Content-Type", ""), response.read()
    value = json.loads(raw.decode("utf-8")) if content_type.startswith("application/json") else raw
    assert status == expected, (route, status, value)
    return value


def free_port():
    for port in range(8766, 8776):
        with socket.socket() as candidate:
            try:
                candidate.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free desktop port in 8766..8775")


def extract(archive, target):
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None, "Release archive CRC check failed"
        for item in bundle.infolist():
            path = PurePosixPath(item.filename.replace("\\", "/"))
            assert not path.is_absolute() and ".." not in path.parts and not any(":" in part for part in path.parts), item.filename
            assert "追因数据" not in path.parts and "data" not in path.parts, "Archive contains runtime data"
            destination = (target / Path(*path.parts)).resolve()
            assert destination.is_relative_to(target.resolve()), item.filename
        bundle.extractall(target)


def main():
    parser = argparse.ArgumentParser(description="Check new product workflows in the shipped EXE without model calls")
    parser.add_argument("delivery", type=Path, help="Delivery directory or its portable ZIP")
    parser.add_argument("--output", type=Path, help="Verification JSON path; defaults to the delivery directory")
    args = parser.parse_args()
    delivery = args.delivery.resolve()
    archive = delivery if delivery.is_file() else delivery / "追因-便携版-Windows-x64.zip"
    if delivery.is_file():
        delivery = delivery.parent
    if not archive.is_file():
        raise FileNotFoundError(archive)
    output = args.output.resolve() if args.output else delivery / "产品功能封装实测.json"
    work = ROOT.parent / ".pkg" / ("product-release-check-" + uuid.uuid4().hex[:12])
    work.mkdir(parents=True)
    extracted, data = work / "软件 中文空格目录", work / "独立验证数据"
    extracted.mkdir()
    extract(archive, extracted)
    exe = extracted / "追因" / "追因.exe"
    assert exe.is_file(), exe
    manifest = json.loads((exe.parent / "构建清单.json").read_text(encoding="utf-8"))
    environment = {key: value for key, value in os.environ.items() if key not in {
        "PYTHONHOME", "PYTHONPATH", "ZHUIYIN_DATA_DIR", "ZHUIYIN_CAMPAIGN_POLICY",
        "ZHUIYIN_EMBEDDED_PROBE", "ZHUIYIN_LAUNCHER_PROBE"}}
    environment.update(ZHUIYIN_NO_BROWSER="1", PYINSTALLER_RESET_ENVIRONMENT="1")
    environment["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    process, session, log = None, None, None
    checks = []
    outcome = {"passed": False, "build_id": manifest.get("build_id"), "checks": checks,
               "python": sys.version, "external_model_requests": 0, "test_data": str(work),
               "original_history_modified": False,
               "scope": "Shipped EXE backend, assets and offline product APIs in an isolated data directory; no real model, browser or embedded-window rendering is tested here."}

    def launch():
        nonlocal process, session, log
        port = free_port()
        log = (work / ("server-" + str(time.time_ns()) + ".log")).open("wb")
        process = subprocess.Popen([str(exe), "--serve", "--no-browser", "--data-dir", str(data), "--port", str(port)],
            cwd=os.environ.get("SystemRoot", "C:/Windows"), env=environment,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 40
        record = data / "desktop-session.json"
        while time.monotonic() < deadline:
            code = process.poll()
            assert code is None, f"Frozen backend exited before readiness ({code}, 0x{code & 0xffffffff:08X}); inspect {work}"
            if record.is_file():
                try:
                    current = json.loads(record.read_text(encoding="utf-8"))
                    health = api(current["port"], "/api/health")
                    if current["port"] == port and health.get("desktop", {}).get("build_id") == manifest["build_id"]:
                        session = current
                        return port
                except (ValueError, OSError, AssertionError):
                    pass
            time.sleep(0.1)
        raise AssertionError("Frozen backend did not become ready")

    def stop():
        nonlocal process, session, log
        if session is not None and process is not None and process.poll() is None:
            api(session["port"], "/api/desktop/stop", {"token": session["token"]})
            process.wait(timeout=15)
            assert process.returncode == 0, process.returncode
        elif process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if log:
            log.close()
        process = session = log = None

    def run_rules(port, snapshot):
        launched = api(port, "/api/run", {**snapshot["config"], "agent_mode": "rules", "template_snapshot": snapshot}, expected=202)
        saved = wait_job(port, launched["run_id"])
        assert saved["status"] == "done", saved.get("error")
        assert saved["result"]["model_usage"]["requests"] == 0
        assert saved["result"]["template_snapshot"] == snapshot
        return launched["run_id"], saved

    try:
        port = launch()
        health = api(port, "/api/health")
        assert health.get("product_workbench") == "product-workbench-v1"
        assert all(health.get(key) is True for key in ("diagnostics", "history_comparison", "task_templates"))
        checks.append("new_product_capabilities_from_frozen_backend")
        for asset in ASSETS:
            raw = api(port, "/" + asset)
            assert isinstance(raw, bytes) and raw
            assert hashlib.sha256(raw).hexdigest() == manifest["files"]["static/" + asset], asset
        html = api(port, "/").decode("utf-8")
        assert all(asset in html for asset in ASSETS)
        checks.append("eight_shipped_product_assets_and_main_page_links")

        template_body = {"name": "封装验证专用模板", "description": "独立合成数据，不属于研究样本",
            "config": {"scenario": "equipment", "equipment": "验证相机", "stock": 6, "quantity": 1,
                       "request_id": "SYNTHETIC-PRODUCT-RELEASE-001", "seed": 42,
                       "faults": ["commit_response_lost", "stale_read"], "agent_mode": "rules",
                       "max_requests": 30, "prompt_policy": "neutral"},
            "contract": {"record_count": {"mode": "exactly", "value": 0}, "expected_fields": {}, "stock_delta": None}}
        first_template = api(port, "/api/task-templates", template_body, expected=201)
        template_id = first_template["id"]
        template_path = "/api/task-templates/" + template_id
        assert api(port, template_path) == first_template
        assert api(port, "/api/task-templates")["templates"] == [first_template]
        checks.append("template_create_list_and_read")

        marker = "SYNTHETIC_PRODUCT_CREDENTIAL_NOT_TO_PERSIST"
        rejected = copy.deepcopy(template_body)
        rejected["config"]["api_key"] = marker
        response = api(port, "/api/task-templates", rejected, expected=400)
        assert marker not in json.dumps(response)
        assert api(port, "/api/task-templates")["templates"] == [first_template]
        checks.append("credential_shaped_template_fields_rejected_without_store_change")

        first_id, first = run_rules(port, first_template)
        baseline = first["result"]["baseline"]
        assert baseline["business_failed"] is True
        assert baseline["template_contract"]["status"] == "fail"
        diagnostics = api(port, "/api/runs/" + first_id + "/diagnostics")
        assert diagnostics["schema"] == "diagnostics-v1"
        assert any(row["findings"] and row["events"] for row in diagnostics["trials"])
        assert any(fault["id"] == "commit_response_lost" for row in diagnostics["trials"] for fault in row["activated_faults"])
        checks.append("offline_template_execution_and_linked_diagnostics")

        second_body = copy.deepcopy(template_body)
        second_body.update(name="封装验证模板第二版", expected_version=1)
        second_body["contract"]["record_count"]["value"] = 2
        second_template = api(port, template_path, second_body, method="PUT")
        assert second_template["version"] == 2
        api(port, template_path, {**second_body, "expected_version": 1}, method="PUT", expected=409)
        second_id, second = run_rules(port, second_template)
        assert second["result"]["baseline"]["business_failed"] is True
        assert second["result"]["baseline"]["template_contract"]["status"] == "pass"
        compared = api(port, "/api/compare", {"left_run_id": first_id, "right_run_id": second_id})
        assert compared["read_only"] is True and compared["configuration_differences"]
        assert compared["paired"] and all(item["condition_changed"] for item in compared["paired"])
        assert any(item["strategy"] == "baseline" and "contract_failed" in item["improved_metrics"] for item in compared["paired"])
        checks.append("template_version_update_conflict_and_condition_aware_history_comparison")

        candidates = api(port, "/api/runs/" + first_id + "/retest-plan")["candidates"]
        selected = [item["candidate_id"] for item in candidates if item["eligible"]]
        before_total = api(port, "/api/history")["total"]
        prepared = api(port, "/api/retest/prepare", {"run_id": first_id, "candidate_ids": selected})
        assert api(port, "/api/history")["total"] == before_total
        assert prepared["retest_source"]["unique_tasks"] == 1
        assert prepared["cases"][0]["template_snapshot"] == first_template
        checks.append("retest_preview_preserves_original_template_without_starting_job")

        launched = api(port, "/api/test-suites/start", {"suite_id": prepared["suite_id"], "profile": "quick"}, expected=202)
        retested = wait_job(port, launched["run_id"])
        assert retested["status"] == "done", retested.get("error")
        case = retested["result"]["cases"][0]
        assert case["template_snapshot"] == first_template
        assert len(case["strategies"]) == 4
        assert all(value["template_contract"]["status"] == "fail" for value in case["strategies"].values())
        second_candidates = api(port, "/api/runs/" + launched["run_id"] + "/retest-plan")["candidates"]
        assert second_candidates and all(item["config"]["template_snapshot"] == first_template for item in second_candidates)
        again = api(port, "/api/retest/prepare", {"run_id": launched["run_id"], "candidate_ids": [second_candidates[0]["candidate_id"]]})
        assert again["cases"][0]["template_snapshot"] == first_template
        checks.append("four_strategy_rules_retest_and_second_generation_keep_original_rules")

        stop()
        port = launch()
        assert api(port, template_path) == second_template
        assert api(port, "/api/runs/" + first_id)["result"]["template_snapshot"] == first_template
        assert api(port, "/api/runs/" + launched["run_id"])["result"]["cases"][0]["template_snapshot"] == first_template
        checks.append("templates_history_and_retest_rules_survive_frozen_backend_restart")
        api(port, template_path, {"expected_version": 2}, method="DELETE")
        api(port, template_path, expected=404)
        assert api(port, "/api/task-templates")["templates"] == []
        assert api(port, "/api/runs/" + first_id)["result"]["template_snapshot"] == first_template
        checks.append("template_delete_does_not_rewrite_historical_snapshots")
        stop()
        assert not (data / "desktop-session.json").exists()
        assert not any(marker.encode() in path.read_bytes() for path in data.rglob("*") if path.is_file())
        checks.append("isolated_data_clean_shutdown_and_rejected_credential_not_persisted")
        outcome.update(passed=True, source_run_ids=[first_id, second_id, launched["run_id"]])
    except Exception as exc:
        outcome["error"] = type(exc).__name__ + ": " + str(exc)
        traceback.print_exc()
    finally:
        try:
            stop()
        except Exception as exc:
            outcome.update(passed=False, cleanup_error=type(exc).__name__ + ": " + str(exc))
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            if log:
                log.close()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in outcome.items() if key != "checks"}, ensure_ascii=False))
    print("Product checks passed: " + str(len(checks)))
    return 0 if outcome["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
