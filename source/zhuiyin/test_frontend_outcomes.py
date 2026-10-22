"""Exercise the page's actual display helper against real engine outcomes."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import engine


ROOT = Path(__file__).resolve().parent
BUNDLED_NODE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
NODE = shutil.which("node") or (str(BUNDLED_NODE) if BUNDLED_NODE.is_file() else None)


@unittest.skipUnless(NODE, "Node is optional; install it to run frontend presentation regressions")
class FrontendOutcomeTests(unittest.TestCase):
    def describe(self, step, target):
        script = "const api=require(process.argv[1]);const p=JSON.parse(require('fs').readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(api.describe(p.step,p.target)));"
        completed = subprocess.run([NODE, "-e", script, str(ROOT / "static/outcome.js")],
            input=json.dumps({"step": step, "target": target}), text=True, encoding="utf-8",
            capture_output=True, check=True, timeout=15)
        return json.loads(completed.stdout)

    def test_real_three_fault_result_never_calls_other_business_error_clean(self):
        faults = ["commit_response_lost", "stale_read", "corrupt_payload"]
        reduced = engine.minimize(engine.Config(faults=faults), faults)
        step = next(item for item in reduced["steps"] if item["removed_fault"] == "corrupt_payload")
        outcome = self.describe(step, reduced["failure_fingerprint"])
        self.assertFalse(outcome["sameError"])
        self.assertEqual(outcome["sameErrorLabel"], "未复现")
        self.assertGreater(outcome["counts"]["other_error"], 0)
        self.assertEqual(outcome["businessOutcomeLabel"], "出现其他业务错误")
        self.assertNotIn("无业务错误", outcome["businessOutcomeLabel"])
        self.assertNotEqual(outcome["sameErrorLabel"], "未出错")
        matched = self.describe({"fingerprints": [reduced["failure_fingerprint"]]}, reduced["failure_fingerprint"])
        self.assertEqual(matched["sameErrorLabel"], "已复现")

    def test_real_no_fault_control_displays_no_business_error(self):
        clean = engine.run_trial(engine.Config(faults=[]), [])
        self.assertFalse(clean["failed"])
        outcome = self.describe({"fingerprints": [clean["failure_fingerprint"]], "completed": True}, "TARGET_ERROR")
        self.assertEqual(outcome["counts"]["no_error"], 1)
        self.assertEqual(outcome["sameErrorLabel"], "未复现")
        self.assertEqual(outcome["businessOutcomeLabel"], "无业务错误")

    def test_real_unfinished_execution_is_distinct_from_clean_business_outcome(self):
        with patch.object(engine, "llm_agent", side_effect=RuntimeError("offline synthetic failure")):
            incomplete = engine.run_trial(engine.Config(faults=[]), [], agent_mode="llm")
        outcome = self.describe({"fingerprints": [incomplete["failure_fingerprint"]], "completed": False}, "TARGET_ERROR")
        self.assertEqual(outcome["counts"]["infra_error"], 1)
        self.assertEqual(outcome["counts"]["no_error"], 0)
        self.assertEqual(outcome["sameErrorLabel"], "未复现")
        self.assertEqual(outcome["businessOutcomeLabel"], "运行未完成")

    def test_legacy_report_uses_fingerprints_and_missing_detail_stays_unknown(self):
        inferred = self.describe({"failed": False, "fingerprints": ["DIFFERENT_ERROR", ""]}, "TARGET_ERROR")
        self.assertEqual(inferred["sameErrorLabel"], "未复现")
        self.assertEqual(inferred["counts"], {"same_error": 0, "other_error": 1, "no_error": 1, "infra_error": 0})
        self.assertEqual(inferred["businessOutcomeLabel"], "出现其他业务错误")
        missing = self.describe({"failed": False}, "TARGET_ERROR")
        self.assertEqual(missing["sameErrorLabel"], "未复现")
        self.assertTrue(all(value is None for value in missing["counts"].values()))
        self.assertEqual(missing["businessOutcomeLabel"], "业务结果未知")
        absent_target = self.describe({"fingerprints": ["DIFFERENT_ERROR"]}, None)
        self.assertEqual(absent_target["sameErrorLabel"], "未知")
        self.assertIsNone(absent_target["counts"]["other_error"])
        self.assertEqual(absent_target["businessOutcomeLabel"], "业务结果未知")


if __name__ == "__main__":
    unittest.main()
