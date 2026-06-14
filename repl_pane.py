"""ReplPane: a terminal-style REPL widget backed by a single tk.Text.

The full buffer is always editable, but key bindings enforce the rule that
destructive keystrokes (BackSpace, Delete, printable characters) cannot
modify text before the 'input_start' mark.  Cursor movement keys work freely
throughout.  To recall a previous expression, move the caret onto it and press
Enter: it is copied down to the live prompt for editing and resubmission (see
_extract_expr_at_cursor); this works off the on-screen buffer.
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
   def __init__(self, parent, bridge, get_cwd=None, get_interp_cmd=None,
                get_suite_selection=None, save_suite_selection=None,
                font_family='Courier New', font_size=10, **kwargs):
      super().__init__(parent, **kwargs)
      self._bridge             = bridge
      self._get_cwd            = get_cwd or os.getcwd
      self._get_interp_cmd     = get_interp_cmd      # () -> current interpreter cmd list
      self._get_suite_selection  = get_suite_selection   # () -> {name: bool}
      self._save_suite_selection = save_suite_selection  # (dict) -> persist
      self._lines      = []
      self._busy       = False
      self._debug_mode = False
      self._show_test_tools = True   # set False (or via set_test_tools_visible) for a release build

      # The REPL font is owned here (not a per-call literal) so set_font can
      # restyle the live widget; the bold variant tracks it for ANSI bold runs.
      self._font      = tkfont.Font(family=font_family, size=font_size)
      self._bold_font = tkfont.Font(family=font_family, size=font_size,
                                    weight='bold')

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
      # Undercarriage tests = cppscheme2's C++ gc_test binary; only meaningful
      # for cppscheme2 (the other interpreters have no custom GC / no such exe),
      # so the button is enabled only while cppscheme2 is the active interpreter.
      under_cfg = dict(btn)
      under_cfg.update(bg='#3a2f5a', activebackground='#4a3f6e',
                       disabledforeground='#666666')
      self._undercarriage_btn = tk.Button(
         self._test_bar, text='Undercarriage',
         command=self._cmd_undercarriage, **under_cfg)
      self._undercarriage_btn.pack(side=tk.LEFT, padx=2)
      self._refresh_undercarriage_btn()

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
      frame = tk.Frame(self)
      frame.pack(fill=tk.BOTH, expand=True)

      self._text = tk.Text(
         frame,
         font=self._font,
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
      # No explicit font -> inherits the widget font, so it tracks set_font.
      self._text.tag_configure(TAG_BANNER, foreground='#888888')
      self._text.tag_configure(TAG_PAREN,  background='#3c3c00')
      self._configure_ansi_tags()

   def _configure_ansi_tags(self):
      """Configure the Tk tags used to render ANSI SGR color runs.  Created
      after the semantic tags so they take precedence when both could apply."""
      for code, hexcol in _ANSI_FG.items():
         self._text.tag_configure('ansi_fg_%d' % code, foreground=hexcol)
      self._text.tag_configure('ansi_dim', foreground='#9a9a9a')
      self._text.tag_configure('ansi_bold', font=self._bold_font)

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
            elif kind == 'exited':
               # The interpreter died (e.g. (exit)/]quit, or a crash).  Respawn
               # silently -- the fresh interpreter's boot banner is the signal.
               self._lines = []
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
      # Stop interrupts the current evaluation -- including a running ]suites
      # batch, which the interpreter drives as a single command.
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

   def set_font(self, family, size):
      """Restyle the REPL text (and its bold ANSI variant) live."""
      self._font.configure(family=family, size=size)
      self._bold_font.configure(family=family, size=size)

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

   def _slow_compliance_applicable(self):
      # 'Compliance (slow)' is a high-N memory soak whose real payoff is
      # stressing cppscheme2's custom generational GC.  pyScheme (and the older
      # interpreters) have no custom GC -- and bounded-space TCO is already
      # proven at small N by 3.05's %continuation-depth checks, which run in the
      # quick suite too -- so the slow run only makes sense for cppscheme2.
      if not self._get_interp_cmd:
         return False
      try:
         cmd = self._get_interp_cmd() or []
      except Exception:
         return False
      return any('cppscheme2' in str(p).lower() for p in cmd)

   # ---- Undercarriage tests (cppscheme2's C++ gc_test binary) ------------

   def _undercarriage_exe(self):
      """Path to cppscheme2's gc_test.exe when cppscheme2 is the active
      interpreter, else None.  gc_test sits beside cppscheme2.exe in
      build/Release.  Returns the path even if the file is missing (the click
      handler reports that, with a build hint) -- the gate is the interpreter,
      not whether the binary has been built."""
      if not self._get_interp_cmd:
         return None
      try:
         cmd = self._get_interp_cmd() or []
      except Exception:
         return None
      if not cmd or not any('cppscheme2' in str(p).lower() for p in cmd):
         return None
      return os.path.join(os.path.dirname(str(cmd[0])), 'gc_test.exe')

   def _refresh_undercarriage_btn(self):
      """Enable the Undercarriage button only while cppscheme2 is active."""
      btn = getattr(self, '_undercarriage_btn', None)
      if btn is None:
         return
      state = tk.NORMAL if self._undercarriage_exe() else tk.DISABLED
      btn.configure(state=state)

   def _cmd_undercarriage(self):
      if self._busy:
         return
      exe = self._undercarriage_exe()
      if exe is None:
         return  # not cppscheme2 -- the button is disabled anyway
      self._append('\n', TAG_OUTPUT)
      if not os.path.isfile(exe):
         self._append('Undercarriage tests: gc_test.exe not found at\n  ' + exe
                      + '\nBuild it:  cmake --build build --config Release '
                        '--target gc_test\n', TAG_ERROR)
         self._show_prompt()
         return
      self._append('Running undercarriage tests (gc_test)...\n', TAG_BANNER)
      self._set_busy(True)
      bridge = self._bridge   # results flow through its queue, drained by _poll
      threading.Thread(target=self._undercarriage_worker,
                       args=(exe, bridge), daemon=True).start()

   def _undercarriage_worker(self, exe, bridge):
      """Run gc_test.exe to completion in a background thread, streaming its
      output into the REPL via the bridge's result_queue (drained by _poll on the
      main thread).  A final 'ready' restores the prompt and clears the busy
      state -- the same path a normal evaluation takes."""
      q = bridge.result_queue
      try:
         proc = subprocess.Popen(
            [exe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=os.path.dirname(exe), text=True, bufsize=1)
         for line in proc.stdout:
            q.put(('output', line))
         proc.wait()
         if proc.returncode == 0:
            q.put(('output', 'Undercarriage tests PASSED.\n'))
         else:
            q.put(('error', 'Undercarriage tests FAILED (exit %d).'
                   % proc.returncode))
      except OSError as e:
         q.put(('error', 'Undercarriage tests: %s' % e))
      q.put(('ready',))

   def _cmd_test_suites(self):
      if self._busy:
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
      # 'Compliance (slow)' only makes sense for cppscheme2; disable it
      # (forced unchecked) for the other interpreters.
      slow_ok = self._slow_compliance_applicable()
      checks = {}
      box = tk.Frame(dlg, bg='#2d2d2d')
      box.pack(fill=tk.X, padx=28)
      for name in ReplPane._SUITE_ORDER:
         enabled = not (name == 'Compliance (slow)' and not slow_ok)
         init = bool(saved.get(name, defaults[name])) if enabled else False
         v = tk.BooleanVar(value=init)
         checks[name] = v
         tk.Checkbutton(box, text=name, variable=v,
                        bg='#2d2d2d', fg='#d4d4d4',
                        activebackground='#2d2d2d', activeforeground='#ffffff',
                        selectcolor='#1e1e1e', highlightthickness=0,
                        disabledforeground='#666666',
                        state=(tk.NORMAL if enabled else tk.DISABLED),
                        anchor=tk.W, padx=4, cursor='hand2').pack(fill=tk.X, anchor=tk.W)

      tk.Label(dlg,
               text="'Compliance (quick)' runs -I:100k.  'Compliance (slow)'\n"
                    "calibrates this machine's TCO overflow threshold then soaks\n"
                    'above it -- a GC stress run, for cppscheme2 only.',
               bg='#2d2d2d', fg='#888888', padx=24,
               anchor=tk.W, justify=tk.LEFT).pack(fill=tk.X, pady=(8, 6))

      btn_row = tk.Frame(dlg, bg='#2d2d2d')
      btn_row.pack(pady=(4, 14))

      def _do_run():
         selected = [n for n in ReplPane._SUITE_ORDER if checks[n].get()]
         # Persist on Run only (Cancel has no effect).  When 'Compliance (slow)'
         # is disabled (non-cppscheme2), keep its previously-saved value rather
         # than clobbering it, so a cppscheme2 preference survives a detour.
         if self._save_suite_selection:
            config = {}
            for n in ReplPane._SUITE_ORDER:
               if n == 'Compliance (slow)' and not slow_ok:
                  config[n] = bool(saved.get(n, defaults[n]))
               else:
                  config[n] = bool(checks[n].get())
            self._save_suite_selection(config)
         dlg.destroy()
         if selected:
            self._run_selected_suites(selected)

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

   # ---- run the checked suites via the interpreter's ]suites command -----
   # The interpreter owns the sequencing now -- and, for compliance-slow, the
   # heap-OOM/TCO calibration that used to live here (it spawns child
   # interpreters internally).  So the whole selection runs as ONE ]suites
   # command; Cherry just maps the checked boxes to suite tokens.

   _SUITE_TOKEN = {
      'Feature':            'feature',
      'Compliance (quick)': 'compliance-quick',
      'Compliance (slow)':  'compliance-slow',
      'Regressions':        'regression',
   }

   def _run_selected_suites(self, selected):
      tokens = [ReplPane._SUITE_TOKEN[n] for n in selected
                if n in ReplPane._SUITE_TOKEN]
      # ]suites treats compliance-quick / compliance-slow as mutually exclusive;
      # if both boxes were somehow checked, run the stronger (slow) and drop
      # quick rather than tripping the interpreter's error.
      if 'compliance-slow' in tokens and 'compliance-quick' in tokens:
         tokens.remove('compliance-quick')
      if tokens:
         self.inject_source(']suites ' + ' '.join(tokens))

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
      """Return the full expression block the cursor is sitting in, with prompt
      prefixes stripped.  The caret can be anywhere inside the block -- on the
      >>> line, a ... continuation, or one of the command's output/result lines
      -- and we scan up to the >>> line that opens it.  Returns '' only if there
      is no >>> line at or above the cursor."""
      cursor_line = int(self._text.index(tk.INSERT).split('.')[0])

      # Scan up to the nearest >>> line at or above the cursor.
      ln = cursor_line
      while ln >= 1:
         content = self._text.get(str(ln) + '.0', str(ln) + '.end')
         if content.startswith(PROMPT):
            break
         ln -= 1
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
      self._refresh_undercarriage_btn()

   def inject_source(self, source):
      """Submit source as if typed at the prompt (called by editor Run button)."""
      if self._busy:
         return
      self._lines = []
      self._text.delete('input_start', 'end-1c')
      # Render multi-line input with '... ' continuation prefixes, exactly like
      # typed input, so the buffer stays consistent and expression-recall (which
      # keys off the >>> / ... prefixes to know which lines belong to the
      # expression) can reassemble the whole block, not just its first line.
      src_lines = source.split('\n')
      self._text.insert('input_start', src_lines[0], (TAG_INPUT,))
      for cont in src_lines[1:]:
         self._append('\n', TAG_INPUT)
         self._append(CONT_PROMPT, TAG_PROMPT)
         self._append(cont, TAG_INPUT)
      self._append('\n', TAG_INPUT)
      if source.strip():
         self._set_busy(True)
         self._bridge.submit(source)
      else:
         self._show_prompt()
