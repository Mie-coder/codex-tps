#!/usr/bin/env python3
"""Control TPS monitoring, its login service, and the Codex TPS launcher."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

from cdp import CDPError, devtools_targets, is_codex_renderer_target
from monitor import DATA_ROOT, ROOT, STATE

LABEL = 'mie.codex-tps'
DOMAIN = f'gui/{os.getuid()}'
SERVICE = f'{DOMAIN}/{LABEL}'
AGENT = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
LAUNCHER = Path.home() / 'Applications/Codex TPS.app'


def owned_port(port):
    try:
        return any(is_codex_renderer_target(t) for t in devtools_targets(port))
    except (CDPError, OSError, ValueError):
        return False


def running_pid(state=STATE):
    try:
        pid = int((state / 'monitor.pid').read_text())
        command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True).strip()
        return pid if any(str(root / 'scripts/monitor.py') in command for root in (ROOT, DATA_ROOT)) else None
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def stop_monitor(state=STATE):
    pid = running_pid(state)
    if not pid: return
    try: os.kill(pid, signal.SIGTERM)
    except ProcessLookupError: return
    for _ in range(50):
        if not running_pid(state): return
        time.sleep(.2)
    raise RuntimeError('TPS 监视器还未退出；未强制终止。请稍后重试。')


def app_info():
    candidates = [directory / name for directory in (Path('/Applications'), Path.home() / 'Applications')
                  for name in ('ChatGPT.app', 'Codex.app')]
    app = next((p for p in candidates if p.is_dir()), None)
    if app is None: raise RuntimeError('没有找到 ChatGPT.app 或 Codex.app。')
    return app, plistlib.loads((app / 'Contents/Info.plist').read_bytes())


def app_running(app, meta):
    executable = str(app / 'Contents/MacOS' / meta['CFBundleExecutable'])
    processes = subprocess.check_output(['ps', '-axo', 'command='], text=True).splitlines()
    return any(c == executable or c.startswith(executable + ' ') for c in processes)


def free_port():
    for candidate in range(9222, 9232):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', candidate)); return candidate
            except OSError: continue
    raise RuntimeError('本机 9222–9231 端口均不可用。')


def open_app(app, port):
    subprocess.run(['open', str(app), '--args', '--remote-debugging-address=127.0.0.1',
                    f'--remote-debugging-port={port}', f'--remote-allow-origins=http://127.0.0.1:{port}'], check=True)
    for _ in range(45):
        if owned_port(port): return
        time.sleep(1)
    raise RuntimeError('客户端未开放预期的本机调试接口；未修改应用包。')


def restart_app(port):
    app, meta = app_info()
    if app_running(app, meta):
        subprocess.run(['osascript', '-e', f'tell application id "{meta["CFBundleIdentifier"]}" to quit'], check=True)
        for _ in range(30):
            if not app_running(app, meta): break
            time.sleep(.5)
        else:
            raise RuntimeError('客户端仍在运行，可能有活跃任务；未强制退出。请在任务结束后重试。')
    open_app(app, port)


def service_loaded():
    return subprocess.run(['launchctl', 'print', SERVICE], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def check_agent():
    if AGENT.exists() and plistlib.loads(AGENT.read_bytes()).get('Label') != LABEL:
        raise RuntimeError(f'启动项路径已被其他服务占用：{AGENT}')


def stop_service():
    check_agent()
    if AGENT.exists():
        subprocess.run(['launchctl', 'disable', SERVICE], check=True)
    if service_loaded():
        subprocess.run(['launchctl', 'bootout', SERVICE], check=True)
    stop_monitor()


def sync_runtime():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    if ROOT == DATA_ROOT: return
    (DATA_ROOT / 'scripts').mkdir(exist_ok=True)
    for name in ('control.py', 'monitor.py', 'cdp.py', 'tps_stats.py', 'overlay.js'):
        shutil.copy2(ROOT / 'scripts' / name, DATA_ROOT / 'scripts' / name)
    shutil.copy2(ROOT / 'LICENSE', DATA_ROOT / 'LICENSE')


def start_service():
    check_agent()
    subprocess.run(['launchctl', 'enable', SERVICE], check=True)
    if not service_loaded():
        stop_monitor()
        sync_runtime()
        subprocess.run(['launchctl', 'bootstrap', DOMAIN, str(AGENT)], check=True)
    else:
        subprocess.run(['launchctl', 'kickstart', SERVICE], check=True)


def start_monitor(port):
    if AGENT.exists():
        start_service()
    elif not running_pid():
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / 'monitor.log').open('a') as log:
            subprocess.Popen([sys.executable, str(ROOT / 'scripts/monitor.py'), '--port', str(port), '--auto-port'],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(40):
        if running_pid(): return
        time.sleep(.25)
    raise RuntimeError(f'监视器启动失败，请查看 {STATE / "monitor.log"}。')


def make_launcher(path):
    contents = path / 'Contents'
    (contents / 'MacOS').mkdir(parents=True, exist_ok=True)
    (contents / 'Resources').mkdir(exist_ok=True)
    meta = {'CFBundleIdentifier':'mie.codex-tps.launcher', 'CFBundleExecutable':'CodexTPS',
            'CFBundleName':'Codex TPS', 'CFBundleDisplayName':'Codex TPS', 'CFBundlePackageType':'APPL',
            'CFBundleVersion':'1', 'CFBundleInfoDictionaryVersion':'6.0', 'LSUIElement':True}
    app, original = app_info()
    icon = app / 'Contents/Resources' / original.get('CFBundleIconFile', '')
    if icon.is_file():
        shutil.copy2(icon, contents / 'Resources/app.icns'); meta['CFBundleIconFile'] = 'app.icns'
    (contents / 'Info.plist').write_bytes(plistlib.dumps(meta))
    executable = contents / 'MacOS/CodexTPS'
    command = shlex.join([sys.executable, str(DATA_ROOT / 'scripts/control.py'), 'launch'])
    executable.write_text('#!/bin/sh\nif ! message=$(' + command + ' 2>&1); then\n'
        '  /usr/bin/osascript -e \'on run argv\' -e \'display alert "Codex TPS" message (item 1 of argv) as warning\''
        ' -e \'end run\' "$message"\n  exit 1\nfi\n')
    executable.chmod(0o755)


def install_auto():
    check_agent()
    if LAUNCHER.exists():
        meta = plistlib.loads((LAUNCHER / 'Contents/Info.plist').read_bytes())
        if meta.get('CFBundleIdentifier') != 'mie.codex-tps.launcher':
            raise RuntimeError(f'启动图标路径已被其他应用占用：{LAUNCHER}')
    stop_service()
    # Migrate the old detached monitor before the shared service takes its lock.
    stop_monitor(ROOT / '.runtime')
    sync_runtime()
    STATE.mkdir(parents=True, exist_ok=True); STATE.chmod(0o700)
    make_launcher(LAUNCHER)
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    AGENT.write_bytes(plistlib.dumps({'Label':LABEL,
        'ProgramArguments':[sys.executable, str(DATA_ROOT / 'scripts/monitor.py'), '--auto-port'],
        'EnvironmentVariables':{'CODEX_TPS_DATA':str(STATE), 'PYTHONDONTWRITEBYTECODE':'1',
                                'CODEX_HOME':str(Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).resolve())},
        'RunAtLoad':True, 'KeepAlive':True, 'ThrottleInterval':10,
        'WorkingDirectory':str(DATA_ROOT),
        'StandardOutPath':str(STATE / 'monitor.log'), 'StandardErrorPath':str(STATE / 'monitor.log')}))
    AGENT.chmod(0o600)
    start_monitor(9222)
    print(f'自动启动已安装，监视器退出后由 macOS 重新拉起。以后从此图标打开客户端：{LAUNCHER}')


def remove_auto():
    stop_service()
    AGENT.unlink(missing_ok=True)
    if LAUNCHER.exists():
        meta = plistlib.loads((LAUNCHER / 'Contents/Info.plist').read_bytes())
        if meta.get('CFBundleIdentifier') == 'mie.codex-tps.launcher': shutil.rmtree(LAUNCHER)
    print('已移除自动启动项和启动图标；保留插件及运行诊断。')


def launch():
    app, meta = app_info()
    port = next((p for p in range(9222, 9232) if owned_port(p)), None)
    if port is None:
        if app_running(app, meta):
            raise RuntimeError('Codex 已运行，但没有开启 TPS 调试接口。请在任务结束后完全退出客户端，再用“Codex TPS”图标打开；无需再次运行启用命令。')
        port = free_port()
        open_app(app, port)
    else:
        subprocess.run(['open', str(app)], check=True)
    start_monitor(port)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'stop', 'status', 'auto-install', 'auto-remove', 'launch'])
    parser.add_argument('--restart-app', action='store_true', help='Explicitly permit a graceful desktop app restart.')
    args = parser.parse_args()
    if args.action == 'auto-install': install_auto(); return 0
    if args.action == 'auto-remove': remove_auto(); return 0
    if args.action == 'launch': launch(); return 0
    if args.action == 'status':
        if not running_pid():
            print('TPS 监视器未运行。'); return 1
        try:
            status = json.loads((STATE / 'status.json').read_text())
            status['autostart_installed'] = AGENT.exists()
            status['service_loaded'] = service_loaded()
            if time.time() - status.get('updated_at', 0) > 15:
                print('TPS 进程存在，但状态已过期。'); return 1
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 1 if status.get('error') else 0
        except (OSError, ValueError):
            print('TPS 进程已启动，等待连接。'); return 1
    if args.action == 'stop':
        stop_service()
        print('已停止 TPS 监视器并暂停自动恢复；再次启动或点击 Codex TPS 图标可恢复。')
        return 0
    # Check the endpoint before the PID: a living monitor may be waiting after a normal app restart.
    port = next((p for p in range(9222, 9232) if owned_port(p)), None)
    if port is None and args.restart_app:
        port = free_port(); restart_app(port)
    if port is None and not AGENT.exists():
        print('请完全退出客户端后从“Codex TPS”图标打开，；安装入口见仓库 README。')
        return 2
    start_monitor(port or 9222)
    print('TPS 监视器已启动。' + (f'端口：{port}' if port else '等待 Codex TPS 启动入口开放调试端口。'))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr); raise SystemExit(1)
