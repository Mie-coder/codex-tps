"""Read-only, incremental per-turn statistics. All timestamps are log estimates."""
from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError, TypeError, OverflowError):
        return None


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


class Turn:
    def __init__(self, turn_id, start):
        self.id = turn_id
        self.start = start
        self.end = None
        self.duration = None
        self.status = 'running'
        self.final_ids = []
        self.user_ids = []
        self.models = set()
        self.responses = {}
        self.mode = None
        self.expected_total = None
        self.last_legacy_total = None
        self.sample_start = start
        self.generated_end = None
        self.next_input = None
        self.pending_tools = set()
        self.usage_errors = set()
        self.timing_errors = set()
        self.last_timestamp = start

    def item(self, p, now):
        kind = p.get('type', '')
        generated = kind == 'reasoning' or kind.endswith('_call') or (kind == 'message' and p.get('role') == 'assistant')
        is_input = kind.endswith('_call_output') or (kind == 'message' and p.get('role') in ('user', 'developer', 'system'))
        if generated:
            if self.generated_end is None and self.pending_tools:
                self.timing_errors.add('模型响应与未结束工具重叠')
            self.generated_end = now
            if kind.endswith('_call') and p.get('call_id'):
                self.pending_tools.add(p['call_id'])
            if kind == 'message' and p.get('phase') == 'final_answer' and p.get('id'):
                self.final_ids.append(p['id'])
        elif is_input:
            if kind.endswith('_call_output'):
                self.pending_tools.discard(p.get('call_id'))
            if self.generated_end is None:
                self.sample_start = now
            else:
                # Older Codex writes usage after tool output. Retain both boundaries.
                self.next_input = now

    def usage(self, key, usage, now, expected=None):
        if key in self.responses:
            if self.responses[key]['output'] != usage.get('output_tokens'):
                self.usage_errors.add('重复响应的用量不一致')
            return
        output = number(usage.get('output_tokens'))
        reasoning = number(usage.get('reasoning_output_tokens'))
        if output is None or int(output) != output:
            self.usage_errors.add('输出 token 缺失或无效')
        if reasoning is not None and output is not None and reasoning > output:
            self.usage_errors.add('推理 token 超出总输出')
        seconds = None
        if self.sample_start is not None and self.generated_end is not None:
            seconds = self.generated_end - self.sample_start
        if seconds is None or seconds <= 0:
            self.timing_errors.add('模型响应时间边界不完整')
            seconds = None
        self.responses[key] = {'output': output, 'reasoning': reasoning, 'seconds': seconds}
        if expected is not None:
            self.expected_total = expected
        self.sample_start = self.next_input if self.next_input is not None else now
        self.generated_end = self.next_input = None

    def finish(self, p, now, status):
        self.end = now
        self.status = status
        if 'duration_ms' in p:
            ms = number(p['duration_ms'])
            self.duration = ms / 1000 if ms is not None else None
        elif now is not None and self.start is not None:
            self.duration = now - self.start

    def result(self):
        records = list(self.responses.values())
        output = sum(r['output'] or 0 for r in records)
        reasoning = sum(r['reasoning'] for r in records) if records and all(r['reasoning'] is not None for r in records) else None
        errors = set(self.usage_errors)
        if not records:
            errors.add('缺少输出用量')
        if self.expected_total is not None and self.expected_total != output:
            errors.add('分次输出与整轮用量不一致')
        if self.duration is None or self.duration <= 0:
            errors.add('整轮耗时无效')
        model_seconds = sum(r['seconds'] or 0 for r in records)
        timing_errors = set(self.timing_errors)
        if self.generated_end is not None:
            timing_errors.add('部分模型输出尚无用量记录')
            errors.add('部分模型输出尚无用量记录')
        if not model_seconds or (self.duration and model_seconds > self.duration + .05):
            timing_errors.add('模型响应耗时无效')
        valid = self.status == 'completed' and not errors
        return {
            'turn_id': self.id, 'status': self.status,
            'final_ids': sorted(set(self.final_ids)), 'user_ids': sorted(set(self.user_ids)),
            'output_tokens': output if records and not errors else None,
            'reasoning_tokens': reasoning if not errors else None,
            'response_count': len(records), 'models': sorted(self.models),
            'wall_seconds': self.duration,
            'model_seconds': model_seconds if valid and not timing_errors else None,
            'wall_tps': output / self.duration if valid else None,
            'effective_tps': output / model_seconds if valid and not timing_errors else None,
            'notes': sorted(errors | timing_errors),
        }


