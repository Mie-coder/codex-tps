import contextlib
import io
from pathlib import Path
import plistlib
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import control


class ControlTests(unittest.TestCase):
    def test_login_install_start_stop_and_launcher_stay_outside_original_app(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / 'plugin'; data = base / 'support'
            shutil.copytree(control.ROOT / 'scripts', source / 'scripts')
            shutil.copy2(control.ROOT / 'LICENSE', source / 'LICENSE')
            agent = base / 'agents/tps.plist'; launcher = base / 'apps/Codex TPS.app'
            original = base / 'Original.app'
            with patch.object(control, 'ROOT', source), patch.object(control, 'DATA_ROOT', data), \
                 patch.object(control, 'STATE', data / '.runtime'), patch.object(control, 'AGENT', agent), \
                 patch.object(control, 'LAUNCHER', launcher), \
                 patch.dict(control.os.environ, {'CODEX_HOME': str(base / 'codex-home')}), \
                 patch.object(control, 'app_info', return_value=(original, {})), \
                 patch.object(control, 'stop_monitor') as stop, \
                 patch.object(control, 'service_loaded', return_value=False) as loaded, \
                 patch.object(control.subprocess, 'run') as run, \
                 patch.object(control, 'start_monitor') as start, contextlib.redirect_stdout(io.StringIO()):
                control.install_auto()
                config = plistlib.loads(agent.read_bytes())
                self.assertTrue(config['RunAtLoad'] and config['KeepAlive'])
                self.assertEqual(config['ProgramArguments'][1:], [str(data / 'scripts/monitor.py'), '--auto-port'])
                self.assertEqual(config['EnvironmentVariables']['CODEX_HOME'], str((base / 'codex-home').resolve()))
                self.assertEqual(agent.stat().st_mode & 0o777, 0o600)
                self.assertTrue((launcher / 'Contents/MacOS/CodexTPS').stat().st_mode & 0o111)
                self.assertFalse(original.exists())
                self.assertFalse((source / 'Codex TPS.app').exists())
                self.assertIn(str(data / 'scripts/control.py'), (launcher / 'Contents/MacOS/CodexTPS').read_text())
                stop.assert_any_call(source / '.runtime')
                start.assert_called_once_with(9222)
                run.reset_mock(); control.start_service()
                commands = [call.args[0][:2] for call in run.call_args_list]
                self.assertEqual(commands, [['launchctl', 'enable'], ['launchctl', 'bootstrap']])
                loaded.return_value = True; run.reset_mock(); control.stop_service()
                commands = [call.args[0][:2] for call in run.call_args_list]
                self.assertEqual(commands, [['launchctl', 'disable'], ['launchctl', 'bootout']])

    def test_launcher_requires_clean_launch_and_preserves_active_client(self):
        app = Path('/Applications/ChatGPT.app')
        with patch.object(control, 'app_info', return_value=(app, {})), \
             patch.object(control, 'owned_port', return_value=False), \
             patch.object(control, 'app_running', return_value=True) as active, \
             patch.object(control, 'free_port', return_value=9223), \
             patch.object(control, 'open_app') as opened, \
             patch.object(control, 'start_monitor') as start:
            with self.assertRaisesRegex(RuntimeError, '完全退出'):
                control.launch()
            opened.assert_not_called(); start.assert_not_called()
            active.return_value = False; control.launch()
            opened.assert_called_once_with(app, 9223); start.assert_called_once_with(9223)
        with patch.object(control, 'owned_port', return_value=False), \
             patch.object(control, 'free_port', return_value=9223), \
             patch.object(control, 'restart_app') as restart, \
             patch.object(control, 'start_monitor'), \
             patch.object(control, 'running_pid', return_value=12345), \
             patch.object(sys, 'argv', ['control.py', 'start', '--restart-app']), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(control.main(), 0)
            restart.assert_called_once_with(9223)

    def test_open_uses_loopback_flags_and_rejects_unrelated_launch_agent(self):
        with patch.object(control, 'owned_port', return_value=True), patch.object(control.subprocess, 'run') as run:
            control.open_app(Path('/Applications/ChatGPT.app'), 9223)
            arguments = run.call_args.args[0]
            self.assertIn('--remote-debugging-address=127.0.0.1', arguments)
            self.assertIn('--remote-debugging-port=9223', arguments)
            self.assertIn('--remote-allow-origins=http://127.0.0.1:9223', arguments)
        with tempfile.TemporaryDirectory() as directory:
            agent = Path(directory) / 'tps.plist'
            agent.write_bytes(plistlib.dumps({'Label':'someone.else'}))
            with patch.object(control, 'AGENT', agent), patch.object(control.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, '其他服务'):
                    control.stop_service()
                run.assert_not_called(); self.assertTrue(agent.exists())
