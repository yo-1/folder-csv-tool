# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic scan pipeline, no NAS or actual file-content I/O. Output is temporary."""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import resource
import tempfile
import time
import sys
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_inventory import Entry, Entries, read_rows
import inventory as inv

parser = argparse.ArgumentParser()
parser.add_argument('--files', type=int, default=842254)
parser.add_argument('--folders', type=int, default=5157)
args = parser.parse_args()
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp) / 'source'
    root.mkdir()
    def listing(path):
        if Path(path) != root:
            return Entries([])
        def records():
            for i in range(args.files):
                yield Entry(root, f'{i:08d}.txt')
            for i in range(args.folders):
                yield Entry(root, f'folder_{i:05d}', mode=0o040755)
        return Entries(records())
    began = time.monotonic()
    with patch.object(inv.os, 'scandir', side_effect=listing):
        result = inv.scan(root, Path(temp) / 'output')
    elapsed = time.monotonic() - began
    # Strict sequence checks catch split-boundary duplicates/omissions without a full set in memory.
    files = folders = 0
    for row in read_rows(result):
        if row[0] == 'ファイル':
            assert row[1] == f'{files:08d}.txt'
            files += 1
        elif row[0] == 'フォルダ':
            assert row[1] == f'folder_{folders:05d}'
            folders += 1
    assert files == args.files and folders == args.folders
    parts = [p['data_rows'] for p in result.manifest['csv_parts']]
    assert sum(parts) == args.files + args.folders
    print(json.dumps({'system': platform.platform(), 'python': platform.python_version(),
                      'files': files, 'folders': folders, 'parts': parts,
                      'elapsed_scan_seconds': round(elapsed, 3),
                      'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      'data_access': 'synthetic_metadata; local temporary output',
                      'gui_or_nas_test': False}, ensure_ascii=False))
