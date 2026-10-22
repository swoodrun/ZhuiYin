"""Embedded-host launcher and policy tests without UI or model calls."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, call, patch

import desktop_entry
import desktop_shell as shell
import launch


class EmbeddedPolicyTests(unittest.TestCase):
    def test_only_the_active_loopback_origin_is_network_accessible(self):
        origin = 'http://127.0.0.1:8768'
        self.assertTrue(shell.navigation_allowed(origin+'/#workbench', origin))
        self.assertTrue(shell.navigation_allowed(origin+'/api/runs/example/export', origin))
        for url in ('https://example.com/', 'http://127.0.0.1:8766/', 'http://localhost:8768/',
                    'http://127.0.0.1.evil:8768/', 'http://user@127.0.0.1:8768/',
                    'file:///C:/private.txt', 'javascript:alert(1)', None):
            with self.subTest(url=url):
                self.assertFalse(shell.navigation_allowed(url, origin))

    def test_memory_assets_and_local_blob_downloads_are_allowed(self):
        origin = 'http://127.0.0.1:8768'
        for url in ('about:blank', 'data:image/png;base64,AA', 'qrc:/internal', 'blob:'+origin+'/id'):
            self.assertTrue(shell.navigation_allowed(url, origin))
        self.assertFalse(shell.navigation_allowed('blob:https://example.com/id', origin))

    def test_malformed_ports_are_rejected(self):
        self.assertFalse(shell.same_origin('http://127.0.0.1:invalid/', 'http://127.0.0.1:8768'))
        self.assertFalse(shell.same_origin('http://127.0.0.1:99999/', 'http://127.0.0.1:8768'))

    def test_download_suggestions_cannot_escape_the_selected_directory(self):
        self.assertEqual(shell.download_name('../../results.zip'), 'results.zip')
        self.assertEqual(shell.download_name(r'C:\private\results.csv'), 'results.csv')
        self.assertEqual(shell.download_name('a:b?.json'), 'a_b_.json')
        self.assertEqual(shell.download_name('CON.txt'), '追因结果.json')
        self.assertEqual(shell.download_name(''), '追因结果.json')
        self.assertEqual(shell.download_name('测试结果.json'), '测试结果.json')

    def test_default_exe_entry_never_dispatches_an_external_browser(self):
        with patch.object(desktop_entry.sys, 'frozen', True, create=True), \
             patch.object(desktop_entry.sys, 'argv', ['追因.exe']), \
             patch.dict(desktop_entry.os.environ, {}, clear=True), \
             patch.object(shell, 'run_desktop', return_value=0) as embedded, \
             patch.object(launch, 'open_page') as browser:
            self.assertEqual(desktop_entry.main(), 0)
        embedded.assert_called_once_with('workbench')
        browser.assert_not_called()


class EmbeddedHostLauncherTests(unittest.TestCase):
    def test_missing_native_host_reports_the_complete_archive_requirement(self):
        resources = Path('C:/Synthetic/中文 便携版/_internal')
        with patch.object(shell.launch, 'ensure_service', return_value='http://127.0.0.1:8768/#testing') as service, \
             patch.object(shell, 'resource_root', return_value=resources), \
             patch.object(Path, 'is_file', autospec=True, return_value=False) as exists, \
             patch.object(shell.subprocess, 'Popen') as spawn, \
             patch('ctypes.windll.kernel32.SetDllDirectoryW') as dll:
            with self.assertRaisesRegex(RuntimeError, '内嵌窗口组件缺失.*完整解压.*_internal'):
                shell.run_desktop('testing')
        service.assert_called_once_with('testing')
        exists.assert_called_once_with(resources/'webview-host'/'追因窗口.exe')
        spawn.assert_not_called()
        dll.assert_not_called()

    def test_native_host_receives_local_url_data_and_clean_environment(self):
        resources = Path('C:/Synthetic/中文 便携版/_internal')
        data = Path('C:/Synthetic/独立 用户数据')
        url = 'http://127.0.0.1:8768/#testing'
        inherited = {
            '_PYI_APPLICATION_HOME_DIR': 'private bundle',
            '_PYI_PARENT_PROCESS_LEVEL': '1',
            'PYINSTALLER_RESET_ENVIRONMENT': '1',
            'PYINSTALLER_STRICT_UNPACK_MODE': '1',
            'PYTHONHOME': 'private python',
            'PYTHONPATH': 'private imports',
            'SYSTEMROOT': 'C:/Windows',
            'TEMP': 'C:/Synthetic/临时 文件',
            'ZHUIYIN_EMBEDDED_PROBE': '1',
        }
        process = Mock()
        process.wait.return_value = 17
        events = Mock()
        with patch.object(shell.launch, 'ensure_service', return_value=url) as service, \
             patch.object(shell, 'resource_root', return_value=resources), \
             patch.object(shell, 'data_root', return_value=data), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(sys, '_MEIPASS', str(resources), create=True), \
             patch.object(shell.subprocess, 'Popen', return_value=process) as spawn, \
             patch('ctypes.windll.kernel32.SetDllDirectoryW') as dll, \
             patch.dict(os.environ, inherited, clear=True):
            events.attach_mock(dll, 'dll')
            events.attach_mock(spawn, 'spawn')
            events.attach_mock(process.wait, 'wait')
            self.assertEqual(shell.run_desktop('testing'), 17)
            self.assertEqual(dict(os.environ), inherited)
        service.assert_called_once_with('testing')
        clean = {key: inherited[key] for key in ('SYSTEMROOT', 'TEMP', 'ZHUIYIN_EMBEDDED_PROBE')}
        self.assertEqual(events.mock_calls, [
            call.dll(None),
            call.spawn([str(resources/'webview-host'/'追因窗口.exe'), url, str(data)],
                env=clean, cwd=data, stdin=shell.subprocess.DEVNULL,
                stdout=shell.subprocess.DEVNULL, stderr=shell.subprocess.DEVNULL,
                creationflags=shell.subprocess.CREATE_NO_WINDOW, close_fds=True),
            call.dll(str(resources)),
            call.wait(),
        ])

    def test_failed_native_host_launch_restores_the_bundle_dll_directory(self):
        resources = Path('C:/Synthetic/中文 便携版/_internal')
        failure = OSError('synthetic native host failure')
        with patch.object(shell.launch, 'ensure_service', return_value='http://127.0.0.1:8768/'), \
             patch.object(shell, 'resource_root', return_value=resources), \
             patch.object(shell, 'data_root', return_value=Path('C:/Synthetic/独立 用户数据')), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(sys, '_MEIPASS', str(resources), create=True), \
             patch.object(shell.subprocess, 'Popen', side_effect=failure) as spawn, \
             patch('ctypes.windll.kernel32.SetDllDirectoryW') as dll:
            with self.assertRaises(OSError) as caught:
                shell.run_desktop()
        self.assertIs(caught.exception, failure)
        spawn.assert_called_once()
        self.assertEqual(dll.call_args_list, [call(None), call(str(resources))])


if __name__ == '__main__':
    unittest.main()
