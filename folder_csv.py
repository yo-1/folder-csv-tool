# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Yoichi Wada
"""GUI/CLI entry point for Folder CSV Tool."""
import argparse
import os
from pathlib import Path
import queue
import threading
import time

from inventory import HEADERS, VERSION, scan


def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    app = tk.Tk()
    app.title('フォルダ・ファイル一覧CSV作成ツール v' + VERSION)
    app.geometry('900x640')
    app.minsize(740, 570)
    frame = ttk.Frame(app, padding=18)
    frame.pack(fill='both', expand=True)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(8, weight=1)
    source = tk.StringVar()
    destination = tk.StringVar(value=str(Path.home() / 'Documents' / 'Folder_CSV_Output'))
    split = tk.StringVar(value='10万行')
    state = tk.StringVar(value='対象フォルダと保存先を指定してください。')
    progress_text = tk.StringVar()
    results = tk.StringVar()
    events = queue.Queue(maxsize=2)
    cancel = threading.Event()
    busy = False
    last_result = None
    last_progress = None
    began = 0.0

    def select(variable):
        value = filedialog.askdirectory()
        if value:
            variable.set(value)

    ttk.Label(frame, text='対象フォルダ（ローカル／NASのUNCパス）').grid(row=0, column=0, sticky='w')
    source_entry = ttk.Entry(frame, textvariable=source)
    source_entry.grid(row=1, column=0, sticky='ew', pady=5)
    source_browse = ttk.Button(frame, text='参照…', command=lambda: select(source))
    source_browse.grid(row=1, column=1, padx=8)
    ttk.Label(frame, text='保存先（PCローカル側を推奨。取得IDごとにサブフォルダを作成）').grid(row=2, column=0, sticky='w', pady=(10, 0))
    destination_entry = ttk.Entry(frame, textvariable=destination)
    destination_entry.grid(row=3, column=0, sticky='ew', pady=5)
    destination_browse = ttk.Button(frame, text='参照…', command=lambda: select(destination))
    destination_browse.grid(row=3, column=1, padx=8)
    options = ttk.Frame(frame)
    options.grid(row=4, column=0, columnspan=2, sticky='w', pady=10)
    ttk.Label(options, text='1CSVのデータ行数：').pack(side='left')
    selector = ttk.Combobox(options, textvariable=split, values=['1万行', '5万行', '10万行', '分割なし'],
                            state='readonly', width=12)
    selector.pack(side='left')
    ttk.Label(frame, textvariable=state, wraplength=840).grid(row=5, column=0, columnspan=2, sticky='w', pady=8)
    ttk.Label(frame, textvariable=progress_text, wraplength=840).grid(row=6, column=0, columnspan=2, sticky='w')
    bar = ttk.Progressbar(frame, mode='indeterminate')
    bar.grid(row=7, column=0, columnspan=2, sticky='ew', pady=8)
    ttk.Label(frame, textvariable=results, wraplength=840, justify='left').grid(row=8, column=0, columnspan=2, sticky='nw')
    actions = ttk.Frame(frame)
    actions.grid(row=9, column=0, columnspan=2, sticky='w', pady=12)
    inputs = [source_entry, source_browse, destination_entry, destination_browse]

    def send(event, value):
        # Retain at most the latest two notifications. No unbounded UI log/queue.
        try:
            events.put_nowait((event, value))
        except queue.Full:
            try:
                events.get_nowait()
            except queue.Empty:
                pass
            events.put_nowait((event, value))

    def worker(root, target, limit):
        try:
            result = scan(root, target, limit, cancel=cancel,
                          progress=lambda value: send('progress', value))
            send('done', result)
        except Exception as exc:
            send('error', str(exc))

    def start():
        nonlocal busy, last_result, last_progress, began
        root = source.get().strip().strip('"').strip()
        target = destination.get().strip().strip('"').strip()
        if not root or not target:
            messagebox.showwarning('入力不足', '対象フォルダと保存先を指定してください。')
            return
        busy = True
        last_result = last_progress = None
        began = time.monotonic()
        cancel.clear()
        for widget in inputs:
            widget.configure(state='disabled')
        selector.configure(state='disabled')
        start_button.configure(state='disabled')
        cancel_button.configure(state='normal')
        folder_button.configure(state='disabled')
        summary_button.configure(state='disabled')
        results.set('')
        state.set('取得中。全件数は事前に数えず、取得件数を表示します。')
        bar.start(15)
        limit = {'1万行': 10000, '5万行': 50000, '10万行': 100000, '分割なし': 0}[split.get()]
        threading.Thread(target=worker, args=(root, target, limit), daemon=True).start()

    def stop():
        cancel.set()
        cancel_button.configure(state='disabled')
        state.set('中断要求済み。現在のOSアクセスが戻り次第、取得済み結果を保存します。')

    def open_path(summary=False):
        if last_result is None:
            return
        path = last_result.summary_path if summary else last_result.directory
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror('開けません', str(exc))

    def close():
        if busy:
            messagebox.showinfo('取得中', '「中断」を押し、結果保存が終わってから終了してください。\n'
                                'NASのOS通信待ちは、今回の版では即時停止できません。')
        else:
            app.destroy()

    start_button = ttk.Button(actions, text='一覧を取得', command=start)
    start_button.pack(side='left', padx=(0, 8))
    cancel_button = ttk.Button(actions, text='中断', command=stop, state='disabled')
    cancel_button.pack(side='left', padx=(0, 8))
    folder_button = ttk.Button(actions, text='保存フォルダーを開く', command=open_path, state='disabled')
    folder_button.pack(side='left', padx=(0, 8))
    summary_button = ttk.Button(actions, text='終了概要を開く', command=lambda: open_path(True), state='disabled')
    summary_button.pack(side='left', padx=(0, 8))
    ttk.Button(actions, text='終了', command=close).pack(side='left')

    def poll():
        nonlocal busy, last_result, last_progress
        for _ in range(2):
            try:
                event, value = events.get_nowait()
            except queue.Empty:
                break
            if event == 'progress':
                last_progress = value
                continue
            busy = False
            bar.stop()
            cancel_button.configure(state='disabled')
            for widget in inputs:
                widget.configure(state='normal')
            selector.configure(state='readonly')
            start_button.configure(state='normal', text='新しい一覧を取得')
            if event == 'error':
                state.set('未開始／保存先準備失敗。完了していません。')
                results.set(value)
                continue
            last_result = value
            m = value.manifest
            c = m['counts']
            state.set(m['status_label'] + ('（リンク先省略あり）' if c['links'] else ''))
            results.set(f"開始：{m['started_at']}\n終了：{m['ended_at']}\n"
                        f"経過：{m['elapsed_seconds']:.1f}秒\n"
                        f"ファイル {c['files']:,}／フォルダ {c['folders']:,}／リンク {c['links']:,}／不明 {c['unknown']:,}\n"
                        f"取得エラー {c['errors']:,}／取得済み分の合計容量 {c['bytes']:,}バイト\n"
                        f"処理済み {m['processed_rows']:,}行／確定CSV {m['confirmed_rows']:,}行\n"
                        f"保存先：{value.directory}\n管理JSON：{value.manifest_path.name}\n"
                        f"出力CSV：{len(m['csv_parts'])}本（一覧は終了概要・管理JSON）"
                        + ('\n保存失敗：' + str(m['fatal_error']) if m['fatal_error'] else ''))
            folder_button.configure(state='normal')
            summary_button.configure(state='normal' if value.summary_path.is_file() else 'disabled')
        if busy:
            elapsed = time.monotonic() - began
            heartbeat = last_progress['heartbeat_monotonic'] if last_progress else began
            waiting = time.monotonic() - heartbeat
            row = last_progress['processed_rows'] if last_progress else 0
            confirmed = last_progress['confirmed_rows'] if last_progress else 0
            path = last_progress['current_path'] if last_progress else source.get()
            stamp = last_progress['last_response_at'] if last_progress else '未受信'
            progress_text.set(f"処理済み {row:,}行／確定CSV {confirmed:,}行／経過 {elapsed:.1f}秒\n"
                              f"最終応答：{stamp}\n対象：{path}"
                              + (f'\n応答待ち／進捗通知なし {waiting:.0f}秒（通信停止とは未確定）' if waiting >= 15 else ''))
        app.after(200, poll)

    app.protocol('WM_DELETE_WINDOW', close)
    source_entry.focus_set()
    poll()
    app.mainloop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='全階層一覧を分割CSVと管理JSONに保存')
    parser.add_argument('--root')
    parser.add_argument('--output-dir')
    parser.add_argument('--rows-per-csv', type=int, choices=[0, 10000, 50000, 100000], default=100000)
    args = parser.parse_args()
    if args.root or args.output_dir:
        if not args.root or not args.output_dir:
            parser.error('--root と --output-dir を両方指定してください。')
        result = scan(args.root, args.output_dir, args.rows_per_csv)
        print(result.manifest['status_label'])
        print(result.manifest_path)
        raise SystemExit({'completed': 0, 'partial_failure': 2, 'interrupted': 130,
                          'not_started': 2, 'abnormal': 1}.get(result.manifest['run_status'], 1))
    gui()
