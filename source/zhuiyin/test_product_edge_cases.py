"""Independent regression probes for cross-feature product boundaries."""
from pathlib import Path
import shutil
import subprocess
import unittest

from diagnostics import analyze_report


ROOT = Path(__file__).resolve().parent


class ProductEdgeTests(unittest.TestCase):
    def test_legacy_batch_nested_result_remains_diagnosable(self):
        report = {"kind": "batch", "cases": [{"name": "保存的旧批量任务", "result": {
            "config": {"scenario": "equipment", "request_id": "old-1", "faults": []},
            "baseline": {"completed": True, "actual_success": True, "reported_outcome": "success"}}}]}
        result = analyze_report(report)
        self.assertEqual(result["counts"]["completed"], 1)
        self.assertTrue(result["trials"][0]["actual_success"])
        self.assertIn(".result.baseline", result["trials"][0]["path"])

    def test_copy_rule_history_restores_original_template_model_budget(self):
        bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
        node = str(bundled) if bundled.is_file() else shutil.which("node")
        if not node:
            self.skipTest("Node is required for the synthetic DOM regression")
        # Reuse the existing integration harness, changing only the historical
        # fixture to a free rules check whose limit differs from its template.
        script = r"""
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const file=process.argv[1];let source=fs.readFileSync(file,'utf8');
const original="config:{scenario:'equipment',stock:6,quantity:2,request_id:'saved-001'}";
assert(source.includes(original),'Template fixture changed; update this boundary probe');
source=source.replace(original,"config:{scenario:'equipment',stock:6,quantity:2,request_id:'saved-001',agent_mode:'llm',max_requests:22,billing:{max_cost_cny:1,input_cache_miss_price_per_million:3,output_price_per_million:12}}");
source=source.replace("const savedConfig={scenario:","const savedConfig={agent_mode:'rules',scenario:");
source=source.replace("request_id:'saved-001',max_requests:22,billing:","request_id:'saved-001',max_requests:400,billing:");
source=source.replace("applyScenario(value){if(busy)","applyScenario(value,label){if(busy)");
source=source.replace("applied.push(clone(value));","applied.push(clone({...value,_sourceLabel:label}));");
source=source.replace("append(...items){this.children.push(...items);}","after(){}append(...items){this.children.push(...items);}");
source=source.replace("assert.equal(id('max-requests').value,'22');","assert.equal(id('max-requests').value,'22');assert.ok(applied.at(-1)._sourceLabel.includes('已恢复模板原定预算'));");
vm.runInNewContext(source,{require,__dirname:path.dirname(file),console,process,setImmediate});
"""
        completed = subprocess.run([node, "-e", script, str(ROOT / "tools/check_product_workflows.js")],
                                   cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("checks passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
