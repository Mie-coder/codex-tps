import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tps_stats import Session


def row(second, kind, **payload):
    return {'timestamp': f'2026-10-07T00:{second // 60:02}:{second % 60:02}.000Z',
            'type': kind, 'payload': payload}


def usage(second, response, output, total, reasoning=0):
    return row(second, 'token_usage_record', turn_id='turn1', response_id=response,
               usage={'output_tokens': output, 'reasoning_output_tokens': reasoning},
               turn_token_usage={'output_tokens': total})


def fixture():
    return [
        row(0, 'session_meta', id='thread1'),
        row(0, 'event_msg', type='task_started', turn_id='turn1'),
        row(0, 'response_item', type='message', role='user'),
        row(20, 'response_item', type='custom_tool_call', call_id='tool1'),
        usage(20, 'r1', 600, 600, 200),
        row(57, 'response_item', type='custom_tool_call_output', call_id='tool1'),
        row(57, 'event_msg', type='token_count', info={'last_token_usage': {'output_tokens': 600}}),
        row(97, 'response_item', type='message', role='assistant', phase='final_answer', id='msg1'),
        usage(97, 'r2', 1212, 1812, 412),
        row(97, 'event_msg', type='task_complete', turn_id='turn1', duration_ms=97000),
    ]


def parse(rows):
    s = Session()
    for r in rows:
        s.consume(r)
    return s


class StatsTests(unittest.TestCase):
    def test_final_phase_tool_call_is_not_a_final_reply_anchor(self):
        rows = fixture()
        rows[3]['payload'].update(id='call1', phase='final_answer')
        self.assertEqual(parse(rows).results()[0]['final_ids'], ['msg1'])

    def test_weighted_turn_excludes_tool_wait_and_never_double_counts_reasoning(self):
        result = parse(fixture()).results()[0]
        self.assertEqual(result['output_tokens'], 1812)
        self.assertEqual(result['reasoning_tokens'], 612)
        self.assertEqual(result['model_seconds'], 60)
        self.assertAlmostEqual(result['effective_tps'], 30.2)
        self.assertAlmostEqual(result['wall_tps'], 1812 / 97)
        self.assertEqual(result['final_ids'], ['msg1'])

    def test_replayed_response_is_not_counted_twice(self):
        rows = fixture()
        rows.insert(5, rows[4])
        self.assertEqual(parse(rows).results()[0]['effective_tps'], 30.2)

    def test_running_and_interrupted_turn_never_claim_completed_tps(self):
        s = parse(fixture()[:-1])
        self.assertEqual(s.results(), [])
        s.consume(row(98, 'event_msg', type='turn_aborted', turn_id='turn1'))
        self.assertEqual(s.results()[0]['status'], 'interrupted')
        self.assertIsNone(s.results()[0]['wall_tps'])

    def test_missing_sampling_boundary_preserves_only_reliable_wall_tps(self):
        rows = [r for r in fixture() if r['payload'].get('type') != 'custom_tool_call']
        result = parse(rows).results()[0]
        self.assertIsNone(result['effective_tps'])
        self.assertAlmostEqual(result['wall_tps'], 1812 / 97)

    def test_mismatched_total_or_zero_duration_never_produces_a_number(self):
        for change in ('total', 'duration'):
            rows = fixture()
            if change == 'total':
                rows[-2]['payload']['turn_token_usage']['output_tokens'] = 9999
            else:
                rows[-1]['payload']['duration_ms'] = 0
            result = parse(rows).results()[0]
            self.assertIsNone(result['wall_tps'])
            self.assertIsNone(result['effective_tps'])

    def test_legacy_token_counts_deduplicate_and_exclude_tool_time(self):
        rows = fixture()
        rows = [r for r in rows if r['payload'].get('type') != 'token_count']
        for i, r in enumerate(rows):
            if r['type'] == 'token_usage_record':
                p = r['payload']
                rows[i] = {**r, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                    'last_token_usage': p['usage'], 'total_token_usage': p['turn_token_usage']}}}
        rows.insert(5, rows[4])
        self.assertEqual(parse(rows).results()[0]['effective_tps'], 30.2)

    def test_incremental_read_waits_for_complete_line_and_resets_after_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rollout.jsonl'
            content = ''.join(json.dumps(r) + '\n' for r in fixture())
            p.write_text(content[:-20])
            s = Session(); s.read(p)
            self.assertEqual(s.results(), [])
            with p.open('a') as f:
                f.write(content[-20:])
            s.read(p)
            self.assertEqual(s.results()[0]['effective_tps'], 30.2)
            replacement = p.with_suffix('.tmp'); replacement.write_text('')
            replacement.replace(p); s.read(p)
            self.assertEqual(s.results(), [])

    def test_foreign_turn_usage_does_not_leak_into_active_turn(self):
        rows = fixture(); foreign = usage(30, 'other-response', 10000, 10000)
        foreign['payload']['turn_id'] = 'other-turn'; rows.insert(5, foreign)
        self.assertEqual(parse(rows).results()[0]['output_tokens'], 1812)

    def test_effective_tps_unavailable_when_model_overlaps_outstanding_tool(self):
        rows = [r for r in fixture() if r['payload'].get('type') != 'custom_tool_call_output']
        self.assertIsNone(parse(rows).results()[0]['effective_tps'])

    def test_next_turn_does_not_inherit_previous_tokens_or_duration(self):
        s = parse(fixture())
        s.consume(row(100, 'event_msg', type='task_started', turn_id='turn2'))
        s.consume(row(110, 'response_item', type='message', role='assistant', phase='final_answer', id='msg2'))
        record = usage(110, 'r3', 100, 100, 20); record['payload']['turn_id'] = 'turn2'
        s.consume(record)
        s.consume(row(110, 'event_msg', type='task_complete', turn_id='turn2', duration_ms=10000))
        self.assertEqual([r['output_tokens'] for r in s.results()], [1812, 100])
        self.assertEqual(s.results()[1]['wall_tps'], 10)

    def test_duplicate_log_channels_never_change_modern_usage(self):
        rows = fixture()
        rows.insert(-1, row(97, 'event_msg', type='token_count', info={
            'total_token_usage':{'output_tokens':1812}, 'last_token_usage':{'output_tokens':1212}}))
        self.assertEqual(parse(rows).results()[0]['output_tokens'], 1812)


if __name__ == '__main__':
    unittest.main()
