"""SubprocessBridge: drives any Lisp REPL via stdin/stdout.

The interpreter is spawned as a child process.  A reader thread accumulates
stdout one byte at a time and fires whenever the buffer ends with a known
prompt string.  No sentinel characters or protocol changes are required in
the interpreter -- the existing '>>> ' / '... ' / 'debug> ' prompts are
sufficient because the interpreter always prints one of them as the very
last thing before blocking for input.

Queue messages put onto result_queue (same protocol as InProcessBridge):
    ('output', str)   - plain output (display, banner text, etc.)
    ('result', str)   - return value line, '==> ' prefix already stripped
    ('error',  str)   - error line,  '%%% ' prefix already stripped
    ('ready',)        - interpreter is idle; GUI should re-enable input
    ('exited', code)  - the child process died (e.g. (exit)/]quit, or a
                        crash) without an intentional shutdown; the GUI
                        should restart() to recover.

Prompts that trigger 'ready':  '>>> '  'debug> '
Prompts that are suppressed:   '... '  (continuation -- ReplPane manages
                                        its own multi-line display)

To use with a different Lisp, pass cmd= and prompts= to the constructor.
"""

import os
import queue
import signal
import subprocess
import sys
import threading
import time


_DEFAULT_CMD         = [sys.executable, '-u', '-m', 'pyscheme']
_DEFAULT_READY       = {'>>> ', 'debug> '}
_DEFAULT_CONT        = {'... '}


