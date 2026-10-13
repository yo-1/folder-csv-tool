# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Yoichi Wada
"""Folder inventory: Python standard library only; GUI or command line."""
import argparse
import csv
import datetime as dt
import os
from pathlib import Path
import queue
import stat
import threading

HEADERS = ['種別', '名前', '相対パス', 'フルパス', '拡張子', 'サイズ_バイト', '更新日時', '表示分類', '状態']


def clean_path(value):
    return value.strip().strip('"').strip()


def export_inventory(root, output, progress=None):
    root = Path(clean_path(str(root))).absolute()
    output = Path(output).absolute()
    if not root.is_dir():
        raise ValueError('対象フォルダにアクセスできません：' + str(root))
    # Keep all generated output outside the input tree.
    try:
        inside = os.path.commonpath([str(root.resolve()), str(output.resolve())]) == str(root.resolve())
    except ValueError:  # UNC source and local destination can be on different drives.
        inside = False
    if inside:
        raise ValueError('CSVの保存先は対象フォルダの外を選んでください。')
    error_file = output.with_suffix('.errors.csv')
    summary_file = output.with_suffix('.summary.txt')
    counts = dict(files=0, folders=0, links=0, errors=0, rows=0)
    # Exclusive creation prevents overwriting prior results.
    with output.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADERS)
        error_stream = None
        error_writer = None

        def error(path, exc):
            nonlocal error_stream, error_writer
            if error_stream is None:
                error_stream = error_file.open('x', encoding='utf-8-sig', newline='')
                error_writer = csv.writer(error_stream)
                error_writer.writerow(['パス', 'エラー'])
            error_writer.writerow([str(path), str(exc)])
            counts['errors'] += 1

        pending = [root]
        try:
            while pending:
                folder = pending.pop()
                if progress:
                    progress(dict(counts), str(folder))
                try:
                    with os.scandir(folder) as entries:
                        for entry in entries:
                            path = Path(entry.path)
                            relative = str(path.relative_to(root))
                            try:
                                info = entry.stat(follow_symlinks=False)
                                is_link = entry.is_symlink() or bool(
                                    getattr(info, 'st_file_attributes', 0) & 0x400)
                                is_folder = stat.S_ISDIR(info.st_mode)
                                if is_link:
                                    kind, status = 'リンク', 'リンク先の再帰走査は省略'
                                    counts['links'] += 1
                                elif is_folder:
                                    kind, status = 'フォルダ', '取得済み'
                                    counts['folders'] += 1
                                    pending.append(path)
                                else:
                                    kind, status = 'ファイル', '取得済み'
                                    counts['files'] += 1
                                writer.writerow([kind, entry.name, relative, str(path),
                                                 path.suffix if kind == 'ファイル' else '',
                                                 info.st_size if kind == 'ファイル' else '',
                                                 dt.datetime.fromtimestamp(info.st_mtime).isoformat(timespec='seconds'),
                                                 (path.suffix or '拡張子なし') if kind == 'ファイル' else kind,
                                                 status])
                            except OSError as exc:
                                error(path, exc)
                                writer.writerow(['不明', entry.name, relative, str(path), '', '', '', '不明', '取得エラー'])
                            counts['rows'] += 1
                            if progress and counts['rows'] % 500 == 0:
                                progress(dict(counts), str(folder))
                except OSError as exc:
                    error(folder, exc)
        finally:
            if error_stream:
                error_stream.close()
    summary = (f"対象：{root}\nCSV：{output}\n"
               f"ファイル：{counts['files']}\nフォルダ：{counts['folders']}\n"
               f"リンク（再帰省略）：{counts['links']}\n取得エラー：{counts['errors']}\n"
               f"終了日時：{dt.datetime.now().isoformat(timespec='seconds')}\n"
               '走査中の変更は反映が一定ではありません。リンク先は走査していません。\n')
    summary_file.write_text(summary, encoding='utf-8-sig')
    return counts, summary


