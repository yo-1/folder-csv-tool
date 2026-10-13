# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Yoichi Wada
"""Streaming inventory and a fail-closed handoff manifest. Standard library only."""
import csv
import datetime as dt
import errno
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass

VERSION = '1.3.0'
SCHEMA_VERSION = '1.0'
HEADERS = ['種別', '名前', '相対パス', 'フルパス', '拡張子', 'サイズ_バイト',
           '更新日時', '表示分類', '状態']
ROW_STATES = {
    '取得済み': '当該項目の情報取得済み。配下の完全性は管理JSONと範囲記録で確認する。',
    '取得エラー': '当該項目の情報取得失敗。サイズ空欄は0ではない。',
    'フォルダ列挙失敗': 'フォルダの情報は取得できたが配下列挙は失敗・不完全。',
    '配下未走査': 'フォルダ列挙を中断。配下の完全性は未確認。',
    'リンク先の再帰走査は省略': 'リンク項目のみ取得。参照先を意図的に未走査。',
}
RUN_LABELS = {'running': '取得中', 'completed': '正常完了', 'partial_failure': '一部取得失敗',
              'interrupted': '中断', 'abnormal': '異常終了', 'not_started': '未開始'}


def now():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def error_reason(exc):
    code = getattr(exc, 'winerror', None)
    if isinstance(exc, PermissionError) or code == 5:
        return 'permission_denied'
    if isinstance(exc, FileNotFoundError) or code in (2, 3):
        return 'not_found'
    if code in (53, 64, 67, 121, 1231, 1232) or getattr(exc, 'errno', None) in (
            errno.ENETDOWN, errno.ENETUNREACH, errno.ECONNRESET, errno.ETIMEDOUT):
        return 'communication_failure'
    if code in (123, 206) or getattr(exc, 'errno', None) in (errno.ENAMETOOLONG, errno.EINVAL):
        return 'invalid_name_or_path'
    return 'other_os_error'


def atomic_json(path, value):
    """Same-directory replace: an interrupted update cannot truncate the prior JSON."""
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@dataclass
class RunResult:
    directory: Path
    manifest_path: Path
    summary_path: Path
    manifest: dict


class OutputSet:
    """Only finalized, flushed CSV parts are eligible for a consumer."""
    def __init__(self, directory, run_id, limit):
        self.directory, self.run_id, self.limit = directory, run_id, limit
        self.parts = []
        self.stream = self.writer = None
        self.processed = 0
        self.confirmed = 0

    def _open(self):
        order = len(self.parts) + 1
        name = f'{self.run_id}_{order:05d}.csv'
        part = {'order': order, 'file': name + '.part', 'data_rows': 0,
                'finalized': False, 'final_file': name}
        self.parts.append(part)
        self.stream = (self.directory / part['file']).open('x', encoding='utf-8-sig', newline='')
        self.writer = csv.writer(self.stream)
        self.writer.writerow(HEADERS)

    def write(self, row):
        if self.stream is not None and self.limit and self.parts[-1]['data_rows'] >= self.limit:
            self.finalize()
        if self.stream is None:
            self._open()
        self.writer.writerow(row)  # Write errors must never be classified as input errors.
        self.parts[-1]['data_rows'] += 1
        self.processed += 1

    def finalize(self):
        if self.stream is None:
            return
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        self.stream = None
        part = self.parts[-1]
        os.rename(self.directory / part['file'], self.directory / part['final_file'])
        part['file'] = part['final_file']
        part['finalized'] = True
        self.confirmed += part['data_rows']

    def finish(self):
        if not self.parts:
            self._open()  # Empty input still produces one header-only CSV.
        self.finalize()

    def abort(self):
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
            self.stream = None


