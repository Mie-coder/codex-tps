#!/usr/bin/env python3
"""Install this local marketplace through Codex; optionally enable macOS footer display."""
import argparse
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
PLUGIN = ROOT / 'plugins/codex-tps-monitor'
PLUGIN_ID = 'codex-tps-monitor@mie-codex-tps'


def find_codex(explicit=None):
    if explicit:
        path = shutil.which(explicit)
        if not path:
            raise RuntimeError('The supplied Codex executable was not found.')
        return path
    path = shutil.which('codex')
    if path:
        return path
    for directory in (Path('/Applications'), Path.home() / 'Applications'):
        for name in ('ChatGPT.app', 'Codex.app'):
            path = directory / name / 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
            if path.is_file():
                return str(path)
    raise RuntimeError('Codex CLI was not found. Install a compatible client or pass --codex PATH.')


class Server:
    """Short-lived configuration connection; never starts a model turn."""
    def __init__(self, executable, home):
        self.home = home.resolve()
        self.process = subprocess.Popen(
            [executable, '--enable', 'hooks', 'app-server', '--stdio'],
            cwd=ROOT, env=dict(os.environ, CODEX_HOME=str(self.home)),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8')
        self.messages = queue.Queue()
        self.counter = 0
        threading.Thread(target=self.read, daemon=True).start()
        try:
            result = self.call('initialize', {
                'clientInfo': {'name': 'codex_tps_setup', 'title': 'Codex TPS Setup', 'version': '0.2.1'},
                'capabilities': {'experimentalApi': True}})
            if Path(result['codexHome']).resolve() != self.home:
                raise RuntimeError('Configuration home mismatch; installation stopped.')
            self.send({'method': 'initialized'})
        except Exception:
            self.close()
            raise

    def read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                pass
        self.messages.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()

    def call(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.send({'id': request_id, 'method': method, 'params': params})
        deadline = time.monotonic() + 30
        while True:
            try:
                reply = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError('Codex App Server timed out: ' + method)
            if reply is None:
                raise RuntimeError('Codex App Server exited: ' + method)
            if 'method' in reply and 'id' in reply:
                raise RuntimeError('Unexpected interactive request; use the native installation UI.')
            if reply.get('id') != request_id:
                continue
            if 'error' in reply:
                raise RuntimeError('Codex rejected ' + method + ' (code ' + str(reply['error'].get('code')) + ').')
            return reply['result']

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process.stdin.close()
        self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def selected_hook(server, home):
    listing = server.call('hooks/list', {'cwds': [str(ROOT)]})
    hooks = [h for entry in listing['data'] for h in entry['hooks'] if h.get('pluginId') == PLUGIN_ID]
    if len(hooks) != 1 or hooks[0].get('eventName') != 'stop' or not hooks[0].get('enabled'):
        raise RuntimeError('Expected exactly one enabled TPS Stop Hook.')
    hook = hooks[0]
    path = Path(hook['sourcePath']).resolve(strict=True)
    path.relative_to((home / 'plugins/cache/mie-codex-tps/codex-tps-monitor').resolve())
    if path.parts[-2:] != ('hooks', 'hooks.json'):
        raise RuntimeError('Unexpected TPS Hook source.')
    installed = path.parent.parent
    for name in ('hooks/hooks.json', 'scripts/stop_hook.py', 'scripts/tps_stats.py', '.codex-plugin/plugin.json'):
        if (installed / name).read_bytes() != (PLUGIN / name).read_bytes():
            raise RuntimeError('Installed files differ from the reviewed release: ' + name)
    if shlex.split(hook['command']) != ['python3', str(installed / 'scripts/stop_hook.py')]:
        raise RuntimeError('Unexpected TPS Hook command.')
    return hook, installed


def configure(executable, home, reviewed=False):
    with Server(executable, home) as server:
        server.call('config/batchWrite', {
            'filePath': str(home / 'config.toml'), 'reloadUserConfig': True,
            'edits': [{'keyPath': 'features.hooks', 'value': True, 'mergeStrategy': 'replace'}]})
        hook, installed = selected_hook(server, home)
        if reviewed:
            before = server.call('config/read', {})['config'].get('hooks', {}).get('state', {})
            key = hook['key']
            server.call('config/batchWrite', {
                'filePath': str(home / 'config.toml'), 'reloadUserConfig': True,
                'edits': [{'keyPath': 'hooks.state.' + json.dumps(key) + '.trusted_hash',
                           'value': hook['currentHash'], 'mergeStrategy': 'replace'}]})
            after = server.call('config/read', {})['config'].get('hooks', {}).get('state', {})
            if any(after.get(k) != v for k, v in before.items() if k != key):
                raise RuntimeError('Other Hook states changed; inspect the configuration backup.')
    with Server(executable, home) as server:
        hook, installed = selected_hook(server, home)
        if reviewed and hook['trustStatus'] != 'trusted':
            raise RuntimeError('The reviewed TPS Hook did not become trusted.')
    return hook, installed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', help='Codex CLI path; the bundled desktop CLI is a fallback.')
    parser.add_argument('--trust-reviewed-hook', action='store_true',
                        help='Trust ONLY this installed TPS definition after reviewing its scripts.')
    parser.add_argument('--inline', action='store_true', help='Install macOS footer service and launcher; never restart the client.')
    args = parser.parse_args()
    if sys.version_info < (3, 9):
        parser.error('Python 3.9 or newer is required.')
    if args.inline and sys.platform != 'darwin':
        parser.error('--inline currently supports macOS only.')
    executable = find_codex(args.codex)
    home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).resolve()
    home.mkdir(parents=True, exist_ok=True)
    config = home / 'config.toml'
    if config.exists():
        directory = home / 'codex-tps-backups'
        directory.mkdir(mode=0o700, exist_ok=True)
        backup = directory / ('config-' + str(time.time_ns()) + '.toml')
        shutil.copy2(config, backup)
        backup.chmod(0o600)
    env = dict(os.environ, CODEX_HOME=str(home))
    for command in (['plugin', 'marketplace', 'add', str(ROOT), '--json'],
                    ['plugin', 'add', PLUGIN_ID, '--json']):
        result = subprocess.run([executable, *command], cwd=ROOT, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        if result.returncode:
            raise RuntimeError('Official plugin installation failed at: ' + ' '.join(command[:3]))
    hook, installed = configure(executable, home, args.trust_reviewed_hook)
    if args.inline:
        subprocess.run([sys.executable, str(installed / 'scripts/control.py'), 'auto-install'],
                       env=env, check=True, timeout=60)
    print(json.dumps({'plugin_id': PLUGIN_ID, 'hook_enabled': hook['enabled'],
                      'trust_status': hook['trustStatus'], 'inline_installed': args.inline,
                      'native_display_needs_verification': True}, ensure_ascii=False, indent=2))
    print('Verify a newly completed turn after the client loads the configuration.')
    if hook['trustStatus'] != 'trusted':
        print('Review and trust this TPS Stop Hook in native Hook settings before it will run.')
    if args.inline:
        print('After active work finishes, quit normally and open ~/Applications/Codex TPS.app.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
