"""SettingsDialog: modal configuration dialog for Cherry.

Lets the end-user toggle Dev Mode and fully configure the interpreter list --
display name, launch command line, working directory, and the test-suite paths
that used to be hard-coded constants -- so no interpreter is baked into the
source.  The dialog owns no persistence: on Save it hands a fresh interpreter
list and the dev-mode flag back to the caller through the on_save callback,
which is responsible for applying and saving them.
"""

import os
import tkinter as tk
from tkinter import filedialog, font as tkfont

_BG       = '#2d2d2d'
_FG       = '#d4d4d4'
_MUTED    = '#888888'
_ENTRY_BG = '#1e1e1e'
_FIELD    = '#3c3c3c'


def _font(size=9, weight='normal'):
   return tkfont.Font(family='Courier New', size=size, weight=weight)


def _fresh_id(existing):
   """Return an interpreter id not already in the given set."""
   n = 1
   while True:
      cand = 'i' + str(n)
      if cand not in existing:
         return cand
      n += 1


class _InterpRow:
   """One interpreter's set of entry widgets within the scrollable list.

   Holds the row's stable id (so per-interpreter editor state files survive an
   edit) plus a tk variable for each configurable field."""

   def __init__(self, dialog, parent, cfg):
      self.dialog = dialog
      self.iid    = cfg.get('id') or _fresh_id(dialog._used_ids())

      self.frame = tk.Frame(parent, bg=_BG, highlightbackground='#555555',
                            highlightthickness=1, padx=8, pady=6)
      self.frame.pack(fill=tk.X, padx=2, pady=4)

      self.name_var = tk.StringVar(value=cfg.get('label', ''))
      self.cmd_var  = tk.StringVar(value=cfg.get('cmd', ''))
      self.cwd_var  = tk.StringVar(value=cfg.get('cwd', ''))

      self._field('Name',         self.name_var, 0)
      self._field('Command line', self.cmd_var,  1)
      self._field('Working dir',  self.cwd_var,  2, browse=True)
      self.frame.columnconfigure(1, weight=1)

      tk.Button(self.frame, text='Remove', command=self._remove,
                bg='#6b1f1f', fg=_FG, activebackground='#8b2f2f',
                activeforeground='#ffffff', relief=tk.FLAT,
                padx=10, pady=2, cursor='hand2', font=_font(),
                ).grid(row=3, column=2, sticky=tk.E, pady=(6, 0))

   def _field(self, label, var, r, browse=False):
      tk.Label(self.frame, text=label, bg=_BG, fg=_MUTED, anchor=tk.W,
               font=_font()).grid(row=r, column=0, sticky=tk.W,
                                  padx=(0, 8), pady=2)
      tk.Entry(self.frame, textvariable=var, bg=_ENTRY_BG, fg=_FG,
               insertbackground=_FG, relief=tk.FLAT, font=_font(),
               ).grid(row=r, column=1, sticky=tk.EW, pady=2)
      if browse:
         tk.Button(self.frame, text='...',
                   command=lambda: self._browse_dir(var),
                   bg=_FIELD, fg=_FG, activebackground='#505050',
                   activeforeground='#ffffff', relief=tk.FLAT,
                   padx=6, cursor='hand2', font=_font(),
                   ).grid(row=r, column=2, sticky=tk.E, padx=(6, 0), pady=2)

   def _browse_dir(self, var):
      cur  = var.get().strip()
      init = cur if os.path.isdir(cur) else os.getcwd()
      path = filedialog.askdirectory(title='Choose directory',
                                     initialdir=init, parent=self.dialog)
      if path:
         var.set(path)

   def _remove(self):
      self.dialog._remove_row(self)

   def to_cfg(self):
      return {
         'id':    self.iid,
         'label': self.name_var.get().strip(),
         'cmd':   self.cmd_var.get().strip(),
         'cwd':   self.cwd_var.get().strip(),
      }


