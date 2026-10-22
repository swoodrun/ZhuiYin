"""Keep plain-language reports honest and historical fault input compatible."""
import json
import unittest
from unittest.mock import patch

import engine
import ingest
import study


class PlainReportCopyTests(unittest.TestCase):
    def test_rules_scope_sentence_matches_between_experiment_and_statistics(self):
        config = engine.Config(faults=[])
        experiment = engine.run_experiment(config)
        statistics = study.run_study(config, {"repetitions": 2, "strategies": ["baseline"]})
        self.assertEqual(experiment["limitations"][0], statistics["limitations"][0])
        self.assertIn("本地模拟", experiment["limitations"][0])
        self.assertIn("真实模型", experiment["limitations"][0])

    def test_rules_interrupt_messages_describe_the_main_trial(self):
        config = engine.Config(faults=[])
        with self.assertRaisesRegex(RuntimeError, "主试验未开始") as cancelled:
            engine.run_experiment(config, cancel_check=lambda: True)
        self.assertNotIn("模型", str(cancelled.exception))

        incomplete = {"failure_fingerprint": "AGENT_DID_NOT_FINISH",
                      "violations": [{"message": "助手没有提交明确结果"}]}
        for cancel_after_trial in (False, True):
            with self.subTest(cancel_after_trial=cancel_after_trial):
                checks = iter((False, cancel_after_trial))
                with patch.object(engine, "run_trial", return_value=incomplete):
                    with self.assertRaisesRegex(RuntimeError, "主试验未完成") as stopped:
                        engine.run_experiment(config, cancel_check=lambda: next(checks))
                self.assertNotIn("模型", str(stopped.exception))

    def test_old_chinese_fault_names_still_import_as_the_same_ids(self):
        old_names = [
            "提交成功后响应丢失", "恢复查询返回过期结果", "响应缺少非关键字段", "额外工具延迟",
            "首次写入前超时（未提交）", "首次返回成功但未提交", "提交时写入错误业务字段",
        ]
        for names in (old_names, list(engine.FAULTS.values())):
            parsed = ingest.parse_upload("scene.json", json.dumps({
                "kind": "scenario", "config": {"faults": names},
            }, ensure_ascii=False))
            self.assertEqual(parsed["scenarios"][0]["config"]["faults"], list(engine.FAULTS))
        self.assertEqual({key: value["label"] for key, value in engine.STRATEGIES.items()}, study.STRATEGIES)

    def test_statistics_keep_selection_denominator_and_uncertainty_explanations(self):
        report = study.run_study(engine.Config(faults=[]), {"repetitions": 2, "strategies": ["baseline"]})
        self.assertTrue(all(isinstance(item, str) for item in report["limitations"]))
        text = "\n".join(report["limitations"])
        self.assertIn("本地模拟", text)
        self.assertIn("真实模型", text)
        self.assertRegex(text, r"运行错误.+不计入分母")
        self.assertRegex(text, r"第一轮自身不计入")
        self.assertRegex(text, r"成功则不统计.+不会改选")
        self.assertRegex(text, r"Wilson 95%.+未做显著性检验")
        self.assertIn("轮次相关", text)
        self.assertIn("模型服务变化", text)
        self.assertRegex(text, r"种子相同.+输出仍可能不同")
        self.assertRegex(text, r"缺失按未知处理")
        self.assertRegex(text, r"缓存、推理 token.+需另算")
        self.assertIsNone(report["arms"][0]["reproduction"]["rate"])
        self.assertIsNone(report["model_usage"]["estimated_cost"])

    def test_single_deletion_result_keeps_local_minimality_scope(self):
        faults = ["commit_response_lost", "stale_read"]
        result = engine.minimize(engine.Config(faults=faults), faults)
        self.assertTrue(result["one_minimal"])
        self.assertIn("单独删除", result["reason"])
        self.assertIn("更少的组合", result["reason"])
        self.assertIn("因果", result["reason"])
        self.assertNotIn("全局最少", result["reason"])

    def test_upload_checks_keep_declaration_origin_and_nonexecution_limits(self):
        report = ingest.parse_upload("trace.json", json.dumps({
            "kind": "trace", "task": {"request_id": "r-1"},
            "events": [{"step": 1, "tool": "create", "observed": {"ok": True},
                        "actual": {"committed": True}}],
            "final_state": {"effects": [{"effect_id": "e-1", "request_id": "r-1", "status": "active"}]},
        }))
        analysis = report["analyses"][0]
        self.assertEqual(analysis["verdict"], "no_violation_found")
        self.assertTrue(all(isinstance(item, str) for item in analysis["limitations"]))
        text = "\n".join(analysis["limitations"])
        self.assertRegex(text, r"actual.+上传者.+未经.+独立验证")
        self.assertRegex(text, r"摘要.+识别.+来源、完整性和真实性需另外确认")
        self.assertRegex(text, r"工具、SQL 和代码都不会执行")
        self.assertRegex(text, r"业务正确性与因果关系需另外验证")


if __name__ == "__main__":
    unittest.main()
