import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_diff.recorder import Recorder, file_targets
from session_diff.store import Store
from aws import MockAWS
from session_diff.capture import snapshot, workspace


class RecorderTests(MockAWS, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'workspace'
        self.work.mkdir()
        self.store = self.new_store()
        work = self.work

        class Paths:
            root = work
            blocked = None
            def resolve(self, path):
                return work / path

        self.recorder = Recorder(store_factory=lambda: self.store, paths_factory=lambda task: Paths())
        self.recorder.begin_run(session_id='session-a')

    def call(self, operation, tool='write_file', args=None, session='session-a'):
        return self.recorder.execute(next_call=lambda args: operation(), args=args or {'path': 'app.txt'},
                                     tool_name=tool, session_id=session, tool_call_id='call')

    def test_dirty_baseline_restored_history_and_reopen(self):
        path = self.work / 'app.txt'
        path.write_text('user edit\n', encoding='utf-8')
        self.assertEqual(self.call(lambda: (path.write_text('agent edit\n'), 'ok')[1]), 'ok')
        self.call(lambda: path.write_text('user edit\n'))
        reopened = self.new_store()
        file = reopened.view('session-a')['files'][0]
        self.assertEqual((file['status'], file['edits']), ('restored', 2))
        self.assertEqual(reopened.diff('session-a', file['first_id'], file['last_id'])['patch'], '')
        patch = reopened.diff('session-a', file['first_id'], file['first_id'])['patch']
        self.assertIn('-user edit', patch)
        self.assertIn('+agent edit', patch)
        self.assertEqual(len(reopened.history('session-a', file['first_id'], file['last_id'])), 2)

    def test_failure_records_partial_write_without_retry(self):
        calls = []
        def operation():
            calls.append(1)
            (self.work / 'app.txt').write_text('partial')
            raise ValueError('tool failed')
        with self.assertRaisesRegex(ValueError, 'tool failed'):
            self.call(operation)
        self.assertEqual(calls, [1])
        self.assertEqual(self.store.view('session-a')['files'][0]['status'], 'added')
        self.recorder.store_factory = lambda: (_ for _ in ()).throw(OSError('disk'))
        self.assertEqual(self.call(lambda: 'still works'), 'still works')

    def test_terminal_create_delete_and_git_ignored(self):
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        subprocess.run(['git', 'init', '-q', str(self.work)], check=True, **options)
        (self.work / '.gitignore').write_text('ignored.txt\n')
        (self.work / 'old.txt').write_text('old\n')
        (self.work / 'ignored.txt').write_text('private old\n')
        def operation():
            (self.work / 'old.txt').unlink()
            (self.work / 'new.txt').write_text('new\n')
            (self.work / 'ignored.txt').write_text('private new\n')
        self.call(operation, 'terminal', {'command': 'fixture'})
        files = self.store.view('session-a')['files']
        self.assertEqual({Path(f['path']).name: f['status'] for f in files}, {'old.txt': 'deleted', 'new.txt': 'added'})
        self.assertTrue(all(f['observed'] for f in files))

    def test_workspace_below_alias_excludes_links_inside_workspace(self):
        physical = self.root / 'physical'
        physical.mkdir()
        chosen = physical / 'parent' / 'chosen'
        chosen.mkdir(parents=True)
        (chosen / 'normal.txt').write_text('record this')
        outside = physical / 'outside'
        outside.mkdir()
        (outside / 'private.txt').write_text('do not record')
        alias = self.root / 'alias'
        try:
            alias.symlink_to(physical, target_is_directory=True)
            (chosen / 'linked').symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'Directory symlinks unavailable: {exc}')
        captured, _, notes = workspace(alias / 'parent' / 'chosen')
        self.assertEqual({Path(name).name for name in captured}, {'normal.txt'})
        self.assertEqual(notes, [])

    def test_run_isolation_and_intervening_edits(self):
        path = self.work / 'app.txt'
        path.write_text('original\n')
        self.call(lambda: path.write_text('first\n'))
        self.recorder.begin_run(session_id='session-a')
        self.assertEqual(self.store.view('session-a')['files'], [])
        path.write_text('outside change\n')
        self.call(lambda: path.write_text('second\n'))
        self.assertTrue(self.store.view('session-a', 'all')['files'][0]['gap'])
        self.assertFalse(self.store.view('session-a')['files'][0]['gap'])
        self.assertEqual(self.store.view('other-session')['files'], [])
        first = self.store.view('session-a')['files'][0]
        with self.assertRaises(LookupError):
            self.store.diff('other-session', first['first_id'], first['last_id'])

    def test_secrets_binary_and_non_utf8(self):
        self.call(lambda: (self.work / '.env').write_text('SECRET_TOKEN=abc'), args={'path': '.env'})
        self.assertEqual(self.store.view('session-a')['files'], [])
        self.call(lambda: (self.work / 'app.txt').write_bytes(b'\xff\x00'))
        file = self.store.view('session-a')['files'][0]
        self.assertEqual(file['after']['kind'], 'binary')
        self.assertIn('Binary', self.store.diff('session-a', file['first_id'], file['last_id'])['note'])
        self.assertEqual(self.blob_count(), 0)

    def test_patch_paths_and_newline_export(self):
        self.assertEqual(file_targets({'patch': '*** Update File: a\n*** Move to: b\n*** Delete File: c'}), ['a', 'b', 'c'])
        path = self.work / 'app.txt'
        path.write_bytes(b'before')
        self.call(lambda: path.write_bytes(b'after'))
        file = self.store.view('session-a')['files'][0]
        patch = self.store.diff('session-a', file['first_id'], file['last_id'])['patch']
        self.assertEqual(patch.count('\\ No newline at end of file'), 2)
        self.assertIn('--- a/app.txt', patch)
        path.write_bytes(b'before')
        patch_file = self.root / 'change.patch'
        patch_file.write_text(patch, encoding='utf-8', newline='')
        result = subprocess.run(['git', 'apply', '--check', str(patch_file)], cwd=self.work, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_scan_does_not_invent_additions(self):
        known = self.work / 'known.txt'
        unseen = self.work / 'unseen.txt'
        known.write_text('old')
        unseen.write_text('preexisting')
        old_meta, old_data = snapshot(known)
        new_meta, new_data = snapshot(unseen)
        scans = [({str(known): old_meta}, {old_meta['hash']: old_data}, ['Workspace capture is partial.']),
                 ({str(known): old_meta, str(unseen): new_meta}, {new_meta['hash']: new_data}, [])]
        with patch('session_diff.recorder.workspace', side_effect=scans):
            self.call(lambda: 'ok', 'terminal', {'command': 'no changes'})
        self.assertEqual(self.store.view('session-a')['files'], [])

    def test_symbols_at_start_of_line_are_counted(self):
        path = self.work / 'app.txt'
        path.write_text('--before\n')
        self.call(lambda: path.write_text('++after\n'))
        file = self.store.view('session-a')['files'][0]
        diff = self.store.diff('session-a', file['first_id'], file['last_id'])
        self.assertEqual((diff['added'], diff['removed']), (1, 1))

    def test_expiration_removes_unreferenced_contents(self):
        self.call(lambda: (self.work / 'app.txt').write_text('recorded'))
        self.assertEqual(self.blob_count(), 1)
        self.age_everything()
        self.store.prune(1)
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.blob_count(), 0)
        self.assertEqual(self.budget_bytes(), 0)

    def test_unsupported_environment_does_not_capture_local_files(self):
        self.recorder.paths_factory = lambda task: (_ for _ in ()).throw(RuntimeError('Unsupported environment'))
        self.assertEqual(self.call(lambda: 'ok'), 'ok')
        view = self.store.view('session-a')
        self.assertEqual(view['files'], [])
        self.assertIn('Unsupported environment', view['notes'][0])


if __name__ == '__main__':
    unittest.main()
