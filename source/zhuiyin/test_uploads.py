"""Upload and batch behavior checks. No external model credentials are required."""

from contextlib import closing, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import engine
from ingest import parse_upload


def scenario(number=1, **changes):
    config = {
        "equipment": "单反相机", "stock": 3, "quantity": 1,
        "request_id": f"upload-{number}", "seed": 17,
        "faults": ["commit_response_lost", "stale_read"],
    }
    config.update(changes)
    return {"name": f"自定义场景 {number}", "config": config}


def trace():
    """A non-device workflow; detection must use evidence, not tool names."""
    request = "ticket-request-001"
    return {
        "kind": "trace", "name": "活动票务日志",
        "task": {"request_id": request, "goal": "只生成一张有效票"},
        "contracts": {"max_active_effects_per_request": 1},
        "events": [
            {"step": 1, "tool": "create_ticket_v9", "observed": {"ok": False, "code": "COMMIT_UNKNOWN"}, "actual": {"committed": True, "ticket_id": "ticket-A"}},
            {"step": 2, "tool": "lookup_ticket_cache", "observed": {"tickets": []}, "actual": {"tickets": [{"id": "ticket-A", "request_id": request}]}},
            {"step": 3, "tool": "create_ticket_v9", "observed": {"ok": True}, "actual": {"committed": True, "ticket_id": "ticket-B"}},
            {"step": 4, "tool": "say_result", "kind": "report", "observed": {"success": True}},
        ],
        "final_state": {"effects": [
            {"effect_id": "ticket-A", "request_id": request, "status": "active"},
            {"effect_id": "ticket-B", "request_id": request, "status": "active"},
        ]},
    }


