import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from stop_hook import collect, format_message, save
from tps_stats import timestamp
from test_stats import fixture


class StopHookTests(unittest.TestCase):
    def test_checkpoint_usage_privacy_missing_data_and_wrong_session(self):
        session_id = '00000000-0000-4000-8000-000000000001'
        turn_id = '00000000-0000-4000-8000-000000000003'
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'CODEX_HOME': temp}):
            path = Path(temp) / 'sessions' / 'test.jsonl'
            path.parent.mkdir()
            rows = fixture()[:-1]
            for row in rows:
                payload = row['payload']
                if payload.get('id') == 'thread1': payload['id'] = session_id
                if payload.get('turn_id') == 'turn1': payload['turn_id'] = turn_id
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            event = {'hook_event_name': 'Stop', 'session_id': session_id, 'turn_id': turn_id,
                     'transcript_path': str(path), 'model': 'gpt-6.1-sol',
                     'last_assistant_message': 'PRIVATE_TEXT_SHOULD_NOT_BE_SAVED'}
            result = collect(event, timestamp('2026-10-07T00:01:38Z'))
            self.assertEqual(result['metrics']['output_tokens'], 1812)
            self.assertEqual(result['metrics']['reasoning_tokens'], 612)
            self.assertAlmostEqual(result['metrics']['wall_tps'], 1812 / 98)
            self.assertAlmostEqual(result['metrics']['effective_tps'], 30.2)
            self.assertEqual(result['metrics']['status'], 'stop_checkpoint')
            self.assertNotIn('PRIVATE_TEXT', json.dumps(result))
            self.assertIn('18.49 token/s', format_message(result))
            saved = save(result, Path(temp) / 'data')
            self.assertEqual(json.loads(saved.read_text())['turn_id'], turn_id)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(saved.parent.glob('*.tmp')))
            with self.assertRaises(ValueError):
                collect({**event, 'session_id': '00000000-0000-4000-8000-000000000004'})
            with self.assertRaises(ValueError):
                collect({**event, 'transcript_path': '/etc/hosts'})
            self.assertIn('不可用', format_message(collect({**event, 'transcript_path': None})))
            rows = [row for row in rows if row['type'] != 'token_usage_record']
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            self.assertIsNone(collect(event, timestamp('2026-10-07T00:01:38Z'))['metrics']['wall_tps'])


if __name__ == '__main__':
    unittest.main()