class SubprocessBridge:
   def __init__(self, cmd=None, ready_prompts=None, cont_prompts=None, cwd=None):
      self._ready  = set(ready_prompts or _DEFAULT_READY)
      self._cont   = set(cont_prompts  or _DEFAULT_CONT)
      self._all    = self._ready | self._cont
      self._maxlen = max(len(p) for p in self._all)

      self.result_queue = queue.Queue()

      self._cmd = cmd or _DEFAULT_CMD
      self._cwd = cwd or os.getcwd()
      # Set True before an intentional kill (shutdown / interpreter switch) so
      # the reader's EOF does not look like a crash and trigger a respawn.
      self._closing        = False
      self._last_spawn     = 0.0
      self._rapid_restarts = 0

      self._spawn()

   def _spawn(self):
      """(Re)spawn the interpreter subprocess and its reader thread."""
      extra = {}
      if sys.platform == 'win32':
         extra['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

      self._proc = subprocess.Popen(
         self._cmd,
         stdin=subprocess.PIPE,
         stdout=subprocess.PIPE,
         stderr=subprocess.STDOUT,
         bufsize=0,
         cwd=self._cwd,
         **extra,
      )
      self._last_spawn = time.monotonic()
      self._closing    = False
      # Swallow the one-line state echo from the startup color toggle below,
      # and the extra prompt it triggers, so boot shows one clean prompt.
      self._swallow_color_echo = True
      self._swallow_next_ready = False
      self._thread = threading.Thread(target=self._reader, daemon=True)
      self._thread.start()
      # Ask the interpreter to emit ANSI color codes even though stdout is a
      # pipe (not a TTY); ReplPane renders them.  Interpreters that lack the
      # command simply report it as unknown -- harmless.
      self._write(']toggle-tty-color\n')

   # ---- public API -------------------------------------------------------

   def restart(self):
      """Respawn the interpreter after it exited (e.g. (exit)/]quit/crash).

      Returns False without respawning if the process died again within a
      second of the last spawn twice running -- a likely crash-on-boot loop
      the caller should surface rather than spin on."""
      if time.monotonic() - self._last_spawn < 1.0:
         self._rapid_restarts += 1
      else:
         self._rapid_restarts = 0
      if self._rapid_restarts >= 2:
         return False
      self._spawn()
      return True

   def submit(self, source):
      """Send a (possibly multi-line) expression to the interpreter."""
      # ]exit / ]quit would exit the interpreter to the shell -- which is
      # meaningless under cherry and would kill the subprocess.  Intercept the
      # bare commands and reboot the interpreter in place instead, so the user
      # gets a fresh session at the prompt rather than a dead REPL.
      if source.strip() in (']exit', ']quit'):
         self._write(']reboot\n')
         return
      for line in source.split('\n'):
         self._write(line + '\n')

   def submit_file(self, path):
      self._write(']readsrc ' + path + '\n')

   def submit_test(self, path):
      self._write(']feature ' + path + '\n')

   def submit_test_dir(self, _path):
      """Run the full feature suite.  Requires testing/ in the subprocess CWD."""
      self._write(']feature\n')

   def submit_compliance_dir(self, path):
      """Run the R7RS compliance suite from the given directory."""
      self._write(']compliance ' + path + '\n')

   def reboot(self):
      # If the process is alive, reboot it in place; if it already exited
      # (e.g. after a crash-loop), the user pressing Reboot is an explicit
      # retry -- clear the rapid-restart guard and respawn.
      if self._proc.poll() is None:
         self._write(']reboot\n')
      else:
         self._rapid_restarts = 0
         self._spawn()

   def stop(self):
      if self._proc.poll() is not None:
         return
      if sys.platform == 'win32':
         self._proc.send_signal(signal.CTRL_BREAK_EVENT)
      else:
         self._proc.send_signal(signal.SIGINT)

   def chdir(self, path):
      """Change the subprocess working directory."""
      self._write(']cd ' + path + '\n')

   def shutdown(self):
      """Terminate the child process cleanly."""
      self._closing = True
      try:
         self._write(']quit\n')
      except OSError:
         pass
      try:
         self._proc.wait(timeout=2)
      except subprocess.TimeoutExpired:
         self._proc.terminate()

   # ---- internal ---------------------------------------------------------

   def _write(self, text):
      try:
         self._proc.stdin.write(text.encode('utf-8'))
         self._proc.stdin.flush()
      except OSError:
         pass

   def _decode(self, b):
      return b.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')

   def _reader(self):
      buf = b''
      proc = self._proc
      while True:
         ch = proc.stdout.read(1)
         if not ch:
            # EOF: the child exited.  Unless we asked it to (shutdown /
            # interpreter switch), report it so the GUI can recover.
            if not self._closing:
               self.result_queue.put(('exited', proc.poll()))
            break
         buf += ch

         # Check whether the buffer ends with a known prompt.
         # Prompts contain no newline, so this and the newline branch
         # below are mutually exclusive.
         matched = None
         for p in self._all:
            if buf.endswith(p.encode('utf-8')):
               matched = p
               break

         if matched:
            text = self._decode(buf[:-len(matched)])
            self._emit_chunk(text)
            if matched in self._ready:
               # The startup color toggle produces an extra prompt; swallow it
               # (paired with the swallowed 'tty-color: on' echo) so boot shows
               # a single clean prompt.
               if self._swallow_next_ready:
                  self._swallow_next_ready = False
               else:
                  self.result_queue.put(('ready', matched))
            # cont prompts ('... '): suppressed
            buf = b''

         elif buf.endswith(b'\n'):
            # Complete line -- emit immediately so output streams in
            # rather than waiting for the final prompt.
            self._emit_chunk(self._decode(buf))
            buf = b''

   def _emit_chunk(self, text):
      """Parse a chunk of output and put typed messages on result_queue.

      Scans for '==> ' and '%%% ' markers that may appear mid-line (e.g.
      when display output has no trailing newline and the result is printed
      immediately after on the same line).
      """
      if not text:
         return
      # Drop the single 'tty-color: on' echo produced by the startup toggle,
      # and arrange to drop the extra prompt it triggers.
      if self._swallow_color_echo and text.strip() == 'tty-color: on':
         self._swallow_color_echo = False
         self._swallow_next_ready = True
         return
      remaining = text
      while remaining:
         rpos = remaining.find('==> ')
         epos = remaining.find('%%% ')

         if rpos == -1 and epos == -1:
            self.result_queue.put(('output', remaining))
            return

         if rpos == -1 or (epos != -1 and epos < rpos):
            marker = epos
            kind   = 'error'
         else:
            marker = rpos
            kind   = 'result'

         if marker > 0:
            self.result_queue.put(('output', remaining[:marker]))
         after = remaining[marker + 4:]
         nl    = after.find('\n')
         if nl == -1:
            self.result_queue.put((kind, after))
            return
         self.result_queue.put((kind, after[:nl]))
         remaining = after[nl + 1:]
