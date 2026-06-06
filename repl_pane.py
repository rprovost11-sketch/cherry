"""ReplPane: a terminal-style REPL widget backed by a single tk.Text.

The full buffer is always editable, but key bindings enforce the rule that
destructive keystrokes (BackSpace, Delete, printable characters) cannot
modify text before the 'input_start' mark.  Cursor movement keys work
freely throughout.  Up/Down cycle history instead of moving the cursor
vertically.

History is persisted to ~/.cherry_history across sessions.
"""

import os
import re
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont

from pyscheme.Utils         import paren_state
from pyscheme.Listener      import Listener
from cherry.parens import make_code_map, find_match

PROMPT        = '>>> '
CONT_PROMPT   = '... '
DEBUG_PROMPT  = 'debug> '
_HIST_FILE    = os.path.expanduser('~/.cherry_history')
_HIST_MAX     = 500

TAG_PROMPT = 'prompt'
TAG_OUTPUT = 'output'
TAG_RESULT = 'result'
TAG_ERROR  = 'error'
TAG_INPUT  = 'input'
TAG_BANNER = 'banner'
TAG_PAREN  = 'paren_match'

# ---- ANSI SGR -> Tk rendering -----------------------------------------
# The interpreter emits terminal color escape codes when its
# ]toggle-tty-color flag is on (cherry turns it on after boot).  The
# faithful-replace model: these codes drive the colors, so the bridge's
# ==>/%%% lines arrive as plain 'output' (their markers are ANSI-wrapped)
# and we render the interpreter's own colors here.

_SGR_RE = re.compile('\x1b\\[([0-9;]*)m')

# Standard 16-color foreground palette (VS Code dark theme), keyed by SGR code.
_ANSI_FG = {
   30: '#000000', 31: '#cd3131', 32: '#0dbc79', 33: '#e5e510',
   34: '#2472c8', 35: '#bc3fbc', 36: '#11a8cd', 37: '#e5e5e5',
   90: '#666666', 91: '#f14c4c', 92: '#23d18b', 93: '#f5f543',
   94: '#3b8eea', 95: '#d670d6', 96: '#29b8db', 97: '#ffffff',
}


def _apply_sgr(state, params):
   """Update an SGR state dict in place from one escape's parameter string.
   Tracks foreground / bold / dim; other attributes are parsed and ignored."""
   codes = params.split(';') if params else ['0']
   for c in codes:
      n = int(c) if c else 0
      if n == 0:
         state['fg']   = None
         state['bold'] = False
         state['dim']  = False
      elif n == 1:
         state['bold'] = True
      elif n == 2:
         state['dim'] = True
      elif n == 22:
         state['bold'] = False
         state['dim']  = False
      elif n == 39:
         state['fg'] = None
      elif n in _ANSI_FG:
         state['fg'] = n


def _state_tags(state, base_tag):
   """Map an SGR state to a tuple of Tk tag names.  A run with an explicit
   foreground uses the ANSI color tag (not base_tag, so its color shows);
   an uncolored run falls back to base_tag (or a dim tag)."""
   tags = []
   if state['fg'] is not None:
      tags.append('ansi_fg_%d' % state['fg'])
   elif state['dim']:
      tags.append('ansi_dim')
   elif base_tag:
      tags.append(base_tag)
   if state['bold']:
      tags.append('ansi_bold')
   return tuple(tags)


def _parse_ansi(text, base_tag):
   """Split text containing ANSI SGR codes into (run_text, tag_tuple) pairs.
   Pure (no Tk) so it can be unit-tested headlessly."""
   runs  = []
   state = {'fg': None, 'bold': False, 'dim': False}
   pos   = 0
   for m in _SGR_RE.finditer(text):
      if m.start() > pos:
         runs.append((text[pos:m.start()], _state_tags(state, base_tag)))
      _apply_sgr(state, m.group(1))
      pos = m.end()
   if pos < len(text):
      runs.append((text[pos:], _state_tags(state, base_tag)))
   return runs


