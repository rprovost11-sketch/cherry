"""CherryApp: main window.  Two-pane PanedWindow (editor top, REPL bottom)
plus an interpreter selector bar at the top and a CWD status button at the
very bottom.

Interpreters are not hard-coded: the list (display name, launch command line,
working directory, and test-suite paths) lives in ~/.cherry/settings.json and is
edited through the Settings... dialog.  The constants below are only the seed
used to write that file on first run."""

import json
import os
import pathlib
import shlex
import sys
import tkinter as tk
from tkinter import filedialog, font as tkfont

_CHERRY_VERSION = '0.2.2'
_TITLE_PREFIX   = 'Cherry v' + _CHERRY_VERSION + ' - '

_CHERRY_DIR = pathlib.Path.home() / '.cherry'

def _state_path(key):
    return _CHERRY_DIR / ('interp' + str(key) + '.json')

_SETTINGS_PATH = _CHERRY_DIR / 'settings.json'

def _load_settings():
    try:
        with open(_SETTINGS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def _save_settings(settings):
    try:
        with open(_SETTINGS_PATH, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass

_LISP_DIR = pathlib.Path(__file__).resolve().parents[1]


def _quote(s):
    """Quote a path for inclusion in a command-line string if it has spaces."""
    s = str(s)
    if ' ' in s and not (s.startswith('"') and s.endswith('"')):
        return '"' + s + '"'
    return s


def _parse_cmdline(cmd):
    """Parse a command-line string into an argv list, preserving Windows
    backslashes and stripping the surrounding double-quotes from each token.
    A list is returned unchanged (already parsed)."""
    if isinstance(cmd, list):
        return cmd
    text = str(cmd or '').strip()
    if not text:
        return []
    try:
        parts = shlex.split(text, posix=False)
    except ValueError:
        parts = text.split()
    out = []
    for p in parts:
        if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
            p = p[1:-1]
        out.append(p)
    return out


def _default_interpreters():
    """The seed interpreter list, written to settings.json on first run.  After
    that the user's saved list wins -- nothing here is consulted again."""
    py = _quote(sys.executable)
    return [
        {'id': '1', 'label': '1 · PythonsLisp',
         'cmd':  py + ' -u -m pythonslisp',
         'cwd':  str(_LISP_DIR / '1PythonsLisp')},
        {'id': '2', 'label': '2 · CPPScheme',
         'cmd':  _quote(str(_LISP_DIR / '2CPPScheme' / 'build' / 'Release' / 'scheme.exe')),
         'cwd':  str(_LISP_DIR / '2CPPScheme')},
        {'id': '3', 'label': '3 · PyScheme',
         'cmd':  py + ' -u -m pyscheme',
         'cwd':  str(_LISP_DIR / '3PyScheme')},
        {'id': '4', 'label': '4 · CPPScheme2',
         'cmd':  _quote(str(_LISP_DIR / '4CPPScheme2' / 'build' / 'Release' / 'cppscheme2.exe')),
         'cwd':  str(_LISP_DIR / '4CPPScheme2')},
    ]


def _normalize_interpreters(raw):
    """Coerce a loaded 'interpreters' value into a clean list of dicts with all
    expected string fields and unique ids.  Returns None if it is missing or
    yields no usable entry (so the caller can fall back to the seed list)."""
    if not isinstance(raw, list):
        return None
    out  = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get('label', '')).strip()
        cmd   = str(item.get('cmd', '')).strip()
        if not label or not cmd:
            continue
        iid = str(item.get('id', '')).strip()
        if not iid or iid in seen:
            iid = _fresh_id(seen)
        seen.add(iid)
        out.append({
            'id':    iid,
            'label': label,
            'cmd':   cmd,
            'cwd':   str(item.get('cwd', '') or ''),
        })
    return out or None


def _fresh_id(existing):
    n = 1
    while True:
        cand = 'i' + str(n)
        if cand not in existing:
            return cand
        n += 1


# ---- appearance / misc settings (defaults + readers) ----------------------

_DEFAULT_FONT_FAMILY = 'Courier New'
_DEFAULT_EDITOR_SIZE = 10
_DEFAULT_REPL_SIZE   = 10
_MIN_FONT_SIZE       = 6
_MAX_FONT_SIZE       = 72


def _font_setting(settings, key, default_size):
    """Read a {'family', 'size'} font setting, clamping/repairing bad values."""
    raw = settings.get(key)
    family, size = _DEFAULT_FONT_FAMILY, default_size
    if isinstance(raw, dict):
        family = str(raw.get('family') or _DEFAULT_FONT_FAMILY)
        try:
            size = int(raw.get('size', default_size))
        except (TypeError, ValueError):
            size = default_size
        if not (_MIN_FONT_SIZE <= size <= _MAX_FONT_SIZE):
            size = default_size
    return {'family': family, 'size': size}


from cherry.subprocess_bridge import SubprocessBridge
from cherry.editor_pane       import EditorPane
from cherry.repl_pane         import ReplPane
from cherry.settings_dialog   import SettingsDialog


class CherryApp(tk.Tk):
   def __init__(self):
      super().__init__()
      _CHERRY_DIR.mkdir(exist_ok=True)
      self._settings = _load_settings()

      # Interpreter list: user-configured, seeded on first run.
      interps = _normalize_interpreters(self._settings.get('interpreters'))
      if interps is None:
         interps = _default_interpreters()
         self._settings['interpreters'] = interps
         _save_settings(self._settings)
      self._interpreters = interps

      # Global scheme-tests directory (applied via ]scheme-tests; see
      # _init_commands).  Seed it once to the repo's conventional location if
      # that exists -- a user-editable default, like the seeded interpreters,
      # not a baked-in constant.  Absent stays absent so tests prompt for setup.
      if 'scheme_tests_dir' not in self._settings:
         seed_tests = _LISP_DIR / 'scheme-tests'
         self._settings['scheme_tests_dir'] = str(seed_tests) if seed_tests.is_dir() else ''
         _save_settings(self._settings)

      # Restore the last-used interpreter; fall back to the seed default ('4')
      # if present, else the first in the list.
      cur = self._settings.get('last_interp_id')
      if self._interp_by_id(cur) is None:
         cur = '4' if self._interp_by_id('4') else self._interpreters[0]['id']
      self._current_interp = cur

      cfg = self._current_cfg()
      self.title(_TITLE_PREFIX + cfg['label'])
      self.configure(bg='#1e1e1e')

      # Restore the window's saved size + screen position (sash is restored in
      # _build once the window is realized); fall back to a sensible default.
      self.geometry(self._settings.get('window_geometry', '675x700'))
      self._developer_mode = self._settings.get('developer_mode', True)

      # Appearance preferences (live-editable via Settings...).
      self._editor_font_cfg = _font_setting(self._settings, 'editor_font',
                                            _DEFAULT_EDITOR_SIZE)
      self._repl_font_cfg   = _font_setting(self._settings, 'repl_font',
                                            _DEFAULT_REPL_SIZE)

      self._bridge = SubprocessBridge(cmd=self._cmd_list(cfg),
                                      cwd=cfg.get('cwd') or None,
                                      init_commands=self._init_commands())
      self._build()
      self._repl.set_test_tools_visible(self._developer_mode)
      self._editor.restore_state(_state_path(self._current_interp))
      self.protocol('WM_DELETE_WINDOW', self._on_close)

   # ---- interpreter config helpers ---------------------------------------

   def _interp_by_id(self, iid):
      if not iid:
         return None
      for it in self._interpreters:
         if it['id'] == iid:
            return it
      return None

   def _current_cfg(self):
      return self._interp_by_id(self._current_interp) or self._interpreters[0]

   def _cmd_list(self, cfg):
      return _parse_cmdline(cfg.get('cmd'))

   def _init_commands(self):
      """Listener lines Cherry injects after each (re)spawn for configuration
      that can't ride argv.  Currently the global scheme-tests directory, applied
      with ]scheme-tests -- which by the interpreter's own rules overrides a
      -T/--scheme-tests option and $SCHEME_TESTS_DIR.  Cherry never touches the
      command line; interpreters that don't know the command ignore it."""
      cmds = []
      tdir = (self._settings.get('scheme_tests_dir') or '').strip()
      if tdir:
         cmds.append(']scheme-tests ' + tdir)
      return cmds

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

      self._interp_var = tk.StringVar(value=self._current_cfg()['label'])
      om = tk.OptionMenu(hdr, self._interp_var, '')
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
      self._interp_om = om
      self._rebuild_interp_menu()

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

      # ---- Settings... button (opens the configuration dialog: Dev Mode,
      #      interpreters, and test-suite paths).  Packed right, before the
      #      elastic CWD label, so it anchors to the right edge. ----
      tk.Button(
         hdr,
         text='Settings...',
         command=self._open_settings,
         bg='#3c3c3c', fg='#d4d4d4',
         activebackground='#505050', activeforeground='#ffffff',
         relief=tk.FLAT,
         font=tkfont.Font(family='Courier New', size=9),
         padx=8, pady=2, cursor='hand2',
      ).pack(side=tk.RIGHT, padx=6)

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

      self._editor = EditorPane(paned, on_run=self._on_run, bg='#1e1e1e',
                                font_family=self._editor_font_cfg['family'],
                                font_size=self._editor_font_cfg['size'])
      self._repl   = ReplPane(paned, bridge=self._bridge,
                              get_cwd=lambda: self._cwd_var.get(),
                              get_interp_cmd=lambda: self._cmd_list(self._current_cfg()),
                              get_suite_selection=lambda: self._settings.get('suite_selection', {}),
                              save_suite_selection=self._save_suite_selection,
                              font_family=self._repl_font_cfg['family'],
                              font_size=self._repl_font_cfg['size'],
                              bg='#1e1e1e')

      paned.add(self._editor, stretch='always')
      paned.add(self._repl,   stretch='always')

      self._paned = paned
      # Restore the split-pane sash after the window is realized (so the paned
      # height is known); default to the middle on first run.
      self.after(100, self._restore_sash)

   def _rebuild_interp_menu(self):
      """Repopulate the interpreter drop-down from the current config list.
      Called at build time and after the Settings dialog changes the list."""
      menu = self._interp_om['menu']
      menu.delete(0, 'end')
      for it in self._interpreters:
         menu.add_command(
            label=it['label'],
            command=lambda iid=it['id']: self._on_interp_select(iid),
         )
      self._interp_var.set(self._current_cfg()['label'])

   def _restore_sash(self):
      saved = self._settings.get('sash_y')
      if saved is None:
         saved = self.winfo_height() // 2
      h = self._paned.winfo_height()
      if h > 40:
         saved = max(20, min(int(saved), h - 20))   # keep the sash on-screen
      try:
         self._paned.sash_place(0, 0, int(saved))
      except tk.TclError:
         pass

   def _on_interp_select(self, iid):
      if iid == self._current_interp:
         self._interp_var.set(self._current_cfg()['label'])
         return
      self._switch_interpreter(iid)

   def _switch_interpreter(self, iid):
      cfg = self._interp_by_id(iid)
      if cfg is None:
         return

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
         self._interp_var.set(self._current_cfg()['label'])
         return

      self._editor.save_state(_state_path(self._current_interp))
      self._bridge.shutdown()
      new_bridge = SubprocessBridge(cmd=self._cmd_list(cfg),
                                    cwd=cfg.get('cwd') or None,
                                    init_commands=self._init_commands())
      self._bridge = new_bridge
      self._current_interp = iid
      self._cwd_var.set(cfg.get('cwd') or os.getcwd())
      self._repl.set_bridge(new_bridge)
      self._editor.restore_state(_state_path(iid))
      self.title(_TITLE_PREFIX + cfg['label'])
      self._interp_var.set(cfg['label'])
      self._settings['last_interp_id'] = iid
      _save_settings(self._settings)

   # ---- settings dialog --------------------------------------------------

   def _open_settings(self):
      SettingsDialog(self,
                     interpreters=[dict(it) for it in self._interpreters],
                     developer_mode=self._developer_mode,
                     editor_font=dict(self._editor_font_cfg),
                     repl_font=dict(self._repl_font_cfg),
                     scheme_tests_dir=self._settings.get('scheme_tests_dir', ''),
                     on_save=self._apply_settings)

   def _apply_settings(self, result):
      """Callback from the Settings dialog: adopt the edited settings, persist
      them, and reconcile the running session.  `result` carries 'interpreters',
      'developer_mode', 'editor_font', and 'repl_font'."""
      new_interpreters = result['interpreters']
      developer_mode   = result['developer_mode']

      old = self._interp_by_id(self._current_interp)
      old_cmd = old.get('cmd') if old else None
      old_cwd = (old.get('cwd') or '') if old else ''
      old_tdir = (self._settings.get('scheme_tests_dir') or '').strip()

      # Canonicalize (drop any stale fields, dedupe ids) before storing so the
      # saved file always matches the current schema.
      new_interpreters = _normalize_interpreters(new_interpreters) or new_interpreters
      self._interpreters = new_interpreters
      self._settings['interpreters'] = new_interpreters

      new_tdir = (result.get('scheme_tests_dir') or '').strip()
      self._settings['scheme_tests_dir'] = new_tdir

      if developer_mode != self._developer_mode:
         self._developer_mode = developer_mode
         self._repl.set_test_tools_visible(developer_mode)
         self._settings['developer_mode'] = developer_mode

      # ---- appearance (live) ----
      self._editor_font_cfg = result['editor_font']
      self._repl_font_cfg   = result['repl_font']
      self._editor.set_font(self._editor_font_cfg['family'],
                            self._editor_font_cfg['size'])
      self._repl.set_font(self._repl_font_cfg['family'],
                          self._repl_font_cfg['size'])
      self._settings['editor_font'] = self._editor_font_cfg
      self._settings['repl_font']   = self._repl_font_cfg

      cur = self._interp_by_id(self._current_interp)
      if cur is None:
         # The active interpreter was removed -- fall back to the first one.
         self._fallback_to_first()
      else:
         new_cwd = cur.get('cwd') or ''
         cwd_changed = new_cwd != old_cwd
         # A command-line change needs a fresh process; so does a spaced cwd,
         # since ]cd takes a single token (Popen's cwd handles spaces).  Both
         # respawn paths re-apply the cwd and scheme-tests dir on boot.
         if cur.get('cmd') != old_cmd or (cwd_changed and ' ' in new_cwd):
            self._restart_current()
         else:
            # Apply launch-affecting changes in place via listener commands so
            # the live REPL session (in-memory definitions) is preserved.
            if cwd_changed:
               self._apply_cwd_live(new_cwd)
            if new_tdir != old_tdir:
               self._apply_scheme_tests_live(new_tdir)
         self.title(_TITLE_PREFIX + cur['label'])

      # Keep the bridge's replay list current for any future respawn.
      self._bridge.set_init_commands(self._init_commands())
      self._rebuild_interp_menu()
      _save_settings(self._settings)
      self._warn_scheme_tests_override(new_tdir, new_tdir != old_tdir)

   def _apply_cwd_live(self, cwd):
      """Apply a changed working directory to the running interpreter via ]cd,
      without a restart, and keep it for future respawns.  (Callers route a
      spaced path through a restart instead, since ]cd takes a single token.)"""
      self._bridge.set_cwd(cwd or None)
      if cwd:
         self._bridge.chdir(cwd)
         self._cwd_var.set(cwd)

   def _apply_scheme_tests_live(self, tdir):
      """Apply a changed scheme-tests directory to the running interpreter via
      ]scheme-tests, no restart.  The echo is left visible as confirmation.  A
      cleared value can't be unset live -- it takes effect on the next respawn
      (the replay list, updated by the caller, drops it)."""
      if tdir:
         self._bridge.submit(']scheme-tests ' + tdir)

   def _warn_scheme_tests_override(self, tdir, changed):
      """Warn only on a real conflict: the global scheme-tests dir is set AND
      some interpreter's command line also carries -T/--scheme-tests, which the
      Settings value (applied via ]scheme-tests) overrides.  Fires only when the
      dir was just changed, so it never nags."""
      if not tdir or not changed:
         return
      clashing = [it['label'] for it in self._interpreters
                  if any(a == '-T' or a.startswith('--scheme-tests')
                         for a in self._cmd_list(it))]
      if not clashing:
         return
      from tkinter import messagebox
      messagebox.showinfo(
         'Scheme-tests directory',
         'The Scheme-tests directory set in Settings is applied with '
         ']scheme-tests, which overrides any -T/--scheme-tests path on a '
         'command line.\n\nIt takes precedence for:\n  ' + '\n  '.join(clashing),
         parent=self)

   def _restart_current(self):
      cfg = self._current_cfg()
      self._bridge.shutdown()
      new_bridge = SubprocessBridge(cmd=self._cmd_list(cfg),
                                    cwd=cfg.get('cwd') or None,
                                    init_commands=self._init_commands())
      self._bridge = new_bridge
      self._cwd_var.set(cfg.get('cwd') or os.getcwd())
      self._repl.set_bridge(new_bridge)

   def _fallback_to_first(self):
      self._editor.save_state(_state_path(self._current_interp))
      cfg = self._interpreters[0]
      self._restart_current_to(cfg)

   def _restart_current_to(self, cfg):
      self._bridge.shutdown()
      new_bridge = SubprocessBridge(cmd=self._cmd_list(cfg),
                                    cwd=cfg.get('cwd') or None,
                                    init_commands=self._init_commands())
      self._bridge = new_bridge
      self._current_interp = cfg['id']
      self._cwd_var.set(cfg.get('cwd') or os.getcwd())
      self._repl.set_bridge(new_bridge)
      self._editor.restore_state(_state_path(cfg['id']))
      self.title(_TITLE_PREFIX + cfg['label'])
      self._settings['last_interp_id'] = cfg['id']

   def _save_suite_selection(self, config):
      # Persist the Test Suites... checkbox configuration (saved on Run).
      self._settings['suite_selection'] = config
      _save_settings(self._settings)

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

   def _save_geometry(self):
      """Persist the window size + screen position and the split-pane sash y so
      the layout is restored on next launch.  Best-effort: never block close."""
      try:
         self._settings['window_geometry'] = self.geometry()   # "WxH+X+Y"
         self._settings['sash_y'] = self._paned.sash_coord(0)[1]
         _save_settings(self._settings)
      except Exception:
         pass

   def _on_close(self):
      self._save_geometry()
      self._editor.save_state(_state_path(self._current_interp))
      self._bridge.shutdown()
      self.destroy()


def main():
   app = CherryApp()
   app.mainloop()
