"""Desktop visibility and browser dispatch. No real browser or model calls."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import desktop_entry
import desktop_shell
import launch


class BrowserDispatchTests(unittest.TestCase):
    def test_registry_command_is_only_used_to_find_a_known_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            browser = Path(folder)/'msedge.exe'
            browser.touch()
            self.assertEqual(launch._browser_executable('"'+str(browser)+'" --single-argument %1'), browser)
            self.assertIsNone(launch._browser_executable('cmd.exe /c anything'))
            self.assertIsNone(launch._browser_executable('powershell.exe -c anything'))
            self.assertIsNone(launch._browser_executable('"'+str(Path(folder)/'missing.exe')+'"'))

    def test_frozen_browser_gets_argv_and_clean_environment_with_dll_path_restored(self):
        with patch.object(launch.sys, 'frozen', True, create=True), \
             patch.object(launch.sys, '_MEIPASS', 'SYNTHETIC_RESOURCES', create=True), \
             patch.object(launch, 'windows_browser', return_value=Path('C:/Browser/msedge.exe')), \
             patch.object(launch.subprocess, 'Popen') as spawn, \
             patch('ctypes.windll.kernel32.SetDllDirectoryW') as dll, \
             patch.dict(os.environ, {'_PYI_APPLICATION_HOME_DIR': 'private', 'PYTHONPATH': 'private'}, clear=True):
            self.assertEqual(launch.open_page('http://127.0.0.1:8767/#workbench'), 'direct_browser')
        self.assertEqual(spawn.call_args.args[0], ['C:\\Browser\\msedge.exe', 'http://127.0.0.1:8767/#workbench'])
        self.assertNotIn('_PYI_APPLICATION_HOME_DIR', spawn.call_args.kwargs['env'])
        self.assertNotIn('PYTHONPATH', spawn.call_args.kwargs['env'])
        self.assertEqual(dll.call_args_list[0].args, (None,))
        self.assertEqual(dll.call_args_list[-1].args, ('SYNTHETIC_RESOURCES',))

    def test_browser_failure_also_restores_dll_path(self):
        with patch.object(launch.sys, 'frozen', True, create=True), \
             patch.object(launch.sys, '_MEIPASS', 'RESOURCES', create=True), \
             patch.object(launch, 'windows_browser', return_value=Path('C:/Browser/msedge.exe')), \
             patch.object(launch.subprocess, 'Popen', side_effect=OSError('synthetic')), \
             patch('ctypes.windll.kernel32.SetDllDirectoryW') as dll, patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(OSError):
                launch.open_page('http://127.0.0.1:8767/')
        self.assertEqual(dll.call_args_list[-1].args, ('RESOURCES',))

    def test_default_frozen_entry_shows_window_instead_of_exiting_silently(self):
        with patch.object(desktop_entry.sys, 'frozen', True, create=True), \
             patch.object(desktop_entry.sys, 'argv', ['追因.exe']), \
             patch.dict(os.environ, {}, clear=True), patch.object(desktop_shell, 'run_desktop') as window, \
             patch.object(launch, 'main') as headless:
            desktop_entry.main()
        window.assert_called_once_with('workbench')
        headless.assert_not_called()

    def test_explicit_no_browser_keeps_headless_service_checks_available(self):
        with patch.object(desktop_entry.sys, 'frozen', True, create=True), \
             patch.object(desktop_entry.sys, 'argv', ['追因.exe', '--no-browser']), \
             patch.dict(os.environ, {}, clear=True), patch.object(desktop_shell, 'run_desktop') as window, \
             patch.object(launch, 'main') as headless:
            desktop_entry.main()
        headless.assert_called_once()
        window.assert_not_called()

    def test_early_server_exit_is_reported_without_waiting_thirty_seconds(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launch.sys, 'frozen', True, create=True), \
             patch.object(launch, 'data_root', return_value=Path(folder)), \
             patch.object(launch, 'healthy', return_value=False), patch.object(launch.socket, 'socket'), \
             patch.object(launch.subprocess, 'Popen', return_value=Mock(poll=Mock(return_value=1))), \
             patch.object(launch.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '提前退出'):
                launch.ensure_service()
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