class UploadParsingTests(unittest.TestCase):
    def json_upload(self, value):
        return parse_upload("自定义.json", json.dumps(value, ensure_ascii=False, indent=2))

    def test_utf8_bom_and_chinese_csv_columns(self):
        data = "\ufeff设备名称,库存,借用数量,请求编号,种子,故障\r\n单反相机,100,2,中文编号,17,提交后响应丢失;读取过期状态\r\n"
        result = parse_upload("我的场景.csv", data)
        self.assertEqual(result["kind"], "scenarios")
        config = result["scenarios"][0]["config"]
        self.assertEqual(config["equipment"], "单反相机")
        self.assertEqual(config["stock"], 100)
        self.assertEqual(config["quantity"], 2)
        self.assertEqual(config["faults"], ["commit_response_lost", "stale_read"])
        self.assertEqual(len(result["source_sha256"]), 64)

    def test_csv_blank_faults_mean_no_injected_faults(self):
        result = parse_upload("无故障.csv", "equipment,stock,quantity,request_id\n相机,0,1,empty-stock\n")
        config = result["scenarios"][0]["config"]
        self.assertEqual(config["faults"], [])
        self.assertEqual(config["stock"], 0)

    def test_bad_csv_rows_report_the_source_line(self):
        contents = [
            "equipment,stock,quantity,request_id\n相机,3,1,ok\n相机,not-number,1,bad\n",
            "equipment,stock,quantity,request_id\n相机,3,1,ok\n相机,3,1,bad,extra\n",
            "equipment,stock,quantity,request_id\n相机,3,1,same\n相机,3,1,same\n",
        ]
        for content in contents:
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "第 3 行"):
                parse_upload("坏行.csv", content)

    def test_empty_and_oversized_files_are_rejected_by_utf8_byte_size(self):
        for content in ("", " \n\t", "相" * (2 * 1024 * 1024 // 3 + 1)):
            with self.subTest(size=len(content)), self.assertRaises(ValueError):
                parse_upload("data.json", content)

    def test_bad_json_types_and_duplicate_keys_are_rejected(self):
        for content in ("null", "true", '"text"', "123", "[1]", '{"kind":"scenarios","scenarios":{}}', '{"events":[],"events":[]}', '{"events":[],"seed":NaN}'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_upload("bad.json", content)

    def test_invalid_kind_types_are_rejected_with_a_source_line(self):
        for kind in ([], {}, None, True, 1):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "第 [0-9]+ 行"):
                self.json_upload({"kind": kind, "events": []})
        for kind in ([], {}, None, True, 1):
            content = '{"type":"meta","task":{"request_id":"x"}}\n' + json.dumps({"type": kind, "event": {"tool": "ticket"}})
            with self.subTest(jsonl_type=kind), self.assertRaisesRegex(ValueError, "第 2 行"):
                parse_upload("bad-kind.jsonl", content)

    def test_twenty_json_scenarios_allowed_but_twenty_one_rejected(self):
        result = self.json_upload({"kind": "scenarios", "scenarios": [scenario(i) for i in range(20)]})
        self.assertEqual(len(result["scenarios"]), 20)
        with self.assertRaises(ValueError):
            self.json_upload({"kind": "scenarios", "scenarios": [scenario(i) for i in range(21)]})

    def test_csv_twentieth_row_allowed_and_twenty_first_points_to_line_twenty_two(self):
        header = "equipment,stock,quantity,request_id\n"
        rows = [f"相机,3,1,csv-{i}\n" for i in range(21)]
        result = parse_upload("twenty.csv", header + "".join(rows[:20]))
        self.assertEqual(len(result["scenarios"]), 20)
        with self.assertRaisesRegex(ValueError, "第 22 行"):
            parse_upload("twenty-one.csv", header + "".join(rows))

    def test_long_valid_imported_names_can_be_submitted_to_batch(self):
        import server

        value = {"kind": "scenario", **scenario()}
        value["name"] = "自定义" * 40
        parsed = self.json_upload(value)
        entries, _ = server.validate_batch({"scenarios": parsed["scenarios"]})
        self.assertEqual(entries[0]["name"], value["name"])
        csv = "equipment,stock,quantity,request_id\n" + "设" * 80 + ",3,1," + "r" * 80 + "\n"
        parsed = parse_upload("long.csv", csv)
        entries, _ = server.validate_batch({"scenarios": parsed["scenarios"]})
        self.assertEqual(len(entries), 1)

    def test_csv_field_upper_bounds_keep_business_values_and_fit_batch_labels(self):
        import server

        cases = [
            ("equipment", "equipment,stock,quantity,request_id\n", "设" * 80 + ",1000000,1000000," + "r" * 80 + "\n"),
            ("ticket", "scenario,ticket_title,ticket_priority,request_id\n", "ticket," + "题" * 200 + ",urgent," + "r" * 80 + "\n"),
        ]
        for business, header, row in cases:
            with self.subTest(business=business):
                parsed = parse_upload("upper-bounds.csv", header + row)
                item = parsed["scenarios"][0]
                self.assertLessEqual(len(item["name"]), 200)
                entries, _ = server.validate_batch({"scenarios": parsed["scenarios"]})
                config = entries[0]["config"]
                self.assertEqual(config.request_id, "r" * 80)
                if business == "ticket":
                    self.assertEqual(config.ticket_title, "题" * 200)
                    self.assertEqual(len(item["name"]), 200)
                    self.assertTrue(item["name"].endswith("…"))
                else:
                    self.assertEqual(config.equipment, "设" * 80)
                    self.assertEqual((config.stock, config.quantity), (1000000, 1000000))
                    self.assertEqual(item["name"], "设" * 80 + " · " + "r" * 80)

    def test_duplicate_request_ids_and_control_fields_are_rejected(self):
        invalid = [
            {"kind": "scenarios", "scenarios": [scenario(1), scenario(1)]},
            {"kind": "scenario", **scenario(1, model_config={"api_key": "SYNTHETIC_SECRET"})},
            {"kind": "scenario", **scenario(1, agent_mode="llm")},
            {"kind": "scenario", **scenario(1, sqlite_dump="DROP TABLE inventory;")},
            {"kind": "scenario", **scenario(1, python="print('execute')")},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.json_upload(value)

    def test_arbitrary_ticket_tool_names_detect_duplicate_final_effects(self):
        result = self.json_upload(trace())
        self.assertEqual(result["kind"], "trace")
        analysis = result["analyses"][0]
        self.assertEqual(analysis["verdict"], "violation")
        codes = {finding["code"] for finding in analysis["findings"]}
        self.assertTrue({"AMBIGUOUS_COMMIT", "STALE_READ", "DUPLICATE_EFFECT", "REPORT_MISMATCH"} <= codes)
        self.assertEqual(analysis["stats"]["events"], 4)

    def test_unknown_effect_status_is_rejected_instead_of_silently_inactive(self):
        for status in ("pending", "ACTIVE", "已完成", [], None):
            value = trace()
            value["final_state"]["effects"][1]["status"] = status
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "第 [0-9]+ 行"):
                self.json_upload(value)

    def test_cancelled_effect_is_not_counted_as_an_active_duplicate(self):
        value = trace()
        value["final_state"]["effects"][1]["status"] = "cancelled"
        analysis = self.json_upload(value)["analyses"][0]
        self.assertEqual(analysis["verdict"], "no_violation_found")
        self.assertNotIn("DUPLICATE_EFFECT", {finding["code"] for finding in analysis["findings"]})

    def test_observed_only_logs_are_inconclusive(self):
        result = self.json_upload({"kind": "trace", "task": {"request_id": "one"}, "events": [{"tool": "ticket", "observed": {"ok": True, "ticket_id": "A"}}]})
        analysis = result["analyses"][0]
        self.assertEqual(analysis["verdict"], "inconclusive")
        self.assertTrue(any("静态" in text for text in analysis["limitations"]))

    def test_historical_duplicates_without_final_state_are_not_called_confirmed(self):
        value = trace()
        value.pop("final_state")
        analysis = self.json_upload(value)["analyses"][0]
        self.assertEqual(analysis["verdict"], "inconclusive")
        codes = {finding["code"] for finding in analysis["findings"]}
        self.assertIn("POSSIBLE_DUPLICATE_EFFECT", codes)
        self.assertNotIn("DUPLICATE_EFFECT", codes)

    def test_jsonl_meta_event_and_final_records(self):
        value = trace()
        lines = [{"type": "meta", "name": value["name"], "task": value["task"], "contracts": value["contracts"]}]
        lines.extend({"type": "event", "event": event} for event in value["events"])
        lines.append({"type": "final_state", "final_state": value["final_state"]})
        content = "\ufeff" + "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)
        result = parse_upload("ticket.jsonl", content)
        self.assertEqual(result["analyses"][0]["verdict"], "violation")

    def test_bad_jsonl_event_type_reports_line(self):
        content = '{"type":"meta","task":{"request_id":"x"}}\n{"type":"event","event":{"tool":"ticket","observed":"not-object"}}\n'
        with self.assertRaisesRegex(ValueError, "第 2 行"):
            parse_upload("bad.jsonl", content)

    def test_trace_credentials_are_recursively_redacted(self):
        value = trace()
        value["events"][0]["observed"]["headers"] = {"Authorization": "Bearer SYNTHETIC_SECRET"}
        value["events"][0]["actual"]["nested"] = {"api_key": "SYNTHETIC_SECRET", "model_config": {"token": "SYNTHETIC_SECRET"}}
        result = self.json_upload(value)
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("SYNTHETIC_SECRET", rendered)
        self.assertIn("已遮蔽", rendered)

    def test_header_name_value_credentials_are_masked_without_touching_business_pairs(self):
        value = trace()
        value["events"][0]["observed"]["response_headers"] = [
            {"name": "Authorization", "value": "SYNTHETIC_HEADER_SECRET"},
            {"name": "X-Trace-ID", "value": "public-trace-id"},
        ]
        value["events"][0]["actual"]["fields"] = [{"name": "Authorization", "value": "business-field"}]
        parsed = self.json_upload(value)
        event = parsed["analyses"][0]["events"][0]
        self.assertNotIn("SYNTHETIC_HEADER_SECRET", json.dumps(parsed))
        self.assertEqual(event["observed"]["response_headers"][1]["value"], "public-trace-id")
        self.assertEqual(event["actual"]["fields"][0]["value"], "business-field")
        self.assertEqual(len(parsed["warnings"]), 1)

    def test_legacy_report_is_compatible_without_sql_execution_or_key_echo(self):
        config = scenario()["config"]
        request = config["request_id"]
        old = {
            "config": {**config, "agent_mode": "llm", "model_config": {"api_key": "SYNTHETIC_SECRET"}},
            "baseline": {"reservations": [
                {"id": 1, "request_id": request, "quantity": 1, "status": "active"},
                {"id": 2, "request_id": request, "quantity": 1, "status": "active"},
            ], "events": [{"step": 1, "tool": "custom", "actual": {"committed": True}}], "reported_success": True, "actual_success": False,
                "sqlite_dump": "SELECT load_extension('DO_NOT_EXECUTE');"},
            "fixed": {"reservations": [{"id": 1, "request_id": request, "quantity": 1, "status": "active"}], "events": [{"step": 1, "tool": "custom", "actual": {"committed": True}}], "reported_success": True, "actual_success": True},
        }
        with patch("engine.sqlite3.connect", side_effect=AssertionError("Uploaded SQL must not execute")), patch("engine.model_request", side_effect=AssertionError("Upload must not call a model")):
            result = self.json_upload(old)
        self.assertEqual(result["kind"], "trace")
        self.assertEqual(len(result["analyses"]), 2)
        self.assertEqual(result["analyses"][0]["verdict"], "violation")
        self.assertEqual(result["scenario"]["agent_mode"], "rules")
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
        self.assertNotIn("load_extension", json.dumps(result))

    def test_uploaded_code_is_data_and_cannot_execute(self):
        value = trace()
        value["events"][0]["observed"]["code_to_run"] = "__import__('builtins').print('DO_NOT_EXECUTE')"
        with patch("builtins.eval", side_effect=AssertionError("Uploaded code must not execute")), patch("engine.model_request", side_effect=AssertionError("Upload must not call a model")), patch("engine.sqlite3.connect", side_effect=AssertionError("Upload must not open a database")):
            result = self.json_upload(value)
        self.assertEqual(result["analyses"][0]["verdict"], "violation")


class BatchValidationTests(unittest.TestCase):
    def test_scenarios_are_validated_and_source_path_is_only_a_label(self):
        import server

        entries, source = server.validate_batch({
            "scenarios": [scenario(1), scenario(2)],
            "source": {"filename": "../../我的场景.csv", "sha256": "a" * 64},
        })
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(isinstance(entry["config"], engine.Config) for entry in entries))
        self.assertTrue(all(entry["config"].agent_mode == "rules" for entry in entries))
        self.assertEqual(source["filename"], "我的场景.csv")

    def test_invalid_rows_are_rejected_before_batch_execution(self):
        import server

        cases = [
            {"scenarios": []},
            {"scenarios": [scenario(i) for i in range(21)]},
            {"scenarios": [scenario(1), scenario(1)]},
            {"scenarios": [scenario(1), scenario(2, stock=-1)]},
            {"scenarios": [scenario(1, agent_mode="llm")]},
            {"scenarios": [scenario(1, model_config={"api_key": "SYNTHETIC_SECRET"})]},
            {"scenarios": [scenario(1, sqlite_dump="SELECT 1")]},
            {"scenarios": [scenario(1)], "source": {"sha256": "invalid"}},
        ]
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ValueError):
                server.validate_batch(body)

    def test_twenty_valid_rows_are_allowed(self):
        import server

        entries, _ = server.validate_batch({"scenarios": [scenario(i) for i in range(20)]})
        self.assertEqual(len(entries), 20)

    def test_real_stock_range_includes_zero_and_values_above_demo_limit(self):
        import server

        entries, _ = server.validate_batch({"scenarios": [scenario(1, stock=0), scenario(2, stock=100, quantity=12)]})
        self.assertEqual([entry["config"].stock for entry in entries], [0, 100])
        for entry in entries:
            config = entry["config"]
            result = engine.run_trial(config, config.faults, strategy="verified")
            self.assertFalse(result["failed"])
            self.assertEqual(result["stock_remaining"], 0 if config.stock == 0 else 88)
            self.assertEqual(result["reported_success"], config.stock > 0)


