"""An attempted, unfinished model baseline survives restart and export."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import engine
import server
from test_experiment_budget import SyntheticModel, config


class IncompleteBaselineStorageTests(unittest.TestCase):
    def test_worker_retains_partial_events_database_usage_and_no_credential(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server, "DATA", Path(directory)), \
                patch.object(server, "JOBS", {}), patch.object(server, "STOP_EVENTS", {}), \
                patch.object(server, "STORES", {}):
            cfg = config(max_requests=1)
            cfg.model_config["api_key"] = "SYNTHETIC_UNFINISHED_PRIVATE"
            transport = SyntheticModel()
            run_id = server.create_job(config=cfg.public())
            with patch("engine.urllib.request.build_opener", return_value=transport):
                server.worker(run_id, cfg)
            self.assertEqual(len(transport.requests), 1)
            self.assertEqual(cfg.model_config, {})
            server.JOBS.clear()
            job = server.get_job(run_id)
            result = job["partial_result"]
            self.assertEqual(job["status"], "error")
            self.assertEqual(result["status"], "partial")
            self.assertIsNone(result["baseline"]["failed"])
            self.assertFalse(result["baseline"]["completed"])
            self.assertTrue(result["baseline"]["events"])
            self.assertEqual(result["model_usage"]["requests"], 1)
            self.assertTrue(result["fixed"]["skipped"])
            self.assertEqual(result["minimization"]["trials"], 0)
            self.assertTrue((Path(directory) / run_id / "baseline.sqlite").is_file())
            saved = (Path(directory) / run_id / "report.json").read_text(encoding="utf-8")
            self.assertNotIn("SYNTHETIC_UNFINISHED_PRIVATE", saved + json.dumps(job))
            self.assertEqual(json.loads(saved)["model_usage"]["requests"], 1)


if __name__ == "__main__":
    unittest.main()