def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    app = tk.Tk()
    app.title('フォルダ・ファイル一覧CSV作成ツール v1.2')
    app.geometry('840x370')
    app.minsize(640, 370)
    frame = ttk.Frame(app, padding=18)
    frame.pack(fill='both', expand=True)
    frame.columnconfigure(0, weight=1)
    ttk.Label(frame, text='対象フォルダのパス（NASのパスも入力できます）').grid(row=0, column=0, sticky='w')
    source = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=source)
    entry.grid(row=1, column=0, sticky='ew', pady=8)

    def browse():
        value = filedialog.askdirectory(title='一覧を作成するフォルダ')
        if value:
            source.set(value)

    browse_button = ttk.Button(frame, text='参照…', command=browse)
    browse_button.grid(row=1, column=1, padx=(8, 0))
    ttk.Label(frame, text='全階層・隠し項目を含めて取得します。元データは変更しません。').grid(row=2, column=0, columnspan=2, sticky='w')
    status = tk.StringVar(value='パスを入力して「CSV作成」を押してください。')
    status_label = ttk.Label(frame, textvariable=status, wraplength=780)
    status_label.grid(row=4, column=0, columnspan=2, sticky='w', pady=12)
    bar = ttk.Progressbar(frame, mode='indeterminate')
    bar.grid(row=5, column=0, columnspan=2, sticky='ew')
    events = queue.Queue()
    busy = False
    saved_output = None

    def worker(root, output):
        try:
            result = export_inventory(root, output,
                                      lambda count, path: events.put(('progress', (count, path))))
            events.put(('done', (result, output)))
        except Exception as exc:
            events.put(('error', str(exc)))

    def start():
        nonlocal busy, saved_output
        root = clean_path(source.get())
        if not root:
            messagebox.showwarning('対象未入力', '対象フォルダのパスを入力してください。')
            return
        name = root.rstrip('\\/').replace('\\', '/').split('/')[-1]
        output = filedialog.asksaveasfilename(
            title='CSVの保存先（対象フォルダの外を選択）',
            initialfile=f'{name}_全リスト_{dt.datetime.now():%Y%m%d_%H%M%S}.csv',
            defaultextension='.csv', filetypes=[('CSV', '*.csv')])
        if not output:
            return
        # Do not perform NAS access on the UI thread.
        busy = True
        saved_output = None
        open_button.configure(state='disabled')
        for widget in (entry, browse_button, run_button):
            widget.configure(state='disabled')
        status.set('一覧取得中… 大きなNASでは時間がかかります。')
        bar.start(15)
        threading.Thread(target=worker, args=(root, output), daemon=True).start()

    run_button = ttk.Button(frame, text='CSV作成', command=start)
    run_button.grid(row=3, column=0, sticky='w', pady=(16, 0))

    def poll():
        nonlocal busy, saved_output
        latest = None
        while True:
            try:
                event, value = events.get_nowait()
            except queue.Empty:
                break
            if event == 'progress':
                latest = value
                continue
            latest = None
            busy = False
            bar.stop()
            for widget in (entry, browse_button, run_button):
                widget.configure(state='normal')
            if event == 'error':
                status.set('作成に失敗しました。途中のCSVが残る場合があります。')
                messagebox.showerror('作成失敗', value)
            else:
                (counts, summary), saved_output = value
                open_button.configure(state='normal')
                incomplete = counts['errors'] > 0 or counts['links'] > 0
                status.set(('保存完了（取得エラー／省略あり）' if incomplete else '保存完了')
                           + '\n保存先：' + str(Path(saved_output).parent))
                show = messagebox.showwarning if incomplete else messagebox.showinfo
                show('結果', summary + ('\n取得エラーは同名の .errors.csv を確認してください。' if counts['errors'] else ''))
        if latest:
            count, path = latest
            status.set(f"取得中：{count['rows']:,}件／エラー {count['errors']}件\n{path}")
        app.after(150, poll)

    def close():
        if busy:
            messagebox.showinfo('処理中', '処理終了後に閉じてください。')
        else:
            app.destroy()

    def open_output_folder():
        if saved_output is None:
            return
        try:
            os.startfile(str(Path(saved_output).parent))
        except OSError as exc:
            messagebox.showerror('フォルダを開けません', str(exc))

    actions = ttk.Frame(frame)
    actions.grid(row=6, column=0, columnspan=2, sticky='e', pady=(14, 0))
    open_button = ttk.Button(actions, text='保存フォルダを開く',
                             state='disabled', command=open_output_folder)
    open_button.pack(side='left', padx=(0, 8))
    ttk.Button(actions, text='終了', command=close).pack(side='left')

    app.protocol('WM_DELETE_WINDOW', close)
    entry.focus_set()
    poll()
    app.mainloop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='全サブフォルダ・ファイル一覧をUTF-8 BOM付きCSVに保存')
    parser.add_argument('--root')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.root or args.output:
        if not (args.root and args.output):
            parser.error('--root と --output を両方指定してください。')
        counts, summary = export_inventory(args.root, args.output)
        print(summary)
        raise SystemExit(2 if counts['errors'] or counts['links'] else 0)
    gui()