class ReplPane(tk.Frame):
   def __init__(self, parent, bridge, get_cwd=None, get_testdir=None,
                get_compliancedir=None, get_interp_cmd=None,
                calibrate_script=None, get_suite_selection=None,
                save_suite_selection=None, **kwargs):
      super().__init__(parent, **kwargs)
      self._bridge             = bridge
      self._get_cwd            = get_cwd or os.getcwd
      self._get_testdir        = get_testdir
      self._get_compliancedir  = get_compliancedir
      self._get_interp_cmd     = get_interp_cmd      # () -> current interpreter cmd list
      self._calibrate_script   = calibrate_script    # path to calibrate_tco.ps1
      self._get_suite_selection  = get_suite_selection   # () -> {name: bool}
      self._save_suite_selection = save_suite_selection  # (dict) -> persist
      self._history    = []
      self._lines      = []
      self._busy       = False
      self._debug_mode = False
      self._show_test_tools = True   # set False (or via set_test_tools_visible) for a release build
      # Test Suites... sequencer: a queue of zero-arg step callables run one at
      # a time, each advanced by the next 'ready' from the interpreter.
      self._suite_queue    = []
      self._running_suites = False

      self._load_history()
      self._build()
      self._bind_keys()
      # Banner and first prompt arrive from bridge via queue; no _show_prompt() here.
      self.after(50, self._poll)

   # ---- construction -----------------------------------------------------

   def _build(self):
      self._build_toolbar()
      self._build_text()

   def _build_toolbar(self):
      bar = tk.Frame(self, bg='#2d2d2d', pady=3)
      bar.pack(side=tk.TOP, fill=tk.X)

      btn = dict(
         bg='#3c3c3c', fg='#d4d4d4',
         activebackground='#505050', activeforeground='#ffffff',
         relief=tk.FLAT, padx=10, pady=3, cursor='hand2',
      )

      # ---- right-side interpreter-session controls (always visible).  Packed
      #      first and in reverse visual order, so this yields:  Load... Reboot Stop
      #      Stop stays pinned to the far-right corner. -----------------------
      stop_cfg = dict(btn)
      stop_cfg.update(bg='#6b1f1f', activebackground='#8b2f2f',
                      disabledforeground='#555555', state=tk.DISABLED)
      self._stop_btn = tk.Button(bar, text='Stop', command=self._cmd_stop, **stop_cfg)
      self._stop_btn.pack(side=tk.RIGHT, padx=(2, 6))

      tk.Button(bar, text='Reboot',  command=self._cmd_reboot, **btn).pack(side=tk.RIGHT, padx=2)
      tk.Button(bar, text='Load...', command=self._cmd_load,   **btn).pack(side=tk.RIGHT, padx=2)

      # ---- left-side screen control (always visible) -------------------------
      tk.Button(bar, text='Clear', command=self._cmd_clear, **btn).pack(side=tk.LEFT, padx=(6, 2))
      tk.Frame(bar, width=1, bg='#555555').pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=2)

      # ---- test group: one contiguous frame so it can be hidden as a unit --
      #      swapped out in debug mode, and easy to drop in a release build
      #      (see set_test_tools_visible).  Two buttons:  Test...  Test Suites...
      self._test_bar = tk.Frame(bar, bg='#2d2d2d')
      self._test_bar.pack(side=tk.LEFT, fill=tk.Y)

      tk.Button(self._test_bar, text='Test...', command=self._cmd_tests, **btn).pack(side=tk.LEFT, padx=2)
      suites_cfg = dict(btn)
      suites_cfg.update(bg='#1f4e2b', activebackground='#2f6e3b')
      tk.Button(self._test_bar, text='Test Suites...',
                command=self._cmd_test_suites, **suites_cfg).pack(side=tk.LEFT, padx=2)

      # ---- debug-mode button group (hidden until debugger is active) ---------
      self._debug_bar = tk.Frame(bar, bg='#2d2d2d')
      # not packed yet

      dbg = dict(btn)
      tk.Button(self._debug_bar, text='Into',     command=self._cmd_dbg_into,     **dbg).pack(side=tk.LEFT, padx=(6, 2))
      tk.Button(self._debug_bar, text='Over',     command=self._cmd_dbg_over,     **dbg).pack(side=tk.LEFT, padx=2)
      tk.Button(self._debug_bar, text='Out',      command=self._cmd_dbg_out,      **dbg).pack(side=tk.LEFT, padx=2)
      tk.Button(self._debug_bar, text='Continue', command=self._cmd_dbg_continue, **dbg).pack(side=tk.LEFT, padx=2)
      tk.Frame(self._debug_bar, width=1, bg='#555555').pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=2)
      abort_cfg = dict(btn)
      abort_cfg.update(bg='#6b1f1f', activebackground='#8b2f2f')
      tk.Button(self._debug_bar, text='Abort', command=self._cmd_dbg_abort, **abort_cfg).pack(side=tk.LEFT, padx=2)

   def _build_text(self):
      mono = tkfont.Font(family='Courier New', size=10)
      frame = tk.Frame(self)
      frame.pack(fill=tk.BOTH, expand=True)

      self._text = tk.Text(
         frame,
         font=mono,
         wrap=tk.WORD,
         undo=False,
         bg='#1e1e1e',
         fg='#d4d4d4',
         insertbackground='#d4d4d4',
         selectbackground='#264f78',
         relief=tk.FLAT,
         padx=6,
         pady=6,
      )
      sb = tk.Scrollbar(frame, command=self._text.yview)
      self._text.configure(yscrollcommand=sb.set)
      sb.pack(side=tk.RIGHT, fill=tk.Y)
      self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

      self._text.tag_configure(TAG_PROMPT, foreground='#569cd6')
      self._text.tag_configure(TAG_OUTPUT, foreground='#d4d4d4')
      self._text.tag_configure(TAG_RESULT, foreground='#4ec9b0')
      self._text.tag_configure(TAG_ERROR,  foreground='#f44747')
      self._text.tag_configure(TAG_INPUT,  foreground='#d4d4d4')
      self._text.tag_configure(TAG_BANNER, foreground='#888888',
                                font=tkfont.Font(family='Courier New', size=10))
      self._text.tag_configure(TAG_PAREN,  background='#3c3c00')
      self._configure_ansi_tags()

   def _configure_ansi_tags(self):
      """Configure the Tk tags used to render ANSI SGR color runs.  Created
      after the semantic tags so they take precedence when both could apply."""
      for code, hexcol in _ANSI_FG.items():
         self._text.tag_configure('ansi_fg_%d' % code, foreground=hexcol)
      self._text.tag_configure('ansi_dim', foreground='#9a9a9a')
      self._text.tag_configure('ansi_bold',
                               font=tkfont.Font(family='Courier New', size=10,
                                                weight='bold'))

   def _bind_keys(self):
      t = self._text
      t.bind('<Return>',      self._on_return)
      t.bind('<BackSpace>',   self._on_backspace)
      t.bind('<KeyRelease>',  self._on_paren_check)
      t.bind('<ButtonRelease-1>', self._on_paren_check)
      t.bind('<Delete>',    self._on_delete)
      t.bind('<Home>',      self._on_home)
      t.bind('<Key>',       self._on_key)
      t.bind('<<Paste>>',   self._on_paste)

   # ---- persistent history -----------------------------------------------

   def _load_history(self):
      try:
         with open(_HIST_FILE, 'r', encoding='utf-8') as f:
            raw = f.read()
         entries = raw.split('\x00')
         self._history = [e for e in entries if e.strip()]
      except FileNotFoundError:
         pass

   def save_history(self):
      tail = self._history[-_HIST_MAX:]
      try:
         with open(_HIST_FILE, 'w', encoding='utf-8') as f:
            f.write('\x00'.join(tail))
      except OSError:
         pass

   # ---- prompt / output --------------------------------------------------

   def _show_prompt(self):
      if self._lines:
         p      = CONT_PROMPT
         indent = Listener._compute_indent(self._lines)
      elif self._debug_mode:
         p      = DEBUG_PROMPT
         indent = ''
      else:
         p      = PROMPT
         indent = ''
      self._append(p, TAG_PROMPT)
      self._text.mark_set('input_start', 'end-1c')
      self._text.mark_gravity('input_start', tk.LEFT)
      if indent:
         self._append(indent, TAG_INPUT)
      self._text.see(tk.END)

   def _append(self, text, tag=None):
      if '\x1b' in text:
         # Text carries ANSI SGR codes (interpreter color); render per-run.
         for run, tags in _parse_ansi(text, tag):
            if run:
               self._text.insert(tk.END, run, tags)
      else:
         self._text.insert(tk.END, text, (tag,) if tag else ())
      self._text.see(tk.END)

   # ---- busy state -------------------------------------------------------

   def _set_busy(self, busy):
      self._busy = busy
      state = tk.NORMAL if busy else tk.DISABLED
      self._stop_btn.configure(state=state)

   # ---- queue polling ----------------------------------------------------

   def _poll(self):
      try:
         while True:
            msg  = self._bridge.result_queue.get_nowait()
            kind = msg[0]
            if kind == 'banner':
               self._append(msg[1], TAG_BANNER)
            elif kind == 'output':
               self._append(msg[1], TAG_OUTPUT)
            elif kind == 'result':
               self._append('==> ' + msg[1] + '\n', TAG_RESULT)
            elif kind == 'error':
               for line in msg[1].splitlines():
                  self._append('%%% ' + line + '\n', TAG_ERROR)
            elif kind == 'ready':
               debug = (len(msg) > 1 and msg[1].strip() == 'debug>')
               if debug != self._debug_mode:
                  self._debug_mode = debug
                  self._swap_toolbar()
               self._set_busy(False)
               self._append('\n', TAG_OUTPUT)
               self._show_prompt()
               # Advance a Test Suites... sequence: the step just finished, so
               # start the next one (or finish if the queue is empty).
               if self._running_suites:
                  self._advance_suite_queue()
            elif kind == 'exited':
               # The interpreter died (e.g. (exit)/]quit, or a crash).  Respawn
               # silently -- the fresh interpreter's boot banner is the signal.
               self._lines = []
               # A crash mid-sequence (e.g. the slow compliance run overflowing
               # on a TCO regression) aborts the remaining suites.
               if self._running_suites:
                  self._running_suites = False
                  self._suite_queue = []
                  self._append('; interpreter exited during a suite run; '
                               'sequence aborted.\n', TAG_ERROR)
               if self._debug_mode:
                  self._debug_mode = False
                  self._swap_toolbar()
               if self._bridge.restart():
                  # Stay busy until the respawned interpreter's boot 'ready'
                  # arrives and re-enables input with a clean prompt.
                  self._set_busy(True)
               else:
                  self._set_busy(False)
                  self._append('\n; interpreter keeps exiting on startup -- '
                               'press Reboot to retry.\n', TAG_ERROR)
                  self._show_prompt()
      except Exception:
         pass
      self.after(50, self._poll)

   # ---- toolbar commands -------------------------------------------------

   def _cmd_reboot(self):
      parent = self.winfo_toplevel()
      dlg = tk.Toplevel(parent)
      dlg.title('Reboot interpreter')
      dlg.resizable(False, False)
      dlg.configure(bg='#2d2d2d')
      dlg.transient(parent)

      tk.Label(dlg,
               text='Reboot the interpreter?\nAll current bindings will be lost.',
               bg='#2d2d2d', fg='#d4d4d4',
               padx=24, pady=16, justify=tk.LEFT).pack()

      btn_row = tk.Frame(dlg, bg='#2d2d2d')
      btn_row.pack(pady=(0, 14))

      confirmed = [False]

      def _do_reboot():
         confirmed[0] = True
         dlg.destroy()

      tk.Button(btn_row, text='Reboot', command=_do_reboot,
                bg='#6b1f1f', fg='#d4d4d4',
                activebackground='#8b2f2f', activeforeground='#ffffff',
                relief=tk.FLAT, padx=14, pady=4, cursor='hand2',
                ).pack(side=tk.LEFT, padx=6)
      tk.Button(btn_row, text='Cancel', command=dlg.destroy,
                bg='#3c3c3c', fg='#d4d4d4',
                activebackground='#505050', activeforeground='#ffffff',
                relief=tk.FLAT, padx=14, pady=4, cursor='hand2',
                ).pack(side=tk.LEFT, padx=6)

      dlg.update_idletasks()
      w  = dlg.winfo_width()
      h  = dlg.winfo_height()
      x  = parent.winfo_x() + (parent.winfo_width()  - w) // 2
      y  = parent.winfo_y() + (parent.winfo_height() - h) // 2
      dlg.geometry('+' + str(x) + '+' + str(y))
      dlg.grab_set()
      dlg.wait_window()

      if confirmed[0]:
         self._lines      = []
         if self._debug_mode:
            self._debug_mode = False
            self._swap_toolbar()
         self._set_busy(False)
         self._bridge.reboot()

   def _cmd_stop(self):
      # Stop aborts a running Test Suites... sequence as well as interrupting
      # the current evaluation.  (A calibration subprocess, if mid-run, is not
      # killed here; clearing the flag stops the sequence from continuing.)
      if self._running_suites:
         self._running_suites = False
         self._suite_queue = []
         self._append('\n; suite sequence aborted.\n', TAG_ERROR)
      self._bridge.stop()

   def _cmd_clear(self):
      self._text.delete('1.0', tk.END)
      self._lines = []
      self._show_prompt()

   def _swap_toolbar(self):
      if self._debug_mode:
         self._test_bar.pack_forget()
         self._debug_bar.pack(side=tk.LEFT, fill=tk.Y)
      else:
         self._debug_bar.pack_forget()
         if self._show_test_tools:
            self._test_bar.pack(side=tk.LEFT, fill=tk.Y)

   def set_test_tools_visible(self, visible):
      """Show or hide the Test.../Feature/Compliance/Regressions group as a
      unit (e.g. hide it in a release build).  No effect while the debugger
      bar is showing -- the test group is already swapped out then."""
      self._show_test_tools = visible
      if self._debug_mode:
         return
      if visible:
         self._test_bar.pack(side=tk.LEFT, fill=tk.Y)
      else:
         self._test_bar.pack_forget()

   def _cmd_dbg_into(self):
      self._send_debug_cmd('s')

   def _cmd_dbg_over(self):
      self._send_debug_cmd('n')

   def _cmd_dbg_out(self):
      self._send_debug_cmd('o')

   def _cmd_dbg_continue(self):
      self._send_debug_cmd('c')

   def _cmd_dbg_abort(self):
      self._send_debug_cmd('q')

   def _send_debug_cmd(self, cmd):
      if self._busy:
         return
      self._append(cmd + '\n', TAG_INPUT)
      self._set_busy(True)
      self._bridge.submit(cmd)

   def _cmd_load(self):
      path = filedialog.askopenfilename(
         title='Load Scheme file into interpreter',
         filetypes=[('Scheme files', '*.scm *.ss *.rkt'), ('All files', '*.*')],
      )
      if not path:
         return
      self._lines = []
      label = '(load "' + os.path.basename(path) + '")'
      self._text.delete('input_start', 'end-1c')
      self._append(label + '\n', TAG_INPUT)
      self._set_busy(True)
      self._bridge.submit_file(path)

   def _cmd_tests(self):
      path = filedialog.askopenfilename(
         title='Run test log',
         initialdir='testing',
         filetypes=[('Test logs', '*.log'), ('All files', '*.*')],
      )
      if path:
         self.inject_source(']feature ' + path)

   # ---- Test Suites... dialog + sequencer --------------------------------

   _SUITE_ORDER = ('Feature', 'Compliance (quick)',
                   'Compliance (slow)', 'Regressions')

   def _cmd_test_suites(self):
      if self._running_suites:
         return
      parent = self.winfo_toplevel()
      dlg = tk.Toplevel(parent)
      dlg.title('Run test suites')
      dlg.resizable(False, False)
      dlg.configure(bg='#2d2d2d')
      dlg.transient(parent)

      tk.Label(dlg, text='Run these suites in order:',
               bg='#2d2d2d', fg='#d4d4d4', padx=24,
               anchor=tk.W, justify=tk.LEFT).pack(fill=tk.X, pady=(16, 8))

      defaults = {'Feature': True, 'Compliance (quick)': True,
                  'Compliance (slow)': False, 'Regressions': True}
      # Restore the last-saved checkbox configuration (saved on Run); fall back
      # to defaults for any suite not present in the saved selection.
      saved = {}
      if self._get_suite_selection:
         try:
            saved = self._get_suite_selection() or {}
         except Exception:
            saved = {}
      if not isinstance(saved, dict):
         saved = {}
      checks = {}
      box = tk.Frame(dlg, bg='#2d2d2d')
      box.pack(fill=tk.X, padx=28)
      for name in ReplPane._SUITE_ORDER:
         v = tk.BooleanVar(value=bool(saved.get(name, defaults[name])))
         checks[name] = v
         tk.Checkbutton(box, text=name, variable=v,
                        bg='#2d2d2d', fg='#d4d4d4',
                        activebackground='#2d2d2d', activeforeground='#ffffff',
                        selectcolor='#1e1e1e', highlightthickness=0,
                        anchor=tk.W, padx=4, cursor='hand2').pack(fill=tk.X, anchor=tk.W)

      tk.Label(dlg,
               text="'Compliance (quick)' runs -I:100k.  'Compliance (slow)'\n"
                    "calibrates this machine's TCO overflow threshold (slow,\n"
                    'memory-heavy) then runs full compliance above it.',
               bg='#2d2d2d', fg='#888888', padx=24,
               anchor=tk.W, justify=tk.LEFT).pack(fill=tk.X, pady=(8, 6))

      btn_row = tk.Frame(dlg, bg='#2d2d2d')
      btn_row.pack(pady=(4, 14))

      def _do_run():
         selected = [n for n in ReplPane._SUITE_ORDER if checks[n].get()]
         # Persist the current configuration on Run only (Cancel has no effect).
         if self._save_suite_selection:
            self._save_suite_selection(
               {n: bool(checks[n].get()) for n in ReplPane._SUITE_ORDER})
         dlg.destroy()
         if selected:
            self._run_suite_sequence(selected)

      tk.Button(btn_row, text='Run', command=_do_run,
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
      x = parent.winfo_x() + (parent.winfo_width() - w) // 2
      y = parent.winfo_y() + (parent.winfo_height() - h) // 2
      dlg.geometry('+' + str(x) + '+' + str(y))
      dlg.grab_set()

   # ---- suite command builders -------------------------------------------

   def _feature_cmd(self):
      testdir = self._get_testdir() if self._get_testdir else None
      return ']feature ' + testdir if testdir else ']feature'

   def _compliance_cmd(self, iters):
      d = self._get_compliancedir() if self._get_compliancedir else None
      switch = ' -I:' + str(iters)
      return (']compliance ' + d + switch) if d else (']compliance' + switch)

   # ---- sequencer: run checked suites one at a time ----------------------

   def _run_suite_sequence(self, selected):
      if self._busy or self._running_suites:
         self._append('\n; a test run is already in progress.\n', TAG_ERROR)
         return
      self._suite_queue = [self._make_suite_step(n) for n in selected]
      self._running_suites = True
      self._append('\n; running test suites: ' + ', '.join(selected) + '\n',
                   TAG_OUTPUT)
      self._advance_suite_queue()

   def _advance_suite_queue(self):
      if not self._suite_queue:
         if self._running_suites:
            self._running_suites = False
            self._append('\n; all selected test suites complete.\n', TAG_OUTPUT)
         return
      step = self._suite_queue.pop(0)
      step()

   def _make_suite_step(self, name):
      if name == 'Feature':
         return lambda: self.inject_source(self._feature_cmd())
      if name == 'Compliance (quick)':
         return lambda: self.inject_source(self._compliance_cmd('100k'))
      if name == 'Compliance (slow)':
         return self._run_compliance_slow
      if name == 'Regressions':
         return lambda: self.inject_source(']regression')
      return self._advance_suite_queue   # unknown -> skip

   # ---- Compliance (slow): calibrate, then run above the threshold -------

   def _run_compliance_slow(self):
      if not self._calibrate_script or not os.path.isfile(self._calibrate_script) \
            or not self._get_interp_cmd:
         self._append('; Compliance (slow): calibrator unavailable; skipping.\n',
                      TAG_ERROR)
         self._advance_suite_queue()
         return
      self._set_busy(True)
      self._append(
         "; Compliance (slow): calibrating this platform's TCO overflow\n"
         ';   threshold (slow, memory-heavy)...\n', TAG_OUTPUT)
      self._start_calibration(self._on_calibration_done)

   def _start_calibration(self, on_done):
      cmd = self._get_interp_cmd()
      exe = cmd[0]
      pre_args = ' '.join(cmd[1:]) if len(cmd) > 1 else ''
      script = self._calibrate_script

      def worker():
         threshold = [0]
         proc = None
         for cand in ('pwsh', 'powershell'):
            try:
               proc = subprocess.Popen(
                  [cand, '-NoProfile', '-File', script,
                   '-InterpExe', exe, '-InterpArgs', pre_args],
                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  text=True, bufsize=1, cwd=os.path.dirname(script))
               break
            except FileNotFoundError:
               proc = None
               continue
         if proc is None:
            self.after(0, lambda: self._append(
               '; PowerShell (pwsh/powershell) not found.\n', TAG_ERROR))
            self.after(0, lambda: on_done(None))
            return
         try:
            for line in proc.stdout:
               s = line.rstrip('\n')
               m = re.search(r'CALIBRATE_THRESHOLD\s+(\d+)', s)
               if m:
                  threshold[0] = int(m.group(1))
               else:
                  self.after(0, lambda t=s: self._append('  ' + t + '\n', TAG_OUTPUT))
            proc.wait()
         except Exception as e:
            self.after(0, lambda e=e: self._append(
               '; calibration error: ' + str(e) + '\n', TAG_ERROR))
            self.after(0, lambda: on_done(None))
            return
         n = threshold[0]
         if n <= 0:
            self.after(0, lambda: on_done(None))
         else:
            iters = (n // 1000000 + 1) * 1000000
            self.after(0, lambda v=iters: on_done(v))

      threading.Thread(target=worker, daemon=True).start()

   def _on_calibration_done(self, iters):
      if not self._running_suites:
         # Sequence was aborted (Stop) while calibrating.
         self._set_busy(False)
         self._show_prompt()
         return
      if iters is None:
         self._append('; calibration failed; skipping the slow compliance run.\n',
                      TAG_ERROR)
         self._set_busy(False)
         self._show_prompt()
         self._advance_suite_queue()
         return
      self._append('; overflow threshold calibrated; running full compliance at '
                   '-I:%d.\n' % iters, TAG_OUTPUT)
      cmd = self._compliance_cmd(iters)
      self._lines = []
      self._append(cmd + '\n', TAG_INPUT)
      # busy is already True from _run_compliance_slow; the eventual 'ready'
      # advances the queue to the next suite.
      self._bridge.submit(cmd)

   # ---- key handlers -----------------------------------------------------

   def _before_input_start(self):
      return self._text.compare(tk.INSERT, '<', 'input_start')

   def _on_key(self, event):
      if self._busy:
         return 'break'
      ch = event.char
      if not ch or event.keysym in (
            'Return', 'Up', 'Down', 'BackSpace', 'Delete',
            'Home', 'End', 'Left', 'Right', 'Prior', 'Next',
            'Control_L', 'Control_R', 'Alt_L', 'Alt_R',
            'Shift_L', 'Shift_R', 'Escape', 'Tab',
            'F1','F2','F3','F4','F5','F6','F7','F8','F9','F10','F11','F12',
         ):
         return
      if event.state & 0x4:   # Ctrl held - allow copy/paste shortcuts
         return
      if self._before_input_start():
         self._text.mark_set(tk.INSERT, tk.END)
      return

   def _on_backspace(self, event):
      if self._busy:
         return 'break'
      if self._before_input_start():
         return 'break'
      if self._text.compare(tk.INSERT, '<=', 'input_start'):
         return 'break'
      return

   def _on_delete(self, event):
      if self._busy:
         return 'break'
      if self._before_input_start():
         return 'break'
      return

   def _on_home(self, event):
      self._text.mark_set(tk.INSERT, 'input_start')
      return 'break'

   def _on_paste(self, event):
      if self._busy:
         return 'break'
      if self._before_input_start():
         self._text.mark_set(tk.INSERT, tk.END)
      return

   def _on_return(self, event):
      if self._busy:
         return 'break'

      # Cursor above input_start: copy that expression to the prompt for editing
      if self._before_input_start():
         expr = self._extract_expr_at_cursor()
         if expr:
            self._replace_input(expr)
         return 'break'

      line = self._text.get('input_start', 'end-1c')

      # Super-bracket: trailing ']' closes all open parens
      if line.endswith(']'):
         tentative    = line[:-1]
         combined_try = '\n'.join(self._lines + [tentative])
         ps_try       = paren_state(combined_try)
         innermost_bracket = (len(ps_try.stack) > 0
                              and ps_try.stack[len(ps_try.stack) - 1] == '[')
         if ps_try.depth > 0 and not ps_try.in_string and not innermost_bracket:
            line = tentative + ')' * ps_try.depth
            self._text.delete('input_start', 'end-1c')
            self._text.insert('input_start', line, (TAG_INPUT,))

      self._append('\n', TAG_INPUT)
      self._lines.append(line)
      combined = '\n'.join(self._lines)
      ps = paren_state(combined)

      if ps.depth > 0 or ps.in_string:
         self._show_prompt()
         return 'break'

      source = combined.strip()
      self._lines = []
      if source:
         self._history.append(source)
         self._set_busy(True)
         self._bridge.submit(source)
      else:
         self._show_prompt()
      return 'break'

   def _on_paren_check(self, event=None):
      self._highlight_matching_paren()

   def _highlight_matching_paren(self):
      t = self._text
      t.tag_remove(TAG_PAREN, '1.0', tk.END)

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

   def _extract_expr_at_cursor(self):
      """Return the full expression block the cursor is sitting on,
      with prompt prefixes stripped.  Returns '' if the cursor is not
      on an expression line (e.g. output, result, or error line)."""
      cursor_line = int(self._text.index(tk.INSERT).split('.')[0])

      # Walk backwards to the >>> line that opens this block
      ln = cursor_line
      while ln >= 1:
         content = self._text.get(str(ln) + '.0', str(ln) + '.end')
         if content.startswith(PROMPT):
            break
         if content.startswith(CONT_PROMPT):
            ln -= 1
            continue
         return ''   # cursor is on a non-expression line
      if ln < 1:
         return ''

      # Collect the >>> line and any following ... lines
      lines = []
      total = int(self._text.index(tk.END).split('.')[0])
      i = ln
      while i <= total:
         content = self._text.get(str(i) + '.0', str(i) + '.end')
         if i == ln:
            lines.append(content[len(PROMPT):])
         elif content.startswith(CONT_PROMPT):
            lines.append(content[len(CONT_PROMPT):])
         else:
            break
         i += 1

      return '\n'.join(lines)

   def _replace_input(self, text):
      self._text.delete('input_start', 'end-1c')
      self._text.insert('input_start', text, (TAG_INPUT,))
      self._text.mark_set(tk.INSERT, tk.END)
      self._text.see(tk.END)

   # ---- public helpers ---------------------------------------------------

   def set_bridge(self, bridge):
      """Replace the underlying bridge (called when switching interpreters)."""
      self._bridge = bridge
      self._lines = []
      if self._debug_mode:
         self._debug_mode = False
         self._swap_toolbar()
      self._set_busy(False)
      self._text.delete('1.0', tk.END)

   def inject_source(self, source):
      """Submit source as if typed at the prompt (called by editor Run button)."""
      if self._busy:
         return
      self._lines = []
      self._text.delete('input_start', 'end-1c')
      self._text.insert('input_start', source, (TAG_INPUT,))
      self._append('\n', TAG_INPUT)
      if source.strip():
         self._history.append(source)
         self._set_busy(True)
         self._bridge.submit(source)
      else:
         self._show_prompt()
