import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import monitor


class MonitorTests(unittest.TestCase):
    def test_reconnect_after_closed_client_and_changed_port(self):
        page = {'id':'window', 'webSocketDebuggerUrl':'not-connected'}
        def target(port):
            if port == 9223: return [page]
            raise monitor.CDPError('closed')
        with patch.object(monitor, 'targets', side_effect=target):
            self.assertEqual(monitor.available_pages(9222, True), (9223, [page]))
            with self.assertRaises(monitor.CDPError):
                monitor.available_pages(9222)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory); handlers = {}; snapshots = []
            client = Mock()
            client.evaluate.side_effect = ['thread', {'ok':True, 'thread_id':'thread', 'rendered':1},
                'thread', {'ok':True, 'thread_id':'thread', 'rendered':1}, None]
            session = Mock(thread_id='thread'); session.results.return_value = [{}]
            def sleep(_seconds):
                snapshots.append(json.loads((state / 'status.json').read_text()))
                if len(snapshots) == 3: handlers[monitor.signal.SIGTERM](None, None)
            with patch.object(monitor, 'STATE', state), \
                 patch.object(monitor, 'available_pages', side_effect=[
                     (9222,[page]), monitor.CDPError('closed'), (9223,[page])]), \
                 patch.object(monitor, 'CDPClient', return_value=client) as connect, \
                 patch.object(monitor, 'find_session', return_value=Path('/test.jsonl')), \
                 patch.object(monitor, 'Session', return_value=session), \
                 patch.object(monitor.signal, 'signal', side_effect=lambda key, handler: handlers.update({key:handler})), \
                 patch.object(monitor.time, 'sleep', side_effect=sleep), \
                 patch.object(sys, 'argv', ['monitor.py', '--auto-port']), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(monitor.main(), 0)
            self.assertEqual(connect.call_count, 2)
            self.assertEqual([item.get('error') for item in snapshots], [None, 'closed', None])
            self.assertEqual(snapshots[-1]['port'], 9223)
            self.assertEqual(snapshots[-1]['rendered'], 1)
            self.assertFalse((state / 'monitor.pid').exists())

    def test_status_distinguishes_each_pipeline_failure_without_connecting_to_app(self):
        cases = [
            ('', None, '', 0, '', 0, 'thread_not_found'),
            ('thread', None, '', 0, 'thread', 0, 'session_not_found'),
            ('thread', Path('/test.jsonl'), 'other', 1, 'thread', 0, 'session_id_mismatch'),
            ('thread', Path('/test.jsonl'), 'thread', 1, 'other', 0, 'thread_changed'),
            ('thread', Path('/test.jsonl'), 'thread', 0, 'thread', 0, 'no_completed_turns'),
            ('thread', Path('/test.jsonl'), 'thread', 1, 'thread', 0, 'reply_not_matched'),
            ('thread', Path('/test.jsonl'), 'thread', 1, 'thread', 1, 'rendered'),
        ]
        for thread, log_path, session_id, turns, display_thread, rendered, stage in cases:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                client = Mock()
                client.evaluate.side_effect = [thread, {'ok':True, 'thread_id':display_thread, 'rendered':rendered}]
                session = Mock(thread_id=session_id)
                session.results.return_value = [{}] * turns
                with patch.object(monitor, 'STATE', state), \
                     patch.object(monitor, 'targets', return_value=[{'id':'window', 'webSocketDebuggerUrl':'not-connected'}]), \
                     patch.object(monitor, 'CDPClient', return_value=client), \
                     patch.object(monitor, 'find_session', return_value=log_path), \
                     patch.object(monitor, 'Session', return_value=session), \
                     patch.object(monitor.signal, 'signal'), \
                     patch.object(sys, 'argv', ['monitor.py', '--once']), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(monitor.main(), 0)
                status = json.loads((state / 'status.json').read_text())
                self.assertEqual(status['diagnostics'][0]['stage'], stage)
                self.assertEqual(status['rendered'], rendered)
                self.assertIsNone(status['error'])
                self.assertEqual(len(status['build_id']), 12)
                self.assertFalse((state / 'monitor.pid').exists())