class SettingsDialog(tk.Toplevel):
   """Modal settings window.  Construct with the current interpreter list and
   dev-mode flag; on Save it calls on_save(new_interpreters, developer_mode)."""

   def __init__(self, parent, interpreters, developer_mode,
                editor_font, repl_font, history_max, on_save):
      super().__init__(parent)
      self._on_save = on_save
      self._rows    = []

      self.title('Settings')
      self.configure(bg=_BG)
      self.transient(parent)
      self.minsize(560, 520)
      self.geometry('660x680')

      self._dev_var = tk.BooleanVar(value=bool(developer_mode))
      self._build(interpreters, editor_font, repl_font, history_max)

      self.update_idletasks()
      w = self.winfo_width()
      h = self.winfo_height()
      x = parent.winfo_x() + (parent.winfo_width()  - w) // 2
      y = parent.winfo_y() + (parent.winfo_height() - h) // 2
      self.geometry('+' + str(max(x, 0)) + '+' + str(max(y, 0)))
      self.protocol('WM_DELETE_WINDOW', self._cancel)
      self.grab_set()

   # ---- construction -----------------------------------------------------

   def _build(self, interpreters, editor_font, repl_font, history_max):
      # ---- General -------------------------------------------------------
      gen = tk.Frame(self, bg=_BG)
      gen.pack(fill=tk.X, padx=16, pady=(14, 4))
      tk.Checkbutton(
         gen, text='Dev Mode   (show the test-suite tools in the REPL)',
         variable=self._dev_var,
         bg=_BG, fg=_FG, activebackground=_BG, activeforeground='#ffffff',
         selectcolor=_ENTRY_BG, highlightthickness=0, anchor=tk.W,
         cursor='hand2', font=_font(),
      ).pack(side=tk.LEFT)

      tk.Frame(self, height=1, bg='#555555').pack(fill=tk.X, padx=16, pady=6)

      # ---- Appearance ----------------------------------------------------
      self._build_appearance(editor_font, repl_font, history_max)

      tk.Frame(self, height=1, bg='#555555').pack(fill=tk.X, padx=16, pady=6)

      # ---- Interpreters header ------------------------------------------
      hdr = tk.Frame(self, bg=_BG)
      hdr.pack(fill=tk.X, padx=16, pady=(2, 2))
      tk.Label(hdr, text='Interpreters', bg=_BG, fg=_FG,
               font=_font(10, 'bold')).pack(side=tk.LEFT)
      tk.Button(hdr, text='Add interpreter', command=self._add_row,
                bg='#1f4e2b', fg=_FG, activebackground='#2f6e3b',
                activeforeground='#ffffff', relief=tk.FLAT,
                padx=10, pady=2, cursor='hand2', font=_font(),
                ).pack(side=tk.RIGHT)

      # Hint on its own line so it can't be clipped by the Add button above.
      tk.Label(self,
               text="Command line is parsed like a shell line "
                    "(quote paths with spaces).",
               bg=_BG, fg=_MUTED, anchor=tk.W, font=_font(),
               ).pack(fill=tk.X, padx=16, pady=(0, 2))

      # ---- scrollable interpreter list ----------------------------------
      outer = tk.Frame(self, bg=_BG)
      outer.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
      self._canvas = tk.Canvas(outer, bg=_BG, highlightthickness=0)
      sb = tk.Scrollbar(outer, command=self._canvas.yview)
      self._canvas.configure(yscrollcommand=sb.set)
      sb.pack(side=tk.RIGHT, fill=tk.Y)
      self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

      self._list = tk.Frame(self._canvas, bg=_BG)
      self._list_win = self._canvas.create_window((0, 0), window=self._list,
                                                  anchor='nw')
      self._list.bind('<Configure>', lambda e: self._canvas.configure(
         scrollregion=self._canvas.bbox('all')))
      self._canvas.bind('<Configure>', lambda e: self._canvas.itemconfigure(
         self._list_win, width=e.width))
      # Only route the wheel to this canvas while the pointer is over it, and
      # release the binding on leave/destroy so it never lingers app-wide.
      self._canvas.bind('<Enter>',
                        lambda e: self._canvas.bind_all('<MouseWheel>', self._on_wheel))
      self._canvas.bind('<Leave>', lambda e: self._unbind_wheel())

      for cfg in interpreters:
         self._rows.append(_InterpRow(self, self._list, cfg))

      # ---- validation message + action buttons --------------------------
      self._err = tk.Label(self, text='', bg=_BG, fg='#f44747', anchor=tk.W,
                           font=_font())
      self._err.pack(fill=tk.X, padx=16)

      btns = tk.Frame(self, bg=_BG)
      btns.pack(fill=tk.X, padx=16, pady=(4, 14))
      tk.Button(btns, text='Save', command=self._save,
                bg='#1f4e6b', fg=_FG, activebackground='#2f6e8b',
                activeforeground='#ffffff', relief=tk.FLAT,
                padx=16, pady=4, cursor='hand2', font=_font(),
                ).pack(side=tk.RIGHT, padx=(6, 0))
      tk.Button(btns, text='Cancel', command=self._cancel,
                bg=_FIELD, fg=_FG, activebackground='#505050',
                activeforeground='#ffffff', relief=tk.FLAT,
                padx=16, pady=4, cursor='hand2', font=_font(),
                ).pack(side=tk.RIGHT)

   # ---- appearance section ----------------------------------------------

   def _build_appearance(self, editor_font, repl_font, history_max):
      self._ed_family = tk.StringVar(value=editor_font.get('family', 'Courier New'))
      self._ed_size   = tk.StringVar(value=str(editor_font.get('size', 10)))
      self._rp_family = tk.StringVar(value=repl_font.get('family', 'Courier New'))
      self._rp_size   = tk.StringVar(value=str(repl_font.get('size', 10)))
      self._hist_var  = tk.StringVar(value=str(history_max))

      # Preview fonts are reconfigured live as the user edits family/size.
      self._ed_preview = _font(int(editor_font.get('size', 10)))
      self._rp_preview = _font(int(repl_font.get('size', 10)))

      self._font_row('Editor font', self._ed_family, self._ed_size,
                     self._ed_preview)
      self._font_row('REPL font', self._rp_family, self._rp_size,
                     self._rp_preview)

      hist = tk.Frame(self, bg=_BG)
      hist.pack(fill=tk.X, padx=16, pady=(6, 0))
      tk.Label(hist, text='REPL history', bg=_BG, fg=_FG, width=11, anchor=tk.W,
               font=_font()).pack(side=tk.LEFT)
      tk.Spinbox(hist, from_=10, to=100000, increment=50, width=8,
                 textvariable=self._hist_var, justify=tk.RIGHT,
                 bg=_ENTRY_BG, fg=_FG, relief=tk.FLAT, insertbackground=_FG,
                 buttonbackground=_FIELD, font=_font()).pack(side=tk.LEFT)
      tk.Label(hist, text='entries kept across sessions', bg=_BG, fg=_MUTED,
               font=_font()).pack(side=tk.LEFT, padx=(8, 0))

      # Initial preview render + live updates on edit.
      self._refresh_preview(self._ed_family, self._ed_size, self._ed_preview)
      self._refresh_preview(self._rp_family, self._rp_size, self._rp_preview)
      for v in (self._ed_family, self._ed_size):
         v.trace_add('write', lambda *a: self._refresh_preview(
            self._ed_family, self._ed_size, self._ed_preview))
      for v in (self._rp_family, self._rp_size):
         v.trace_add('write', lambda *a: self._refresh_preview(
            self._rp_family, self._rp_size, self._rp_preview))

   def _font_row(self, label, fam_var, size_var, preview_font):
      row = tk.Frame(self, bg=_BG)
      row.pack(fill=tk.X, padx=16, pady=(4, 0))
      tk.Label(row, text=label, bg=_BG, fg=_FG, width=11, anchor=tk.W,
               font=_font()).grid(row=0, column=0, sticky=tk.W)
      tk.Entry(row, textvariable=fam_var, bg=_ENTRY_BG, fg=_FG,
               insertbackground=_FG, relief=tk.FLAT, font=_font(),
               ).grid(row=0, column=1, sticky=tk.EW, padx=(0, 8))
      tk.Label(row, text='Size', bg=_BG, fg=_MUTED, font=_font(),
               ).grid(row=0, column=2, padx=(0, 4))
      tk.Spinbox(row, from_=6, to=72, width=4, textvariable=size_var,
                 justify=tk.RIGHT, bg=_ENTRY_BG, fg=_FG, relief=tk.FLAT,
                 insertbackground=_FG, buttonbackground=_FIELD, font=_font(),
                 ).grid(row=0, column=3)
      tk.Label(row, text='(define (f x) (* x x))   ; preview 0123',
               bg=_ENTRY_BG, fg='#9a9a9a', anchor=tk.W, font=preview_font,
               padx=6, pady=2).grid(row=1, column=1, columnspan=3,
                                    sticky=tk.EW, pady=(3, 0))
      row.columnconfigure(1, weight=1)

   def _refresh_preview(self, fam_var, size_var, preview_font):
      fam = fam_var.get().strip() or 'Courier New'
      try:
         size = int(size_var.get())
      except (TypeError, ValueError):
         return
      if 4 <= size <= 200:
         try:
            preview_font.configure(family=fam, size=size)
         except tk.TclError:
            pass

   def _read_font(self, fam_var, size_var):
      """Return {'family','size'} if valid, else None."""
      fam = fam_var.get().strip() or 'Courier New'
      try:
         size = int(size_var.get())
      except (TypeError, ValueError):
         return None
      if not (6 <= size <= 72):
         return None
      return {'family': fam, 'size': size}

   # ---- scrolling helpers ------------------------------------------------

   def _on_wheel(self, event):
      self._canvas.yview_scroll(int(-event.delta / 120), 'units')

   def _unbind_wheel(self):
      try:
         self._canvas.unbind_all('<MouseWheel>')
      except tk.TclError:
         pass

   def _sync_scrollregion(self):
      self.update_idletasks()
      self._canvas.configure(scrollregion=self._canvas.bbox('all'))

   # ---- row management ---------------------------------------------------

   def _used_ids(self):
      return {r.iid for r in self._rows}

   def _add_row(self):
      cfg = {'id': _fresh_id(self._used_ids()), 'label': '', 'cmd': '', 'cwd': ''}
      self._rows.append(_InterpRow(self, self._list, cfg))
      self._sync_scrollregion()
      self._canvas.yview_moveto(1.0)

   def _remove_row(self, row):
      row.frame.destroy()
      self._rows.remove(row)
      self._sync_scrollregion()

   # ---- save / cancel ----------------------------------------------------

   def _save(self):
      cfgs = [r.to_cfg() for r in self._rows]
      if not cfgs:
         self._err.configure(text='Add at least one interpreter.')
         return
      for c in cfgs:
         if not c['label']:
            self._err.configure(text='Every interpreter needs a name.')
            return
         if not c['cmd']:
            self._err.configure(
               text='Interpreter "' + c['label'] + '" needs a command line.')
            return
      # Defend against duplicate ids (e.g. a hand-edited settings file).
      seen = set()
      for c in cfgs:
         if c['id'] in seen:
            c['id'] = _fresh_id(seen)
         seen.add(c['id'])

      editor_font = self._read_font(self._ed_family, self._ed_size)
      repl_font   = self._read_font(self._rp_family, self._rp_size)
      if editor_font is None or repl_font is None:
         self._err.configure(
            text='Font size must be a whole number from 6 to 72.')
         return
      try:
         history_max = int(self._hist_var.get())
      except (TypeError, ValueError):
         history_max = None
      if history_max is None or not (10 <= history_max <= 100000):
         self._err.configure(
            text='REPL history must be a whole number from 10 to 100000.')
         return

      self._unbind_wheel()
      self._on_save({
         'interpreters':   cfgs,
         'developer_mode': bool(self._dev_var.get()),
         'editor_font':    editor_font,
         'repl_font':      repl_font,
         'history_max':    history_max,
      })
      self.destroy()

   def _cancel(self):
      self._unbind_wheel()
      self.destroy()