def scan(root, destination, rows_per_csv=100000, cancel=None, progress=None):
    """Returns a result for handled failures; destination failures before run creation raise.

    Cancellation is cooperative at OS-call boundaries. A blocked SMB call is NOT
    interruptible here. Process isolation and restart are deliberately separate work.
    """
    if rows_per_csv not in (0, 10000, 50000, 100000):
        raise ValueError('分割行数は0、10000、50000、100000を指定してください。')
    root_text = str(root).strip().strip('"').strip()
    if not root_text:
        raise ValueError('対象フォルダを指定してください。')
    root = Path(root_text).absolute()
    destination = Path(destination).absolute()
    try:
        inside = os.path.commonpath([str(root.resolve()), str(destination.resolve())]) == str(root.resolve())
    except ValueError:
        inside = False
    if inside:
        raise ValueError('保存先は対象フォルダの外を指定してください。')
    run_id = dt.datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex
    directory = destination / run_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest_path = directory / f'{run_id}.manifest.json'
    summary_path = directory / f'{run_id}.summary.txt'
    started = time.monotonic()
    counts = {'files': 0, 'folders': 0, 'links': 0, 'unknown': 0, 'errors': 0, 'bytes': 0}
    manifest = {
        'schema_version': SCHEMA_VERSION, 'tool_version': VERSION, 'acquisition_id': run_id,
        'root': str(root), 'relative_path_base': str(root), 'root_row_included': False,
        'started_at': now(), 'ended_at': None, 'timezone': str(dt.datetime.now().astimezone().tzinfo),
        'run_status': 'running', 'status_label': RUN_LABELS['running'],
        'conditions': {'hidden': True, 'system': True, 'follow_links': False, 'exclusions': [],
                       'parallelism': 1, 'rows_per_csv': rows_per_csv, 'retry_count': 0,
                       'snapshot_guaranteed': False, 'resume_supported': False},
        'csv_columns': HEADERS, 'row_states': ROW_STATES, 'counts': counts,
        'processed_rows': 0, 'confirmed_rows': 0, 'csv_parts': [],
        'error_csv': f'{run_id}.errors.csv', 'summary_file': summary_path.name,
        'scope_file': f'{run_id}.scopes.jsonl', 'scope_count': 0,
        'unscanned_ranges': {'file': f'{run_id}.scopes.jsonl', 'kinds': ['failed', 'interrupted', 'pending'], 'count': 0},
        'intentional_omissions': {'file': f'{run_id}.scopes.jsonl', 'kinds': ['link_skipped'], 'count': 0},
        'coverage_complete': False, 'artifacts_finalized': False,
        'fatal_error': None,
    }
    output = OutputSet(directory, run_id, rows_per_csv)
    db = error_stream = scope_stream = None
    last_report = 0.0
    last_progress = 0.0
    current = str(root)
    last_response = now()
    final_status = 'abnormal'
    summary_saved = False

    def cancelled():
        return cancel is not None and cancel.is_set()

    def save_manifest():
        manifest['processed_rows'] = output.processed
        manifest['confirmed_rows'] = output.confirmed
        manifest['csv_parts'] = output.parts
        manifest['elapsed_seconds'] = round(time.monotonic() - started, 3)
        manifest['last_response_at'] = last_response
        manifest['current_path'] = current
        atomic_json(manifest_path, manifest)

    def report(force=False):
        nonlocal last_report, last_progress
        elapsed = time.monotonic() - started
        if force or elapsed - last_progress >= 0.5:
            if progress:
                progress({'processed_rows': output.processed, 'confirmed_rows': output.confirmed,
                          'counts': dict(counts), 'elapsed_seconds': elapsed,
                          'current_path': current, 'last_response_at': last_response,
                          'heartbeat_monotonic': time.monotonic()})
            last_progress = elapsed
            if force or elapsed - last_report >= 2:
                save_manifest()
                last_report = elapsed
            elif not progress:
                return

    def scope(path, kind, operation, reason, message='', is_subtree=True):
        value = {'path': str(path), 'relative_path': os.path.relpath(path, root),
                 'kind': kind, 'operation': operation, 'reason': reason,
                 'message': message, 'subtree': is_subtree, 'observed_at': now()}
        scope_stream.write(json.dumps(value, ensure_ascii=False) + '\n')
        manifest['scope_count'] += 1
        if kind == 'link_skipped':
            manifest['intentional_omissions']['count'] += 1
        else:
            manifest['unscanned_ranges']['count'] += 1

    def input_error(path, operation, exc, subtree=True):
        reason = error_reason(exc)
        errors.writerow([run_id, str(path), operation, reason,
                         getattr(exc, 'errno', ''), getattr(exc, 'winerror', ''), str(exc)])
        counts['errors'] += 1
        scope(path, 'failed', operation, reason, str(exc), subtree)

    def emit(row):
        output.write(row)
        key = {'ファイル': 'files', 'フォルダ': 'folders', 'リンク': 'links', '不明': 'unknown'}[row[0]]
        counts[key] += 1
        if key == 'files':
            counts['bytes'] += row[5]

    try:
        save_manifest()
        error_stream = (directory / manifest['error_csv']).open('x', encoding='utf-8-sig', newline='')
        errors = csv.writer(error_stream)
        errors.writerow(['取得ID', 'パス', '操作', '理由', 'errno', 'winerror', 'エラー'])
        scope_stream = (directory / manifest['scope_file']).open('x', encoding='utf-8', newline='\n')
        db = sqlite3.connect(directory / f'{run_id}.work.sqlite')
        db.execute('CREATE TABLE pending (id INTEGER PRIMARY KEY, path TEXT, row_json TEXT)')
        try:
            root_info = root.stat()
            if not stat.S_ISDIR(root_info.st_mode):
                raise NotADirectoryError(str(root))
        except OSError as exc:
            input_error(root, 'validate_root', exc)
            final_status = 'not_started'
        else:
            db.execute('INSERT INTO pending(path,row_json) VALUES (?,NULL)', (str(root),))
            db.commit()
            final_status = 'completed'
            while True:
                job = db.execute('SELECT id,path,row_json FROM pending ORDER BY id LIMIT 1').fetchone()
                if not job:
                    break
                if cancelled():
                    final_status = 'interrupted'
                    break
                job_id, folder_name, row_json = job
                folder = Path(folder_name)
                current = folder_name
                folder_row = json.loads(row_json) if row_json else None
                folder_state = '取得済み'
                report()
                entries = None
                try:
                    entries = os.scandir(folder)
                except OSError as exc:
                    input_error(folder, 'list_directory', exc)
                    folder_state = 'フォルダ列挙失敗'
                if entries is not None:
                    try:
                        while True:
                            if cancelled():
                                folder_state = '配下未走査'
                                final_status = 'interrupted'
                                scope(folder, 'interrupted', 'list_directory', 'user_cancelled')
                                break
                            try:
                                entry = next(entries)
                                last_response = now()
                            except StopIteration:
                                break
                            except OSError as exc:
                                input_error(folder, 'list_directory', exc)
                                folder_state = 'フォルダ列挙失敗'
                                break
                            path = Path(entry.path)
                            current = str(path)
                            relative = str(path.relative_to(root))
                            try:
                                info = entry.stat(follow_symlinks=False)
                                last_response = now()
                                is_link = entry.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
                                is_folder = stat.S_ISDIR(info.st_mode)
                                kind = 'リンク' if is_link else ('フォルダ' if is_folder else 'ファイル')
                                stamp = dt.datetime.fromtimestamp(info.st_mtime).isoformat(timespec='seconds')
                                ext = path.suffix if kind == 'ファイル' else ''
                                row = [kind, entry.name, relative, str(path), ext,
                                       info.st_size if kind == 'ファイル' else '', stamp,
                                       (ext or '拡張子なし') if kind == 'ファイル' else kind,
                                       'リンク先の再帰走査は省略' if is_link else '取得済み']
                            except (OSError, ValueError, OverflowError) as exc:
                                input_error(path, 'get_information', exc)
                                emit(['不明', entry.name, relative, str(path), '', '', '', '不明', '取得エラー'])
                            else:
                                if kind == 'フォルダ':
                                    db.execute('INSERT INTO pending(path,row_json) VALUES (?,?)',
                                               (str(path), json.dumps(row, ensure_ascii=False)))
                                else:
                                    emit(row)
                                    if is_link:
                                        scope(path, 'link_skipped', 'follow_link', 'link_policy')
                            report()
                    finally:
                        entries.close()
                if folder_row:
                    folder_row[-1] = folder_state
                    emit(folder_row)
                db.execute('DELETE FROM pending WHERE id=?', (job_id,))
                db.commit()
                if final_status == 'interrupted':
                    break
            if final_status == 'completed' and counts['errors']:
                final_status = 'partial_failure'
        # Known pending folders are recorded individually without retaining them in RAM.
        if db:
            for folder_name, in db.execute('SELECT path FROM pending ORDER BY id'):
                scope(Path(folder_name), 'pending', 'list_directory', 'not_processed')
        output.finish()
        for stream in (error_stream, scope_stream):
            stream.flush()
            os.fsync(stream.fileno())
        manifest['artifacts_finalized'] = True
    except (Exception, KeyboardInterrupt) as exc:
        final_status = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'abnormal'
        manifest['fatal_error'] = {'type': type(exc).__name__, 'message': str(exc), 'current_path': current}
        output.abort()
        # Best effort only: if the output volume is unavailable the last manifest
        # remains running/unfinalized. Consumers must reject that prior snapshot.
        if scope_stream and not scope_stream.closed:
            try:
                scope(Path(current), 'interrupted' if final_status == 'interrupted' else 'failed',
                      'run', 'fatal_or_interrupt', str(exc))
                if db:
                    for folder_name, in db.execute('SELECT path FROM pending ORDER BY id'):
                        scope(Path(folder_name), 'pending', 'list_directory', 'not_processed')
            except (OSError, sqlite3.Error):
                pass
    finally:
        for stream in (error_stream, scope_stream):
            if stream:
                try:
                    stream.close()
                except OSError as exc:
                    final_status = 'abnormal'
                    manifest['artifacts_finalized'] = False
                    manifest['fatal_error'] = {'type': type(exc).__name__, 'message': str(exc)}
        if db:
            db.close()
        manifest['ended_at'] = now()
        manifest['run_status'] = final_status
        manifest['status_label'] = RUN_LABELS[final_status]
        manifest['coverage_complete'] = (final_status == 'completed'
                                         and manifest['intentional_omissions']['count'] == 0)
        manifest['processed_rows'] = output.processed
        manifest['confirmed_rows'] = output.confirmed
        manifest['csv_parts'] = output.parts
        manifest['elapsed_seconds'] = round(time.monotonic() - started, 3)
        capacity_label = '取得済み分の合計容量' if final_status != 'completed' else '取得ファイルの合計容量'
        summary = (f"終了状態：{manifest['status_label']}\n取得ID：{run_id}\n対象：{root}\n"
                   f"開始：{manifest['started_at']}\n終了：{manifest['ended_at']}\n"
                   f"経過：{manifest['elapsed_seconds']} 秒\n"
                   f"ファイル：{counts['files']:,}\nフォルダ：{counts['folders']:,}\n"
                   f"リンク：{counts['links']:,}\n不明：{counts['unknown']:,}\n取得エラー：{counts['errors']:,}\n"
                   f"処理済みデータ行：{output.processed:,}\n確定CSVデータ行：{output.confirmed:,}\n"
                   f"{capacity_label}：{counts['bytes']:,} バイト\n"
                   f"未確認・未走査範囲：{manifest['unscanned_ranges']['count']:,}\n"
                   f"リンク先省略：{manifest['intentional_omissions']['count']:,}\n"
                   f"保存先：{directory}\n管理JSON：{manifest_path.name}\n"
                   + ''.join(f"CSV：{p['file']}（{p['data_rows']:,}行／確定={p['finalized']}）\n" for p in output.parts)
                   + '通常走査は同一時点のスナップショットではありません。再開機能は未実装です。\n')
        try:
            summary_path.write_text(summary, encoding='utf-8-sig')
            summary_saved = True
            save_manifest()
        except OSError as exc:
            manifest['run_status'] = 'abnormal'
            manifest['status_label'] = RUN_LABELS['abnormal']
            manifest['coverage_complete'] = False
            manifest['artifacts_finalized'] = False
            manifest['fatal_error'] = {'type': type(exc).__name__, 'message': str(exc)}
            # Try once to record the failed final save, never report completion.
            try:
                if summary_saved:
                    summary_path.write_text(summary.replace('終了状態：' + RUN_LABELS[final_status],
                                                            '終了状態：異常終了（最終保存失敗）', 1), encoding='utf-8-sig')
                save_manifest()
            except OSError:
                pass
    return RunResult(directory, manifest_path, summary_path, manifest)
