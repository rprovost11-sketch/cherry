"""CherryApp: main window.  Two-pane PanedWindow (editor top, REPL bottom)
plus an interpreter selector bar at the top and a CWD status button at the
very bottom."""

import os
import pathlib
import sys
import tkinter as tk
from tkinter import filedialog, font as tkfont

_CHERRY_DIR = pathlib.Path.home() / '.cherry'

def _state_path(key):
    return _CHERRY_DIR / ('interp' + key + '.json')

_LISP_DIR = pathlib.Path(__file__).resolve().parents[1]

_R7RS_COMPLIANCE_DIR = str(_LISP_DIR / 'scheme-tests' / 'R7RS-Compliance-Tests')

_INTERPRETERS = {
    '1': {
        'label':        '1 · PythonsLisp',
        'cmd':          [sys.executable, '-u', '-m', 'pythonslisp'],
        'cwd':          str(_LISP_DIR / '1PythonsLisp'),
        'testdir':      None,
        'compliancedir': None,
    },
    '2': {
        'label':        '2 · CPPScheme',
        'cmd':          [str(_LISP_DIR / '2CPPScheme' / 'build' / 'Release' / 'scheme.exe')],
        'cwd':          str(_LISP_DIR / '2CPPScheme'),
        'testdir':      None,
        'compliancedir': None,
    },
    '3': {
        'label':        '3 · PyScheme',
        'cmd':          [sys.executable, '-u', '-m', 'pyscheme'],
        'cwd':          str(_LISP_DIR / '3PyScheme'),
        'testdir':      None,   # PyScheme derives testdir internally from scheme-tests/
        'compliancedir': _R7RS_COMPLIANCE_DIR,
    },
    '4': {
        'label':        '4 · CPPScheme2',
        'cmd':          [str(_LISP_DIR / '4CPPScheme2' / 'build' / 'Release' / 'cppscheme2.exe')],
        'cwd':          str(_LISP_DIR / '4CPPScheme2'),
        'testdir':      None,
        'compliancedir': None,
    },
}

_DEFAULT_INTERP = '3'

from cherry.subprocess_bridge import SubprocessBridge
from cherry.editor_pane       import EditorPane
from cherry.repl_pane         import ReplPane


