#!/usr/bin/env python3
"""Local TPS monitor: inspect JSONL, or attach to a loopback Codex renderer."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

from cdp import CDPClient, CDPError, devtools_targets, is_codex_renderer_target, target_score
from tps_stats import Session

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path.home() / 'Library/Application Support/Codex TPS'
# All plugin copies share one lock, so the source and installed skill cannot duplicate rows.
STATE = Path(os.environ.get('CODEX_TPS_DATA', DATA_ROOT / '.runtime'))


def find_session(home, thread):
    import re
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', thread):
        return None
    matches = [p for directory in ('sessions', 'archived_sessions')
               for p in (home / directory).rglob(f'*{thread}.jsonl')]
    return matches[0] if len(matches) == 1 else None


def targets(port):
    return [t for t in devtools_targets(port) if t.get('type') == 'page'
            and is_codex_renderer_target(t) and target_score(t) >= 100
            and 'initialroute=' not in t.get('url', '').lower()
            and t.get('webSocketDebuggerUrl')]


def available_pages(port, auto=False):
    for candidate in dict.fromkeys([port] + (list(range(9222, 9232)) if auto else [])):
        try:
            pages = targets(candidate)
            if pages: return candidate, pages
        except (CDPError, OSError, ValueError):
            if not auto: raise
    raise CDPError('等待 Codex TPS 启动入口开放本机调试端口。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=9222)
    parser.add_argument('--auto-port', action='store_true', help='Reconnect across launcher port changes, or wait while the app is closed.')
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')))
    parser.add_argument('--inspect', type=Path, help='Read this JSONL and print metrics; no UI connection.')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--remove', action='store_true', help='Remove TPS UI from connected windows.')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    if args.inspect:
        s = Session(); s.read(args.inspect)
        print(json.dumps({'thread_id': s.thread_id, 'turns': s.results()}, ensure_ascii=False, indent=2))
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    lock = (STATE / 'monitor.lock').open('w')
    if not args.remove:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('TPS monitor already running.'); return 0
        (STATE / 'monitor.pid').write_text(str(os.getpid()))
        (STATE / 'port').write_text(str(args.port))
    overlay = (ROOT / 'scripts' / 'overlay.js').read_text()
    build_id = hashlib.sha256((Path(__file__).read_text() + overlay).encode()).hexdigest()[:12]
    clients = {}; sessions = {}; session_paths = {}; last_scan = 0; last_error = None
    last_diagnostics = None
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try:
        while running:
            try:
                port, pages = available_pages(args.port, args.auto_port)
                if port != args.port:
                    for client in clients.values(): client.close()
                    clients.clear()
                    args.port = port
                    (STATE / 'port').write_text(str(port))
                live = {p['id'] for p in pages}
                for key in list(clients):
                    if key not in live:
                        clients.pop(key).close()
                if not pages:
                    raise CDPError('No supported Codex window on the debug port.')
                if time.monotonic() - last_scan > 30:
                    session_paths.clear(); last_scan = time.monotonic()
                rendered = 0
                diagnostics = []
                for page in pages:
                    key = page['id']
                    if key not in clients:
                        clients[key] = CDPClient(page['webSocketDebuggerUrl'])
                    client = clients[key]
                    if args.remove:
                        client.evaluate('window.__codexTPSMonitor?.stop()'); continue
                    thread = client.evaluate(f'({overlay})(null)')
                    if thread not in session_paths:
                        session_paths[thread] = find_session(args.codex_home, thread or '')
                    path = session_paths[thread]
                    payload = {'thread_id': thread, 'turns': []}
                    loaded_session_id = None
                    if path:
                        if str(path) not in sessions:
                            # ponytail: eight in-memory sessions; evict oldest if many windows are open.
                            if len(sessions) >= 8: sessions.pop(next(iter(sessions)))
                            sessions[str(path)] = Session()
                        session = sessions[str(path)]; session.read(path)
                        loaded_session_id = session.thread_id
                        if session.thread_id == thread:
                            payload['turns'] = session.results()
                    result = client.evaluate(f'({overlay})({json.dumps(payload, ensure_ascii=False)})')
                    if not isinstance(result, dict) or not result.get('ok'):
                        raise CDPError('TPS overlay did not return a valid result.')
                    count = result.get('rendered', 0)
                    rendered += count
                    if not thread: stage = 'thread_not_found'
                    elif not path: stage = 'session_not_found'
                    elif loaded_session_id != thread: stage = 'session_id_mismatch'
                    elif result.get('thread_id') != thread: stage = 'thread_changed'
                    elif not payload['turns']: stage = 'no_completed_turns'
                    elif not count: stage = 'reply_not_matched'
                    else: stage = 'rendered'
                    # Log only identifiers/counts already used by the monitor; no reply text.
                    diagnostics.append({'window_id':key, 'active_thread_id':thread,
                        'session_found':bool(path), 'loaded_session_id':loaded_session_id,
                        'completed_turns':len(payload['turns']), 'rendered':count, 'stage':stage})
                (STATE / 'status.json').write_text(json.dumps({'updated_at': time.time(),
                    'build_id':build_id, 'port':args.port, 'windows':len(pages),
                    'rendered':rendered, 'diagnostics':diagnostics, 'error':None}))
                if not args.remove and diagnostics != last_diagnostics:
                    print(json.dumps({'diagnostics':diagnostics}, ensure_ascii=False), flush=True)
                    last_diagnostics = diagnostics
                if last_error or args.once:
                    print(f'TPS monitor connected: {len(pages)} window(s), {rendered} turn(s).', flush=True)
                last_error = None
                if args.once or args.remove: return 0
            except (CDPError, OSError, ValueError, KeyError, TypeError) as exc:
                for client in clients.values(): client.close()
                clients.clear()
                message = str(exc)
                (STATE / 'status.json').write_text(json.dumps({'updated_at':time.time(),
                    'build_id':build_id, 'port':args.port, 'error':message}))
                if message != last_error:
                    print(message, file=sys.stderr, flush=True); last_error = message
                if args.once or args.remove: return 1
            time.sleep(5 if args.auto_port and last_error else 2)
    finally:
        for client in clients.values():
            try:
                if not args.once: client.evaluate('window.__codexTPSMonitor?.stop()')
            except (CDPError, OSError, ValueError): pass
            client.close()
        if not args.remove:
            (STATE / 'monitor.pid').unlink(missing_ok=True)
        lock.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
