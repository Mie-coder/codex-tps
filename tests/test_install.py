import copy
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install


class InstallTests(unittest.TestCase):
    def test_only_reviewed_hook_gets_exact_trust_and_other_state_is_preserved(self):
        hook = {'key': 'plugin:codex-tps-monitor@mie-codex-tps:Stop:0:0',
                'currentHash': 'a' * 64, 'trustStatus': 'trusted'}
        home = Path('/isolated-codex-home')
        before = {'other-hook': {'enabled': False, 'trusted_hash': 'b' * 64}}
        after = copy.deepcopy(before)
        after[hook['key']] = {'trusted_hash': hook['currentHash']}
        server = Mock()
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=False)
        with patch.object(install, 'Server', return_value=server), \
             patch.object(install, 'selected_hook', return_value=(hook, install.PLUGIN)):
            server.call.return_value = {}
            install.configure('codex', home)
            self.assertEqual(server.call.call_count, 1)
            self.assertEqual(server.call.call_args.args[1]['edits'],
                             [{'keyPath': 'features.hooks', 'value': True, 'mergeStrategy': 'replace'}])
            server.reset_mock()
            server.call.side_effect = [{}, {'config': {'hooks': {'state': before}}}, {},
                                       {'config': {'hooks': {'state': after}}}]
            install.configure('codex', home, reviewed=True)
            edits = [call.args[1]['edits'] for call in server.call.call_args_list
                     if call.args[0] == 'config/batchWrite']
            self.assertEqual(edits[1], [{'keyPath': 'hooks.state."' + hook['key'] + '".trusted_hash',
                                       'value': hook['currentHash'], 'mergeStrategy': 'replace'}])
            after['other-hook']['enabled'] = True
            server.call.side_effect = [{}, {'config': {'hooks': {'state': before}}}, {},
                                       {'config': {'hooks': {'state': after}}}]
            with self.assertRaisesRegex(RuntimeError, 'Other Hook states changed'):
                install.configure('codex', home, reviewed=True)

    def test_hook_source_and_command_must_match_reviewed_release(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            installed = home / 'plugins/cache/mie-codex-tps/codex-tps-monitor/0.2.1'
            shutil.copytree(install.PLUGIN, installed)
            hook = {'pluginId': install.PLUGIN_ID, 'eventName': 'stop', 'enabled': True,
                    'sourcePath': str(installed / 'hooks/hooks.json'),
                    'command': shlex.join(['python3', str(installed / 'scripts/stop_hook.py')])}
            server = Mock()
            server.call.return_value = {'data': [{'hooks': [hook]}]}
            self.assertEqual(install.selected_hook(server, home)[1], installed)
            hook['command'] += ' --unreviewed'
            with self.assertRaisesRegex(RuntimeError, 'Unexpected TPS Hook command'):
                install.selected_hook(server, home)
            hook['command'] = shlex.join(['python3', str(installed / 'scripts/stop_hook.py')])
            script = installed / 'scripts/stop_hook.py'
            script.write_text(script.read_text() + '\n# altered after review\n')
            with self.assertRaisesRegex(RuntimeError, 'Installed files differ'):
                install.selected_hook(server, home)


if __name__ == '__main__':
    unittest.main()