class BatchWorkerTests(unittest.TestCase):
    def setUp(self):
        import server

        self.server = server
        self.workspace = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(prefix="test-uploads-", dir=self.workspace)
        self.data = Path(self.temporary.name)
        # Verify the resolved target before TemporaryDirectory's recursive cleanup.
        self.assertEqual(self.data.resolve().parent, self.workspace)
        self.jobs = {}
        self.stops = {}
        self.patches = [patch.object(server, "DATA", self.data), patch.object(server, "JOBS", self.jobs), patch.object(server, "STOP_EVENTS", self.stops)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def prepare(self, run_id, rows=None):
        self.jobs[run_id] = {"run_id": run_id, "status": "running", "events": []}
        self.stops[run_id] = threading.Event()
        return self.server.validate_batch({"scenarios": rows or [scenario(1), scenario(2)]})

    def test_each_row_and_repeated_batch_have_independent_databases(self):
        with patch("engine.model_request") as model:
            for run_id in ("batch-one", "batch-two"):
                entries, source = self.prepare(run_id)
                self.server.batch_worker(run_id, entries, source)
                job = self.jobs[run_id]
                self.assertEqual(job["status"], "done", job.get("error"))
                result = job["result"]
                self.assertEqual(result["completed"], 2)
                self.assertEqual(result["summary"], {"baseline_failures": 2, "fixed_failures": 0})
                artifact_urls = set()
                for index, case in enumerate(result["cases"], 1):
                    trial = case["result"]
                    self.assertEqual(trial["baseline"]["stock_remaining"], 1)
                    self.assertEqual(trial["fixed"]["stock_remaining"], 2)
                    self.assertEqual(trial["model_usage"]["requests"], 0)
                    for strategy, expected_records in (("baseline", 2), ("fixed", 1)):
                        artifact = trial["artifacts"][strategy]
                        self.assertNotIn(artifact, artifact_urls)
                        artifact_urls.add(artifact)
                        path = self.data / run_id / f"case-{index:02d}-{strategy}.sqlite"
                        with closing(sqlite3.connect(path)) as db:
                            count = db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
                            request_id = db.execute("SELECT request_id FROM reservations LIMIT 1").fetchone()[0]
                        self.assertEqual(count, expected_records)
                        self.assertEqual(request_id, entries[index - 1]["config"].request_id)
                with zipfile.ZipFile(self.data / run_id / "artifacts.zip") as bundle:
                    self.assertEqual(set(bundle.namelist()), {"report.json"} | {
                        f"case-{i:02d}-{strategy}.sqlite" for i in (1, 2)
                        for strategy in ("baseline", "fixed", "idempotency_only", "verification_only")})
                self.assertEqual(json.loads((self.data / run_id / "report.json").read_text(encoding="utf-8"))["completed"], 2)
            model.assert_not_called()

    def test_cancelled_batch_preserves_only_finished_rows(self):
        entries, source = self.prepare("cancelled")
        original = self.server.run_experiment
        completed = 0

        def execute(*args, **kwargs):
            nonlocal completed
            result = original(*args, **kwargs)
            completed += 1
            if completed == 1:
                self.stops["cancelled"].set()
            return result

        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(self.server, "run_experiment", side_effect=execute):
            self.server.batch_worker("cancelled", entries, source)
        job = self.jobs["cancelled"]
        self.assertEqual(job["status"], "error")
        self.assertTrue(job["partial_result"]["cancelled"])
        self.assertEqual(job["partial_result"]["completed"], 1)
        self.assertTrue((self.data / "cancelled" / "case-01-fixed.sqlite").is_file())
        self.assertFalse((self.data / "cancelled" / "case-02-baseline.sqlite").exists())
        self.assertEqual(json.loads((self.data / "cancelled" / "report.json").read_text(encoding="utf-8"))["completed"], 1)
        self.assertIn("RuntimeError: 用户已停止批量验证", stderr.getvalue())

    def test_row_error_preserves_finished_rows_and_reports_partial_status(self):
        entries, source = self.prepare("row-error", [scenario(1), scenario(2, equipment="故障样例")])
        original = self.server.run_experiment

        def execute(config, *args, **kwargs):
            if config.equipment == "故障样例":
                raise RuntimeError("合成的场景运行错误")
            return original(config, *args, **kwargs)

        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(self.server, "run_experiment", side_effect=execute):
            self.server.batch_worker("row-error", entries, source)
        job = self.jobs["row-error"]
        self.assertEqual(job["status"], "error")
        self.assertFalse(job["partial_result"]["cancelled"])
        self.assertEqual(job["partial_result"]["completed"], 1)
        self.assertIn("批量实验运行中断", job["error"])
        self.assertNotIn("合成的场景运行错误", json.dumps(job, ensure_ascii=False))
        self.assertIn("RuntimeError: 合成的场景运行错误", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
