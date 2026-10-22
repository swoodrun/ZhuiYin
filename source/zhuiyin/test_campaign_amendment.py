"""Offline tests for the single authorized cumulative-budget amendment."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import model_budget
from tools import amend_campaign_budget as amend


class CampaignAmendmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.policy = self.source / "campaign-policy.json"
        self.ledger = self.source / "campaign-ledger.sqlite"
        official_raw = json.loads(amend.DEFAULT_SOURCE_POLICY.read_text(encoding="utf-8"))
        self.frozen = {amend.DEFAULT_SOURCE_POLICY: amend.digest(amend.DEFAULT_SOURCE_POLICY),
                       Path(official_raw["ledger_path"]): amend.digest(official_raw["ledger_path"])}
        shutil.copy2(official_raw["ledger_path"], self.ledger)
        self.raw = dict(official_raw, ledger_path=str(self.ledger))
        self._write_policy()
        self.output = self.root / "amended"

    def tearDown(self):
        for path, digest in self.frozen.items():
            self.assertEqual(amend.digest(path), digest, "frozen original was changed")
        self.temp.cleanup()

    def _write_policy(self):
        self.policy.write_text(json.dumps(self.raw), encoding="utf-8")

    def _snapshot(self):
        return {path: amend.digest(path) for path in (self.policy, self.ledger)}

    def _unchanged(self, snapshot):
        self.assertEqual(self._snapshot(), snapshot)

    def _mutate(self, sql):
        with closing(sqlite3.connect(self.ledger)) as db:
            db.execute(sql)
            db.commit()

    def _assert_rejected(self):
        before = self._snapshot()
        with self.assertRaises((ValueError, model_budget.CampaignBudgetError)):
            amend.amend_campaign_budget(self.output, source_policy=self.policy)
        self._unchanged(before)
        self.assertFalse(self.output.exists())

    def test_copies_all_old_requests_only_changes_authorized_limit_and_metadata(self):
        before = self._snapshot()
        result = amend.amend_campaign_budget(self.output, source_policy=self.policy)
        self._unchanged(before)
        self.assertEqual(result["authorization"]["text"], "批准提高至374次，费用仍累计不超过10元")
        self.assertEqual(result["new_model_requests"], 0)
        self.assertEqual(result["before"]["requests_remaining"], 130)
        self.assertEqual(result["after"]["requests_remaining"], 144)
        self.assertEqual(result["after"]["max_requests"], 374)
        self.assertEqual(result["after"]["max_cost_cny"], "10")
        self.assertEqual(result["after"]["requests_made"], 230)
        self.assertEqual(result["after"]["accounted_cost_cny"], "0.612582")
        self.assertEqual(result["after"]["remaining_cost_cny"], "9.387418")
        self.assertEqual(result["preservation"]["sqlite_sequence_before"], [["requests", 230]])
        self.assertEqual(result["preservation"]["sqlite_sequence_after"], [["requests", 230]])
        self.assertTrue(result["preservation"]["requests_equal"])
        new_raw = json.loads((self.output / "campaign-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(new_raw, dict(self.raw, max_requests=374,
                                      ledger_path=str(self.output / "campaign-ledger.sqlite")))
        with closing(amend._readonly(self.ledger)) as old, closing(amend._readonly(self.output / "campaign-ledger.sqlite")) as new:
            old_meta, old_rows, old_sequence = amend._read_database(old)
            new_meta, new_rows, new_sequence = amend._read_database(new)
        self.assertEqual(old_rows, new_rows)
        self.assertEqual(old_sequence, new_sequence)
        self.assertEqual(new_meta["halted_reason"], old_meta["halted_reason"])
        self.assertEqual(set(key for key in old_meta if old_meta[key] != new_meta[key]),
                         {"policy_json", "policy_sha256"})
        with amend._policy_environment(self.output / "campaign-policy.json"):
            summary = model_budget.public_summary()
        self.assertEqual(summary["requests_made"], 230)
        self.assertEqual(summary["requests_remaining"], 144)
        self.assertEqual(summary["accounted_cost_cny"], "0.612582")
        self.assertEqual(json.loads((self.output / "amendment.json").read_text(encoding="utf-8")), result)

    def test_guard_accepts_374_rejects_375_and_cost_over_10(self):
        self.assertEqual(model_budget.MAX_CAMPAIGN_REQUESTS, 374)
        self.raw["max_requests"] = 374
        self._write_policy()
        self.assertEqual(amend._load_policy(self.policy).max_requests, 374)
        for field, value in [("max_requests", 375), ("max_cost_cny", "10.000001")]:
            raw = dict(self.raw)
            self.raw[field] = value
            self._write_policy()
            with self.assertRaises(model_budget.CampaignBudgetError):
                amend._load_policy(self.policy)
            self.raw = raw

    def test_amendment_rejects_outside_authorized_target_without_side_effects(self):
        before = self._snapshot()
        for limit in [375, 360, 0, True, "374"]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                amend.amend_campaign_budget(self.output, source_policy=self.policy, max_requests=limit)
            self.assertFalse(self.output.exists())
            self._unchanged(before)

    def test_rejects_wrong_source_policy_identity_pricing_and_cost(self):
        original = dict(self.raw)
        for field, value in [("max_requests", 359), ("allowed_model", "wrong"),
                             ("allowed_origin", "https://example.invalid"),
                             ("input_price_per_million", "2"), ("output_price_per_million", "11"),
                             ("max_cost_cny", "9"), ("max_cost_cny", "11")]:
            with self.subTest(field=field, value=value):
                self.raw = dict(original, **{field: value})
                self._write_policy()
                self._assert_rejected()

    def test_rejects_modified_request_even_when_all_totals_unchanged(self):
        self._mutate("UPDATE requests SET reservation_id='different-record' WHERE sequence=1")
        self._assert_rejected()

    def test_rejects_removed_history_and_altered_autoincrement_sequence(self):
        self._mutate("UPDATE sqlite_sequence SET seq=229 WHERE name='requests'")
        self._assert_rejected()
        self._mutate("UPDATE sqlite_sequence SET seq=230 WHERE name='requests'")
        self._mutate("DELETE FROM requests WHERE sequence=230")
        self._assert_rejected()

    def test_rejects_reserved_unknown_and_overrun_states(self):
        for state in ["reserved", "unknown", "overrun"]:
            self._mutate("UPDATE requests SET state='" + state + "' WHERE sequence=1")
            with self.subTest(state=state):
                self._assert_rejected()
        self._mutate("UPDATE requests SET state='settled' WHERE sequence=1")

    def test_rejects_halted_or_policy_mismatching_ledger(self):
        self._mutate("UPDATE metadata SET value='usage_bound_exceeded' WHERE key='halted_reason'")
        self._assert_rejected()
        self._mutate("UPDATE metadata SET value='' WHERE key='halted_reason'")
        self._mutate("UPDATE metadata SET value='wrong' WHERE key='policy_sha256'")
        self._assert_rejected()

    def test_rejects_active_wal_without_touching_any_source(self):
        wal = Path(str(self.ledger) + "-wal")
        wal.write_bytes(b"active-source-marker")
        self._assert_rejected()
        self.assertEqual(wal.read_bytes(), b"active-source-marker")

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("untouched", encoding="utf-8")
        before = self._snapshot()
        with self.assertRaisesRegex(ValueError, "禁止覆盖"):
            amend.amend_campaign_budget(self.output, source_policy=self.policy)
        self.assertEqual(list(self.output.iterdir()), [marker])
        self.assertEqual(marker.read_text(encoding="utf-8"), "untouched")
        self._unchanged(before)

    def test_repeated_amendment_is_rejected_and_completed_output_retained(self):
        amend.amend_campaign_budget(self.output, source_policy=self.policy)
        hashes = {path.name: amend.digest(path) for path in self.output.iterdir()}
        with self.assertRaises(ValueError):
            amend.amend_campaign_budget(self.output, source_policy=self.policy)
        self.assertEqual({path.name: amend.digest(path) for path in self.output.iterdir()}, hashes)

    def test_failed_write_cleans_fresh_output_and_preserves_input(self):
        before = self._snapshot()
        with patch.object(amend, "_write_json", side_effect=OSError("test write fault")):
            with self.assertRaisesRegex(OSError, "test write fault"):
                amend.amend_campaign_budget(self.output, source_policy=self.policy)
        self._unchanged(before)
        self.assertFalse(self.output.exists())

    def test_failed_metadata_validation_rolls_back_before_cleanup(self):
        before = self._snapshot()
        read_database = amend._read_database
        remove_tree = shutil.rmtree
        inspected = []
        calls = []

        def fail_after_updates(db):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("test metadata fault")
            return read_database(db)

        def check_rollback_then_remove(path):
            with closing(amend._readonly(Path(path) / "campaign-ledger.sqlite")) as db:
                metadata, rows, sequence = read_database(db)
            inspected.append(metadata["policy_sha256"])
            self.assertEqual(metadata["policy_sha256"], amend.SOURCE_POLICY_SHA256)
            self.assertEqual(len(rows), 230)
            self.assertEqual(sequence, [["requests", 230]])
            remove_tree(path)

        with patch.object(amend, "_read_database", side_effect=fail_after_updates), \
                patch.object(amend.shutil, "rmtree", side_effect=check_rollback_then_remove):
            with self.assertRaisesRegex(RuntimeError, "test metadata fault"):
                amend.amend_campaign_budget(self.output, source_policy=self.policy)
        self.assertEqual(inspected, [amend.SOURCE_POLICY_SHA256])
        self._unchanged(before)
        self.assertFalse(self.output.exists())

    def test_no_network_no_reservations_and_restores_process_policy(self):
        with patch.dict(os.environ, {model_budget.POLICY_ENV: "existing-process-policy"}), \
                patch("socket.create_connection", side_effect=AssertionError("network forbidden")) as network, \
                patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")) as urlopen, \
                patch.object(model_budget, "reserve_request", side_effect=AssertionError("reservation forbidden")) as reserve:
            amend.amend_campaign_budget(self.output, source_policy=self.policy)
            self.assertEqual(os.environ[model_budget.POLICY_ENV], "existing-process-policy")
        network.assert_not_called()
        urlopen.assert_not_called()
        reserve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