class CherryApp(tk.Tk):
   def __init__(self):
      super().__init__()
      self._current_interp = _DEFAULT_INTERP
      cfg = _INTERPRETERS[_DEFAULT_INTERP]
      self.title('cherry - ' + cfg['label'])
      self.geometry('900x700')
      self.configure(bg='#1e1e1e')

      _CHERRY_DIR.mkdir(exist_ok=True)
      self._bridge = SubprocessBridge(cmd=cfg['cmd'], cwd=cfg['cwd'])
      self._build()
      self._editor.restore_state(_state_path(self._current_interp))
      self.protocol('WM_DELETE_WINDOW', self._on_close)

   def _build(self):
      # ---- interpreter selector bar ----
      hdr = tk.Frame(self, bg='#252526', pady=4)
      hdr.pack(side=tk.TOP, fill=tk.X)

      tk.Label(
         hdr, text='Interpreter:',
         bg='#252526', fg='#888888',
         font=tkfont.Font(family='Courier New', size=9),
         padx=8,
      ).pack(side=tk.LEFT)

      labels = [cfg['label'] for cfg in _INTERPRETERS.values()]
      self._interp_var = tk.StringVar(value=_INTERPRETERS[_DEFAULT_INTERP]['label'])
      om = tk.OptionMenu(hdr, self._interp_var, *labels,
                         command=self._on_interp_change)
      om.configure(
         bg='#3c3c3c', fg='#d4d4d4',
         activebackground='#505050', activeforeground='#ffffff',
         highlightthickness=0, relief=tk.FLAT,
         font=tkfont.Font(family='Courier New', size=9),
         padx=8, pady=2,
      )
      om['menu'].configure(bg='#3c3c3c', fg='#d4d4d4',
                           activebackground='#505050', activeforeground='#ffffff',
                           font=tkfont.Font(family='Courier New', size=9))
      om.pack(side=tk.LEFT)

      tk.Frame(hdr, width=1, bg='#555555').pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=4)

      self._cwd_var = tk.StringVar(value=os.getcwd())
      tk.Button(
         hdr,
         text='CWD...',
         command=self._cmd_chdir,
         bg='#3c3c3c', fg='#d4d4d4',
         activebackground='#505050', activeforeground='#ffffff',
         relief=tk.FLAT,
         font=tkfont.Font(family='Courier New', size=9),
         padx=8, pady=2, cursor='hand2',
      ).pack(side=tk.LEFT)

      self._cwd_label = tk.Label(
         hdr,
         textvariable=self._cwd_var,
         bg='#252526', fg='#888888',
         font=tkfont.Font(family='Courier New', size=9),
         anchor=tk.W,
         padx=4,
      )
      self._cwd_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

      # ---- main paned area ----
      paned = tk.PanedWindow(
         self,
         orient=tk.VERTICAL,
         sashwidth=5,
         sashrelief=tk.FLAT,
         bg='#3c3c3c',
      )
      paned.pack(fill=tk.BOTH, expand=True)

      self._editor = EditorPane(paned, on_run=self._on_run, bg='#1e1e1e')
      self._repl   = ReplPane(paned, bridge=self._bridge,
                              get_cwd=lambda: self._cwd_var.get(),
                              get_testdir=lambda: _INTERPRETERS[self._current_interp].get('testdir'),
                              get_compliancedir=lambda: _INTERPRETERS[self._current_interp].get('compliancedir'),
                              bg='#1e1e1e')

      paned.add(self._editor, stretch='always')
      paned.add(self._repl,   stretch='always')

      self.after(100, lambda: paned.sash_place(0, 0, self.winfo_height() // 2))
      self._paned = paned

   def _on_interp_change(self, selected_label):
      new_key = next(
         (k for k, v in _INTERPRETERS.items() if v['label'] == selected_label),
         None,
      )
      if new_key is None or new_key == self._current_interp:
         return
      self._switch_interpreter(new_key)

   def _switch_interpreter(self, key):
      cfg = _INTERPRETERS[key]

      parent = self
      dlg = tk.Toplevel(parent)
      dlg.title('Switch interpreter')
      dlg.resizable(False, False)
      dlg.configure(bg='#2d2d2d')
      dlg.transient(parent)

      tk.Label(
         dlg,
         text=('Switch to ' + cfg['label'] + '?\n'
               'The current session will be lost.'),
         bg='#2d2d2d', fg='#d4d4d4',
         padx=24, pady=16, justify=tk.LEFT,
      ).pack()

      btn_row = tk.Frame(dlg, bg='#2d2d2d')
      btn_row.pack(pady=(0, 14))
      confirmed = [False]

      def _do_switch():
         confirmed[0] = True
         dlg.destroy()

      tk.Button(btn_row, text='Switch', command=_do_switch,
                bg='#1f4e6b', fg='#d4d4d4',
                activebackground='#2f6e8b', activeforeground='#ffffff',
                relief=tk.FLAT, padx=14, pady=4, cursor='hand2',
                ).pack(side=tk.LEFT, padx=6)
      tk.Button(btn_row, text='Cancel', command=dlg.destroy,
                bg='#3c3c3c', fg='#d4d4d4',
                activebackground='#505050', activeforeground='#ffffff',
                relief=tk.FLAT, padx=14, pady=4, cursor='hand2',
                ).pack(side=tk.LEFT, padx=6)

      dlg.update_idletasks()
      w = dlg.winfo_width()
      h = dlg.winfo_height()
      x = parent.winfo_x() + (parent.winfo_width()  - w) // 2
      y = parent.winfo_y() + (parent.winfo_height() - h) // 2
      dlg.geometry('+' + str(x) + '+' + str(y))
      dlg.grab_set()
      dlg.wait_window()

      if not confirmed[0]:
         self._interp_var.set(_INTERPRETERS[self._current_interp]['label'])
         return

      self._editor.save_state(_state_path(self._current_interp))
      self._bridge.shutdown()
      new_bridge = SubprocessBridge(cmd=cfg['cmd'], cwd=cfg['cwd'])
      self._bridge = new_bridge
      self._current_interp = key
      self._cwd_var.set(cfg['cwd'])
      self._repl.set_bridge(new_bridge)
      self._editor.restore_state(_state_path(key))
      self.title('cherry - ' + cfg['label'])

   def _cmd_chdir(self):
      path = filedialog.askdirectory(title='Change working directory',
                                     initialdir=os.getcwd())
      if path:
         os.chdir(path)
         self._cwd_var.set(os.getcwd())
         self._bridge.chdir(path)

   def _on_run(self, source):
      """Relay editor Run / Help button to the REPL."""
      self._repl.inject_source(source)

   def _on_close(self):
      self._editor.save_state(_state_path(self._current_interp))
      self._repl.save_history()
      self._bridge.shutdown()
      self.destroy()


def main():
   app = CherryApp()
   app.mainloop()
