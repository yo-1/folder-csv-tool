# SPDX-License-Identifier: AGPL-3.0-only
"""Headless event tests; does not verify Windows rendering or os.startfile itself."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import folder_csv as ui


class GuiTests(unittest.TestCase):
    def test_completion_buttons_cancel_and_new_run(self):
        widgets, variables, workers = [], [], []
        class Widget:
            def __init__(self, *a, **kw):
                self.kw = kw
                self.destroyed = False
                widgets.append(self)
            def __getattr__(self, name):
                if name == 'after':
                    return lambda delay, fn: setattr(self, 'poll', fn)
                if name == 'destroy':
                    return lambda: setattr(self, 'destroyed', True)
                return lambda *a, **kw: None
            def configure(self, **kw):
                self.kw.update(kw)
        class Variable:
            def __init__(self, value=''):
                self.value = value
                variables.append(self)
            def get(self):
                return self.value
            def set(self, value):
                self.value = value
        class Worker:
            def __init__(self, target, args, **kw):
                self.target, self.args = target, args
                workers.append(self)
            def start(self):
                pass
            def run(self):
                self.target(*self.args)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'input'
            root.mkdir()
            (root / 'x.txt').write_text('a')
            with patch.object(tk, 'Tk', Widget), patch.object(tk, 'StringVar', Variable), \
                    patch.multiple(ttk, Frame=Widget, Entry=Widget, Label=Widget,
                                   Button=Widget, Progressbar=Widget, Combobox=Widget), \
                    patch.object(ui.threading, 'Thread', Worker), \
                    patch.object(messagebox, 'showinfo', Mock()) as info, \
                    patch.object(messagebox, 'showwarning', Mock()), \
                    patch.object(messagebox, 'showerror', Mock()), \
                    patch.object(ui.os, 'startfile', create=True) as opener:
                ui.gui()
                app = widgets[0]
                buttons = {w.kw.get('text'): w for w in widgets if 'command' in w.kw}
                variables[0].set(str(root))
                variables[1].set(str(Path(temp) / 'output'))
                self.assertEqual(buttons['保存フォルダーを開く'].kw['state'], 'disabled')
                buttons['一覧を取得'].kw['command']()
                buttons['終了'].kw['command']()
                self.assertFalse(app.destroyed)
                self.assertTrue(info.called)
                workers[-1].run()
                app.poll()
                self.assertEqual(variables[3].get(), '正常完了')
                self.assertEqual(buttons['保存フォルダーを開く'].kw['state'], 'normal')
                self.assertEqual(buttons['終了概要を開く'].kw['state'], 'normal')
                buttons['終了概要を開く'].kw['command']()
                self.assertTrue(Path(opener.call_args.args[0]).is_file())
                buttons['一覧を取得'].kw['command']()
                self.assertEqual(buttons['保存フォルダーを開く'].kw['state'], 'disabled')
                buttons['中断'].kw['command']()
                workers[-1].run()
                app.poll()
                self.assertEqual(variables[3].get(), '中断')
                self.assertEqual(buttons['一覧を取得'].kw['text'], '新しい一覧を取得')
                buttons['終了'].kw['command']()
                self.assertTrue(app.destroyed)


if __name__ == '__main__':
    unittest.main()
