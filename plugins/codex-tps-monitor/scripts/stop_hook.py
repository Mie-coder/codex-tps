#!/usr/bin/env python3
"""Official synchronous Stop hook. Reads logs; stores statistics, never chat text."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from uuid import UUID

from tps_stats import Session


def codex_home():
    return Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).resolve()


def data_dir():
    fallback = codex_home() / 'plugins/data/codex-tps-monitor-mie-codex-tps'
    return Path(os.environ.get('PLUGIN_DATA', fallback)) / 'tps-results'


def collect(event, now=None):
    if not isinstance(event, dict) or event.get('hook_event_name') != 'Stop':
        raise ValueError('Expected a Stop event')
    session_id, turn_id = event.get('session_id'), event.get('turn_id')
    UUID(session_id); UUID(turn_id)
    record = {'session_id': session_id, 'turn_id': turn_id, 'model': event.get('model'),
              'observed_at': time.time() if now is None else now}
    transcript = event.get('transcript_path')
    if not transcript:
        return {**record, 'unavailable': '此轮没有提供可读取的会话日志'}
    path = Path(transcript).resolve(strict=True)
    path.relative_to((codex_home() / 'sessions').resolve())
    # ponytail: reparse one transcript per Stop; use a tail reader if large logs exceed the timeout.
    session = Session()
    session.read(path)
    if session.thread_id != session_id:
        raise ValueError('Transcript session mismatch')
    turn = session.turns.get(turn_id)
    if turn is None:
        return {**record, 'unavailable': '会话日志中还没有对应轮次'}
    record['transcript_status'] = turn.status
    snapshot = copy.deepcopy(turn)
    if snapshot.status == 'running':
        # Stop precedes task_complete: measure up to script entry, excluding statistics parsing.
        snapshot.finish({}, record['observed_at'], 'completed')
    record['metrics'] = snapshot.result()
    record['metrics']['status'] = 'stop_checkpoint'
    return record


def format_message(record):
    metrics = record.get('metrics', {})
    if metrics.get('wall_tps') is None:
        reason = record.get('unavailable') or '；'.join(metrics.get('notes', [])) or '本轮用量或耗时不完整'
        return f'TPS 提示详情\n统计不可用：{reason}。'
    reasoning = metrics.get('reasoning_tokens')
    effective = metrics.get('effective_tps')
    model = '、'.join(metrics.get('models', [])) or record.get('model') or '未知'
    return '\n'.join([
        'TPS 提示详情',
        f"整轮平均 TPS：{metrics['wall_tps']:.2f} token/s（估算）",
        f"有效 TPS：{effective:.2f} token/s（估算）" if effective is not None else '有效 TPS：不可用',
        f"输出：{metrics['output_tokens']} token；其中推理：{reasoning if reasoning is not None else '未知'} token",
        f"整轮耗时：{metrics['wall_seconds']:.3f} 秒；模型请求次数：{metrics['response_count']}",
        f'模型：{model}',
        '整轮耗时截至统计开始；有效 TPS 排除可识别的工具等待，包含请求与首 token 等待。',
        '输出量已包含推理和模型生成的工具调用；未计入输入、工具返回或子 Agent 输出。',
    ])


def save(record, directory=None):
    UUID(record['session_id'])
    directory = data_dir() if directory is None else directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / (record['session_id'] + '.json')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                         suffix='.tmp', delete=False) as file:
            temporary = Path(file.name)
            json.dump(record, file, ensure_ascii=False, indent=2)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def status():
    directories = {data_dir()}
    directories.update(p / 'tps-results' for p in (codex_home() / 'plugins/data').glob('*codex-tps-monitor*'))
    files = sorted((p for d in directories for p in d.glob('*.json')), key=lambda p: p.stat().st_mtime, reverse=True)
    records = [json.loads(p.read_text()) for p in files[:5]]
    print(json.dumps({'mode': 'Stop Hook', 'latest_statistics': records}, ensure_ascii=False, indent=2))


def main():
    if sys.argv[1:] == ['--status']:
        status()
        return
    if sys.argv[1:]:
        raise SystemExit('Usage: stop_hook.py [--status]')
    started = time.time()
    try:
        event = json.load(sys.stdin)
        record = collect(event, started)
    except Exception as error:
        record = {'unavailable': '未能验证本轮日志和用量', 'error_type': type(error).__name__}
    try:
        if record.get('session_id'):
            save(record)
    except (OSError, ValueError):
        pass  # Diagnostic storage failure must not prevent the native result from being delivered.
    print(json.dumps({'systemMessage': format_message(record)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
