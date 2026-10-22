"""Release-boundary regressions using synthetic state and no network."""
import io
import json
import unittest
from unittest.mock import Mock, patch

import launch
import server


class ReleaseGuardTests(unittest.TestCase):
    def test_initial_save_failure_does_not_leave_a_phantom_running_job(self):
        with patch.object(server, 'JOBS', {}), patch.object(server, 'STOP_EVENTS', {}):
            with patch.object(server, 'checkpoint', side_effect=OSError('synthetic disk failure')):
                with self.assertRaises(OSError):
                    server.create_job()
            self.assertEqual(server.JOBS, {})
            self.assertEqual(server.STOP_EVENTS, {})
            with patch.object(server, 'checkpoint') as save:
                run_id = server.create_job()
            save.assert_called_once_with(run_id)
            self.assertEqual(server.JOBS[run_id]['status'], 'running')

    def test_launcher_does_not_reuse_backend_missing_current_billing(self):
        health = {'agent_workbench': 'agent-ui-v1', 'version': launch.APP_VERSION,
                  'concurrency': True, 'default_mode': 'rules', 'history': True,
                  'faults': {'corrupt_payload': ''}, 'test_suites': True,
                  'test_workflow_version': launch.WORKFLOW_VERSION}
        for capability, expected in [(None, False), ('old-version', False), ('run-billing-v1', True)]:
            with self.subTest(capability=capability):
                payload = dict(health)
                if capability is not None:
                    payload['workbench_billing'] = capability
                opener = Mock()
                opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
                with patch.object(launch.sys, 'frozen', False, create=True), \
                     patch.object(launch.urllib.request, 'build_opener', return_value=opener):
                    self.assertEqual(launch.healthy(8766), expected)


if __name__ == '__main__':
    unittest.main()