class Session:
    def __init__(self):
        self.thread_id = None
        self.turns = {}
        self.active = None
        self.offset = 0
        self.identity = None

    def read(self, path):
        path = Path(path)
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if self.identity != identity or stat.st_size < self.offset:
            self.__init__()
            self.identity = identity
        with path.open('rb') as f:
            f.seek(self.offset)
            while True:
                line = f.readline()
                if not line or not line.endswith(b'\n'):
                    break
                self.offset = f.tell()
                try:
                    self.consume(json.loads(line))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if self.active in self.turns:
                        self.turns[self.active].usage_errors.add('日志包含损坏记录')

    def consume(self, row):
        if not isinstance(row, dict) or not isinstance(row.get('payload'), dict):
            return
        p = row['payload']; kind = row.get('type'); event = p.get('type')
        now = timestamp(row.get('timestamp'))
        if kind == 'session_meta':
            self.thread_id = p.get('id') or p.get('session_id')
            return
        if kind == 'event_msg' and event == 'task_started':
            turn_id = p.get('turn_id')
            if turn_id and turn_id not in self.turns:
                self.turns[turn_id] = Turn(turn_id, now)
            self.active = turn_id
            return
        explicit_turn = p.get('turn_id')
        turn = self.turns.get(explicit_turn or self.active)
        if turn is None:
            return
        if now is None or (turn.last_timestamp is not None and now < turn.last_timestamp):
            turn.timing_errors.add('时间戳缺失或倒退')
        turn.last_timestamp = now
        if kind == 'turn_context':
            if p.get('model'):
                turn.models.add(p['model'])
        elif kind == 'response_item':
            turn.item(p, now)
        elif kind == 'token_usage_record':
            if p.get('thread_id') and self.thread_id and p['thread_id'] != self.thread_id:
                return
            if turn.mode == 'legacy':
                turn.usage_errors.add('同轮混合用量格式，需核对')
            turn.mode = 'modern'
            response_id = p.get('response_id')
            if not response_id:
                turn.usage_errors.add('响应 ID 缺失')
                return
            turn.usage(response_id, p.get('usage') or {}, now,
                       number((p.get('turn_token_usage') or {}).get('output_tokens')))
        elif kind == 'event_msg':
            if event == 'item_completed':
                item = p.get('item') or {}
                if item.get('type') == 'UserMessage' and item.get('id'):
                    turn.user_ids.append(item['id'])
                if item.get('type') == 'AgentMessage' and item.get('phase') == 'final_answer' and item.get('id'):
                    turn.final_ids.append(item['id'])
            elif event == 'token_count' and turn.mode != 'modern':
                info = p.get('info') or {}
                total = number((info.get('total_token_usage') or {}).get('output_tokens'))
                if total is None:
                    turn.usage_errors.add('旧格式缺少累计用量，无法去重')
                    return
                if total == turn.last_legacy_total:
                    return
                if turn.last_legacy_total is not None and total < turn.last_legacy_total:
                    turn.usage_errors.add('累计用量重置')
                turn.last_legacy_total = total; turn.mode = 'legacy'
                turn.usage('legacy:' + str(total), info.get('last_token_usage') or {}, now)
            elif event == 'task_complete':
                turn.finish(p, now, 'completed')
            elif event in ('turn_aborted', 'task_aborted', 'task_failed'):
                turn.finish(p, now, 'failed' if event == 'task_failed' else 'interrupted')
            elif event == 'context_compacted':
                turn.timing_errors.add('本轮发生上下文压缩')

    def results(self):
        return [t.result() for t in self.turns.values() if t.status != 'running']
