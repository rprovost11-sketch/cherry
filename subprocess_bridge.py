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

      extra = {}
      if sys.platform == 'win32':
         extra['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

      self._proc = subprocess.Popen(
         cmd or _DEFAULT_CMD,
         stdin=subprocess.PIPE,
         stdout=subprocess.PIPE,
         stderr=subprocess.STDOUT,
         bufsize=0,
         cwd=cwd or os.getcwd(),
         **extra,
      )
      self._thread = threading.Thread(target=self._reader, daemon=True)
      self._thread.start()

   # ---- public API -------------------------------------------------------

   def submit(self, source):
      """Send a (possibly multi-line) expression to the interpreter."""
      for line in source.split('\n'):
         self._write(line + '\n')

   def submit_file(self, path):
      self._write(']readsrc ' + path + '\n')

   def submit_test(self, path):
      self._write(']test ' + path + '\n')

   def submit_test_dir(self, _path):
      """Run the full test suite.  Requires testing/ in the subprocess CWD."""
      self._write(']test\n')

   def submit_compliance_dir(self, path):
      """Run the R7RS compliance suite from the given directory."""
      self._write(']compliance ' + path + '\n')

   def reboot(self):
      self._write(']reboot\n')

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
      while True:
         ch = self._proc.stdout.read(1)
         if not ch:
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
