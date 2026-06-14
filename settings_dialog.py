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

   def __init__(self, parent, interpreters, developer_mode, on_save):
      super().__init__(parent)
      self._on_save = on_save
      self._rows    = []

      self.title('Settings')
      self.configure(bg=_BG)
      self.transient(parent)
      self.minsize(560, 420)
      self.geometry('660x600')

      self._dev_var = tk.BooleanVar(value=bool(developer_mode))
      self._build(interpreters)

      self.update_idletasks()
      w = self.winfo_width()
      h = self.winfo_height()
      x = parent.winfo_x() + (parent.winfo_width()  - w) // 2
      y = parent.winfo_y() + (parent.winfo_height() - h) // 2
      self.geometry('+' + str(max(x, 0)) + '+' + str(max(y, 0)))
      self.protocol('WM_DELETE_WINDOW', self._cancel)
      self.grab_set()

   # ---- construction -----------------------------------------------------

   def _build(self, interpreters):
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

      self._unbind_wheel()
      self._on_save(cfgs, bool(self._dev_var.get()))
      self.destroy()

   def _cancel(self):
      self._unbind_wheel()
      self.destroy()
