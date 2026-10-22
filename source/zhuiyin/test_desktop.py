"""Desktop resource/data split and authenticated shutdown, offline only."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import desktop_entry
import launch
import runtime_paths
import server


class DesktopTests(unittest.TestCase):
    def test_source_data_remains_in_original_project(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(runtime_paths.sys, 'frozen', False, create=True):
            self.assertEqual(runtime_paths.data_root(), Path(server.__file__).parent/'data')

    def test_frozen_data_does_not_use_temporary_resources(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': 'C:/Synthetic/UserData'}, clear=True), \
             patch.object(runtime_paths.sys, 'executable', 'C:/Synthetic/Portable/追因.exe'), \
             patch.object(runtime_paths.sys, 'frozen', True, create=True):
            self.assertEqual(runtime_paths.data_root(), Path('C:/Synthetic/Portable/追因数据'))
        with patch.dict(os.environ, {'ZHUIYIN_DATA_DIR': 'relative'}, clear=True):
            with self.assertRaises(ValueError):
                runtime_paths.data_root()

    def handler(self, body):
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {'Host': '127.0.0.1'}
        handler.server = SimpleNamespace(server_port=8767, shutdown=Mock())
        handler.path = '/api/desktop/stop'
        handler.read_json = Mock(return_value=body)
        handler.json = Mock()
        return handler

    def test_shutdown_requires_private_token_and_no_running_experiment(self):
        for configured, sent, jobs, expected in [
            (None, 'token', {}, 403), ('token', 'wrong', {}, 403),
            ('token', None, {}, 403), ('token', 'token', {'x': {'status': 'running'}}, 409),
            ('token', 'token', {'x': {'status': 'done'}}, 200)]:
            handler = self.handler({'token': sent})
            with patch.object(server, 'DESKTOP_TOKEN', configured), patch.object(server, 'JOBS', jobs), \
                 patch.object(server.threading, 'Thread') as thread:
                handler.do_POST()
            self.assertEqual(handler.json.call_args.args[0], expected)
            self.assertEqual(thread.called, expected == 200)
            self.assertNotIn('token', json.dumps(handler.json.call_args.args))

    def test_frozen_launcher_uses_same_exe_and_independent_child_environment(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder, patch.object(launch.sys, 'frozen', True, create=True), \
             patch.object(launch.sys, 'argv', ['追因.exe']), patch.object(launch, 'data_root', return_value=Path(folder)), \
             patch.object(launch, 'healthy', side_effect=[False]*10+[True]), \
             patch.object(launch.socket, 'socket'), \
             patch.object(launch.subprocess, 'Popen') as spawn, patch.object(launch, 'open_page'):
            launch.main()
        self.assertEqual(spawn.call_args.args[0], [launch.sys.executable, '--serve', '--port', '8766'])
        self.assertEqual(spawn.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        self.assertEqual(spawn.call_args.kwargs['cwd'], Path(folder))


if __name__ == '__main__':
    unittest.main()
