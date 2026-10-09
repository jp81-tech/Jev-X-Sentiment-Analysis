"""Standalone offline integration tests: python -m unittest discover -s tests -p test_launchd_offline.py."""
import contextlib
import io
import json
import multiprocessing
from pathlib import Path
import plistlib
import socket
import tempfile
import unittest
from unittest.mock import patch

from scripts import auto_analyze as client
from scripts import launchd_auto_analyze as job

NOW = 2000000000


def hold_lock(path, ready, release):
    with open(path, 'a') as stream:
        client.fcntl.flock(stream, client.fcntl.LOCK_EX)
        ready.set()
        release.wait(10)


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / 'decisions.jsonl'
        self.ledger = self.root / 'runs.jsonl'
        self.calls = []
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def fake(self, port, path, body, token, timeout):
        self.calls.append(path)
        if path == '/health':
            return {'status': 'healthy', 'twitter_configured': True, 'typesafe_configured': True}, 200, None
        return None, 503, 'HTTP_ERROR'

    def invoke(self, execute=False, fake=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = job.run(self.journal, self.ledger, execute, fake or self.fake, lambda: NOW)
        return code, json.loads(output.getvalue())

    def state(self, reservations=0):
        self.journal.touch()
        self.ledger.write_text(''.join(json.dumps(client.event('attempt_reserved', 'ADA', 50,
                              NOW - 100, f'{i:032x}')) + '\n' for i in range(reservations)))

    def test_default_start_is_read_only(self):
        code, result = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result['mode'], 'DRY_RUN')
        self.assertEqual(result['requests'], 0)
        self.assertEqual(result['counts'], {'PLANNED': 3})
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_disabled_plist_round_trip(self):
        config = plistlib.loads(plistlib.dumps(job.configuration('/usr/bin/python3', self.journal, self.ledger)))
        self.assertTrue(config['Disabled'])
        self.assertFalse(config['RunAtLoad'])
        self.assertFalse(config['KeepAlive'])
        self.assertNotIn('--execute', config['ProgramArguments'])
        self.assertEqual(config['StartInterval'], 18000)
        self.assertIn(str(self.ledger), config['ProgramArguments'])

    def test_missing_state_cannot_reset_paid_budget(self):
        self.assertEqual(self.invoke(True)[1]['code'], 'EXISTING_STATE_REQUIRED')
        self.assertEqual(self.calls, [])

    def test_overlap_with_direct_client_process(self):
        self.state()
        ctx = multiprocessing.get_context('spawn')
        ready, release = ctx.Event(), ctx.Event()
        process = ctx.Process(target=hold_lock, args=(self.ledger, ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(5))
            self.assertEqual(self.invoke(True)[1]['code'], 'RUN_LOCKED')
            self.assertEqual(self.calls, [])
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(process.exitcode, 0)

    def test_exhausted_rolling_budget_precedes_api(self):
        self.state(16)
        self.assertEqual(self.invoke(True)[1]['code'], 'DAILY_BUDGET_EXHAUSTED')
        self.assertEqual(self.calls, [])

    def test_only_one_remaining_attempt(self):
        self.state(15)
        self.assertEqual(self.invoke(True)[1]['requests'], 1)
        self.assertEqual(self.calls.count('/api/v1/analyze'), 1)
        self.calls.clear()
        self.assertEqual(self.invoke(True)[1]['code'], 'DAILY_BUDGET_EXHAUSTED')
        self.assertEqual(self.calls, [])

    def test_api_failure_then_restart_preserves_gaps(self):
        self.state()
        code, result = self.invoke(True)
        self.assertEqual(code, 1)
        self.assertEqual(result['counts'], {'HTTP_ERROR': 3})
        self.calls.clear()
        self.assertEqual(self.invoke(True)[1]['counts'], {'SKIPPED_GAP': 3})
        self.assertNotIn('/api/v1/analyze', self.calls)

    def test_crash_after_reservation_then_restart(self):
        self.state()
        def crash(*args):
            if args[1] == '/api/v1/analyze':
                raise KeyboardInterrupt()
            return self.fake(*args)
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(True, crash)
        entries = client.rows(self.ledger.read_bytes(), 'BAD')
        self.assertEqual(client.budget_state(entries, NOW)[0], 1)
        self.assertEqual(self.invoke(True)[1]['requests'], 2)

    def test_unhealthy_api_does_not_reserve(self):
        self.state()
        self.assertEqual(self.invoke(True, lambda *a: (None, None, 'TIMEOUT'))[1]['code'], 'TIMEOUT')
        self.assertEqual(self.ledger.read_bytes(), b'')

    def test_corruption_and_clock_rollback_block(self):
        self.state()
        for data in ('{"truncated":', json.dumps(client.event('attempt_reserved', 'ETH', 50, NOW+1, 'a'*32))+'\n'):
            self.ledger.write_text(data)
            self.assertEqual(self.invoke(True)[1]['code'], 'RUN_LOG_CORRUPT')
        self.assertEqual(self.calls, [])

    def test_audit_unknown_platform(self):
        with patch.object(job.sys, 'platform', 'linux'):
            self.assertEqual(job.audit()['status'], 'UNKNOWN')

    def test_existing_schedules_or_failed_inspection_block(self):
        directory = self.root / 'Library/LaunchAgents'
        directory.mkdir(parents=True)
        (directory / 'old.plist').write_bytes(plistlib.dumps({'ProgramArguments': ['python', 'auto_analyze.py']}))
        def inspect(*args, **kwargs):
            return job.subprocess.CompletedProcess(args[0], 0, '', '')
        with patch.object(job.sys, 'platform', 'darwin'):
            result = job.audit(self.root, inspect)
        self.assertEqual(result['status'], 'REVIEW_REQUIRED')
        self.assertTrue(any('EXISTING_PLIST' in x for x in result['findings']))

    def test_prepare_never_installs_or_overwrites(self):
        with patch.object(job, 'audit', return_value={'status': 'NO_MATCH_IN_CHECKED_SOURCES', 'findings': []}):
            output = self.root / 'agent.plist'
            args = ['prepare', '--python', job.sys.executable, '--log', str(self.journal),
                    '--run-log', str(self.ledger), '--output', str(output)]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(job.main(args), 0)
                before = output.read_bytes()
                self.assertEqual(job.main(args), 78)
                self.assertEqual(output.read_bytes(), before)
                args[-1] = str(self.root / 'Library/LaunchAgents/agent.plist')
                self.assertEqual(job.main(args), 78)


if __name__ == '__main__':
    unittest.main()
