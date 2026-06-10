"""EditorPane: a simple text editor with a toolbar (New/Open/Save/Run)."""

import json
import os
import pathlib
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox

from cherry.parens import make_code_map, find_match

TAG_PAREN = 'paren_match'


class EditorPane(tk.Frame):
   def __init__(self, parent, on_run, **kwargs):
      super().__init__(parent, **kwargs)
      self._on_run      = on_run      # callable(source_str)
      self._filepath    = None        # currently open file path
      self._modified    = False
      self._state_path  = None        # set by restore_state / save_state
      self._autosave_id = None        # pending after() id

      self._build()

   # ---- construction -----------------------------------------------------

   def _build(self):
      self._build_toolbar()
      self._build_editor()

   def _build_toolbar(self):
      bar = tk.Frame(self, bg='#2d2d2d', pady=3)
      bar.pack(side=tk.TOP, fill=tk.X)

      btn_cfg = dict(
         bg='#3c3c3c', fg='#d4d4d4',
         activebackground='#505050', activeforeground='#ffffff',
         relief=tk.FLAT, padx=10, pady=3,
         cursor='hand2',
      )

      tk.Button(bar, text='New',  command=self._cmd_new,  **btn_cfg).pack(side=tk.LEFT, padx=(6, 2))
      tk.Button(bar, text='Open', command=self._cmd_open, **btn_cfg).pack(side=tk.LEFT, padx=2)
      tk.Button(bar, text='Save', command=self._cmd_save, **btn_cfg).pack(side=tk.LEFT, padx=2)

      tk.Frame(bar, width=1, bg='#555555').pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=2)

      run_cfg = dict(btn_cfg)
      run_cfg.update(bg='#0e639c', activebackground='#1177bb', padx=14)
      tk.Button(bar, text='Run',  command=self._cmd_run,  **run_cfg).pack(side=tk.LEFT, padx=2)
      tk.Button(bar, text='Help', command=self._cmd_help, **btn_cfg).pack(side=tk.LEFT, padx=2)

      self._title_var = tk.StringVar(value='untitled')
      tk.Label(bar, textvariable=self._title_var,
               bg='#2d2d2d', fg='#888888',
               anchor=tk.W).pack(side=tk.LEFT, padx=12)

   def _build_editor(self):
      mono = tkfont.Font(family='Courier New', size=10)
      frame = tk.Frame(self)
      frame.pack(fill=tk.BOTH, expand=True)

      self._text = tk.Text(
         frame,
         font=mono,
         wrap=tk.NONE,
         undo=True,
         bg='#1e1e1e',
         fg='#d4d4d4',
         insertbackground='#d4d4d4',
         selectbackground='#264f78',
         relief=tk.FLAT,
         padx=6,
         pady=6,
      )
      vsb = tk.Scrollbar(frame, command=self._text.yview)
      hsb = tk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self._text.xview)
      self._text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

      vsb.pack(side=tk.RIGHT,  fill=tk.Y)
      hsb.pack(side=tk.BOTTOM, fill=tk.X)
      self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

      self._text.tag_configure(TAG_PAREN, background='#3c3c00')
      self._text.bind('<<Modified>>',    self._on_modified)
      self._text.bind('<KeyRelease>',    self._on_paren_check)
      self._text.bind('<ButtonRelease-1>', self._on_paren_check)

   # ---- paren matching ---------------------------------------------------

   def _on_paren_check(self, event=None):
      self._highlight_matching_paren()

   def _highlight_matching_paren(self):
      t = self._text
      t.tag_remove(TAG_PAREN, '1.0', tk.END)

      # Prefer the char immediately left of the cursor, then the char at it
      before = t.get('insert-1c', 'insert')
      at     = t.get('insert',    'insert+1c')
      if before in '()[]':
         paren_tk = 'insert-1c'
      elif at in '()[]':
         paren_tk = 'insert'
      else:
         return

      full = t.get('1.0', tk.END)
      pos  = len(t.get('1.0', paren_tk))
      if pos < 0 or pos >= len(full):
         return

      code  = make_code_map(full)
      match = find_match(full, code, pos)
      if match < 0:
         return

      t.tag_add(TAG_PAREN, paren_tk, paren_tk + '+1c')
      match_tk = '1.0+' + str(match) + 'c'
      t.tag_add(TAG_PAREN, match_tk, match_tk + '+1c')

   # ---- modified tracking ------------------------------------------------

   def _on_modified(self, event):
      if self._text.edit_modified():
         self._modified = True
         self._update_title()
         self._text.edit_modified(False)
         if self._state_path is not None:
            self._schedule_autosave()

   def _update_title(self):
      name = os.path.basename(self._filepath) if self._filepath else 'untitled'
      if self._modified:
         name = name + ' *'
      self._title_var.set(name)

   # ---- toolbar commands -------------------------------------------------

   def _cmd_new(self):
      if not self._confirm_discard():
         return
      self._text.delete('1.0', tk.END)
      self._filepath = None
      self._modified = False
      self._update_title()

   def _cmd_open(self):
      if not self._confirm_discard():
         return
      path = filedialog.askopenfilename(
         title='Open Scheme file',
         filetypes=[('Scheme files', '*.scm *.ss *.rkt'), ('All files', '*.*')],
      )
      if not path:
         return
      try:
         with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
      except OSError as e:
         messagebox.showerror('Open failed', str(e))
         return
      self._text.delete('1.0', tk.END)
      self._text.insert('1.0', content)
      self._text.edit_modified(False)
      self._filepath = path
      self._modified = False
      self._update_title()

   def _cmd_save(self):
      if self._filepath is None:
         path = filedialog.asksaveasfilename(
            title='Save Scheme file',
            defaultextension='.scm',
            filetypes=[('Scheme files', '*.scm *.ss *.rkt'), ('All files', '*.*')],
         )
         if not path:
            return
         self._filepath = path
      try:
         # 'end-1c' drops the Text widget's implicit trailing newline; using
         # 'end' would append one blank line on every save (and grow each time
         # the file is reopened and re-saved).
         content = self._text.get('1.0', 'end-1c')
         with open(self._filepath, 'w', encoding='utf-8') as f:
            f.write(content)
      except OSError as e:
         messagebox.showerror('Save failed', str(e))
         return
      self._text.edit_modified(False)
      self._modified = False
      self._update_title()

   def _cmd_run(self):
      source = self._text.get('1.0', tk.END).strip()
      if source:
         self._on_run(source)

   def _cmd_help(self):
      self._on_run('(help)')

   # ---- helpers ----------------------------------------------------------

   def _confirm_discard(self):
      if not self._modified:
         return True
      answer = messagebox.askyesnocancel(
         'Unsaved changes',
         'The editor has unsaved changes. Discard them?',
      )
      return answer is True

   # ---- session state -------------------------------------------------------

   def _schedule_autosave(self):
      if self._autosave_id is not None:
         self.after_cancel(self._autosave_id)
      self._autosave_id = self.after(2000, self._autosave)

   def _autosave(self):
      self._autosave_id = None
      if self._state_path is not None:
         self.save_state(self._state_path)

   def save_state(self, state_path: pathlib.Path):
      self._state_path = state_path
      try:
         # 'end-1c' drops the Text widget's implicit trailing newline; using
         # 'end' here is what made the session state (and thus the editor)
         # accrete a blank line on every save/restore cycle.
         content = self._text.get('1.0', 'end-1c')
         yview   = self._text.yview()[0]
      except Exception:
         return
      state = {'filepath': self._filepath, 'content': content, 'yview': yview}
      try:
         state_path.write_text(json.dumps(state), encoding='utf-8')
      except Exception:
         pass

   def restore_state(self, state_path: pathlib.Path):
      self._state_path = state_path
      try:
         state = json.loads(state_path.read_text(encoding='utf-8'))
      except (OSError, json.JSONDecodeError):
         return
      content  = state.get('content', '')
      filepath = state.get('filepath')
      yview    = state.get('yview', 0.0)
      self._text.delete('1.0', tk.END)
      self._text.insert('1.0', content)
      self._text.edit_modified(False)
      self._text.after(0, lambda: self._text.yview_moveto(yview))
      self._filepath = filepath
      if filepath and os.path.isfile(filepath):
         try:
            disk = pathlib.Path(filepath).read_text(encoding='utf-8')
         except OSError:
            disk = None
         self._modified = disk != content
      else:
         self._modified = bool(content.strip())
      self._update_title()
