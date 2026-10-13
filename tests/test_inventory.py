# SPDX-License-Identifier: AGPL-3.0-only
import csv
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import inventory as inv


def read_rows(result):
    for part in result.manifest['csv_parts']:
        if not part['finalized']:
            continue
        with (result.directory / part['file']).open(encoding='utf-8-sig', newline='') as f:
            reader = csv.reader(f)
            assert next(reader) == inv.HEADERS
            yield from reader


class Entries:
    def __init__(self, entries):
        self.entries = iter(entries)
    def __iter__(self):
        return self
    def __next__(self):
        return next(self.entries)
    def close(self):
        pass


class Entry:
    def __init__(self, root, name, mode=0o100644, size=3, attributes=0):
        self.name, self.path = name, str(root / name)
        self.mode, self.size, self.attributes = mode, size, attributes
    def stat(self, **kwargs):
        return types.SimpleNamespace(st_mode=self.mode, st_size=self.size,
                                     st_mtime=1700000000, st_file_attributes=self.attributes)
    def is_symlink(self):
        return False


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / '日本語入力'
        self.root.mkdir()
        self.dest = self.base / '保存'

    def assert_consistent(self, r):
        m = json.loads(r.manifest_path.read_text())
        self.assertEqual(m['confirmed_rows'], sum(p['data_rows'] for p in m['csv_parts'] if p['finalized']))
        self.assertEqual(m['processed_rows'], sum(m['counts'][k] for k in ['files', 'folders', 'links', 'unknown']))
        for part in m['csv_parts']:
            if part['finalized']:
                self.assertTrue((r.directory / part['file']).is_file())
                self.assertTrue((r.directory / part['file']).read_bytes().startswith(b'\xef\xbb\xbf'))
        return m

    def test_files_empty_folder_bom_and_columns(self):
        (self.root / '空').mkdir()
        (self.root / 'noext').write_bytes(b'')
        (self.root / '.hidden').write_bytes(b'xyz')
        (self.root / '引用,符.txt').write_bytes(b'abc')
        r = inv.scan(self.root, self.dest)
        m = self.assert_consistent(r)
        self.assertEqual(m['run_status'], 'completed')
        self.assertEqual(m['counts']['bytes'], 6)
        rows = {row[1]: row for row in read_rows(r)}
        self.assertEqual(rows['noext'][5], '0')
        self.assertEqual(rows['noext'][7], '拡張子なし')
        self.assertEqual(rows['空'][5], '')
        self.assertEqual(rows['空'][7], 'フォルダ')
        self.assertIn('引用,符.txt', rows)

    def test_csv_quotes_newlines_and_comma_roundtrip(self):
        special = '引用,符"と改行\n.txt'
        with patch.object(inv.os, 'scandir', side_effect=lambda p: Entries([Entry(self.root, special)])):
            r = inv.scan(self.root, self.dest)
        self.assertEqual(list(read_rows(r))[0][1], special)

    def test_different_drive_destination(self):
        with patch.object(inv.os.path, 'commonpath', side_effect=ValueError('different drives')):
            r = inv.scan(self.root, self.dest)
        self.assertEqual(r.manifest['run_status'], 'completed')

    def test_link_junction_and_hidden_system_attribute(self):
        entries = [Entry(self.root, 'junction', mode=0o040755, attributes=0x400),
                   Entry(self.root, 'hidden.db', attributes=6)]
        with patch.object(inv.os, 'scandir', side_effect=lambda p: Entries(entries)) as scan:
            r = inv.scan(self.root, self.dest)
        self.assertEqual(scan.call_count, 1)
        m = self.assert_consistent(r)
        self.assertEqual(m['counts']['links'], 1)
        self.assertEqual(m['counts']['files'], 1)
        self.assertFalse(m['coverage_complete'])
        self.assertEqual(m['intentional_omissions']['count'], 1)

    def test_directory_denied_and_entry_missing(self):
        (self.root / 'denied').mkdir()
        original = os.scandir
        class Missing(Entry):
            def stat(self, **kwargs):
                raise FileNotFoundError('disappeared')
        def listing(path):
            if Path(path).name == 'denied':
                raise PermissionError('permission denied')
            return Entries([Entry(self.root, 'denied', mode=0o040755), Missing(self.root, 'gone')])
        with patch.object(inv.os, 'scandir', side_effect=listing):
            r = inv.scan(self.root, self.dest)
        m = self.assert_consistent(r)
        self.assertEqual(m['run_status'], 'partial_failure')
        self.assertEqual(m['counts']['errors'], 2)
        rows = {x[1]: x for x in read_rows(r)}
        self.assertEqual(rows['denied'][-1], 'フォルダ列挙失敗')
        self.assertEqual(rows['gone'][5], '')
        scopes = [json.loads(x) for x in (r.directory / m['scope_file']).read_text().splitlines()]
        self.assertEqual({s['reason'] for s in scopes}, {'permission_denied', 'not_found'})

    def test_cancellation_marks_current_and_pending(self):
        event = threading.Event()
        def items():
            yield Entry(self.root, 'pending', mode=0o040755)
            event.set()
            yield Entry(self.root, 'one.txt')
            yield Entry(self.root, 'not_reached.txt')
        with patch.object(inv.os, 'scandir', side_effect=lambda p: Entries(items())):
            r = inv.scan(self.root, self.dest, cancel=event)
        m = self.assert_consistent(r)
        self.assertEqual(m['run_status'], 'interrupted')
        self.assertTrue(m['artifacts_finalized'])
        self.assertEqual(m['confirmed_rows'], 1)
        scopes = [json.loads(x) for x in (r.directory / m['scope_file']).read_text().splitlines()]
        self.assertEqual({s['kind'] for s in scopes}, {'interrupted', 'pending'})

    def test_empty_input_and_missing_root(self):
        empty = inv.scan(self.root, self.dest)
        self.assertEqual(list(read_rows(empty)), [])
        self.assertEqual(len(empty.manifest['csv_parts']), 1)
        r = inv.scan(self.root / 'not_found', self.dest)
        self.assertEqual(r.manifest['run_status'], 'not_started')
        self.assertFalse(r.manifest['coverage_complete'])

    def test_unique_run_ids_and_destination_guard(self):
        a = inv.scan(self.root, self.dest)
        b = inv.scan(self.root, self.dest)
        self.assertNotEqual(a.directory, b.directory)
        with self.assertRaises(ValueError):
            inv.scan(self.root, self.root / 'output')
        with self.assertRaises(ValueError):
            inv.scan('', self.dest)

    def test_write_failure_keeps_unfinalized_part(self):
        (self.root / 'x.txt').write_bytes(b'abc')
        with patch.object(inv.OutputSet, 'finalize', side_effect=OSError(errno.ENOSPC, 'disk full')):
            r = inv.scan(self.root, self.dest)
        m = self.assert_consistent(r)
        self.assertEqual(m['run_status'], 'abnormal')
        self.assertFalse(m['artifacts_finalized'])
        self.assertEqual(m['confirmed_rows'], 0)
        self.assertEqual(m['counts']['errors'], 0)  # NOT an input data error.
        self.assertFalse(m['csv_parts'][0]['finalized'])
        self.assertTrue((r.directory / m['csv_parts'][0]['file']).exists())

    def test_summary_failure_never_reports_success(self):
        original = Path.write_text
        def write(path, *a, **kw):
            if str(path).endswith('.summary.txt'):
                raise PermissionError('summary denied')
            return original(path, *a, **kw)
        with patch.object(Path, 'write_text', write):
            r = inv.scan(self.root, self.dest)
        self.assertEqual(r.manifest['run_status'], 'abnormal')
        self.assertEqual(json.loads(r.manifest_path.read_text())['run_status'], 'abnormal')

    def test_atomic_manifest_failure_keeps_previous_json(self):
        path = self.base / 'm.json'
        inv.atomic_json(path, {'state': 'running'})
        with patch.object(inv.os, 'replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                inv.atomic_json(path, {'state': 'completed'})
        self.assertEqual(json.loads(path.read_text()), {'state': 'running'})

    def test_split_100001_and_all_limits_same_records(self):
        signatures = []
        for limit in (0, 10000, 50000, 100000):
            with patch.object(inv.os, 'scandir', side_effect=lambda p: Entries(
                    Entry(self.root, f'{i:06d}.txt') for i in range(100001))):
                r = inv.scan(self.root, self.dest, limit)
            self.assert_consistent(r)
            import hashlib
            digest = hashlib.sha256()
            total = 0
            for row in read_rows(r):
                total += 1
                digest.update(json.dumps(row, ensure_ascii=False).encode())
            self.assertEqual(total, 100001)
            signatures.append(digest.hexdigest())
            if limit == 100000:
                self.assertEqual([p['data_rows'] for p in r.manifest['csv_parts']], [100000, 1])
        self.assertEqual(len(set(signatures)), 1)


if __name__ == '__main__':
    unittest.main()
