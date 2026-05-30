"""InProcessBridge: runs the pyscheme Interpreter on a worker thread and
exposes a simple submit/poll interface to the GUI.

Queue message tuples sent to result_queue:
    ('banner', str)   - welcome / reboot banner
    ('output', str)   - side-effect output from display/write/newline
    ('result', str)   - pretty-printed return value  (==> ...)
    ('error',  str)   - formatted error message      (%%% ...)
    ('ready',)        - interpreter is idle; GUI should re-enable input

To swap for a subprocess or socket backend, replace this class with one
that offers the same submit() / result_queue / stop() interface.
"""

import ctypes
import io
import queue
import sys
import threading

from pyscheme             import __version__
from pyscheme.Interpreter import Interpreter
from pyscheme.Listener    import Listener
from pyscheme.Utils       import retrieveFileList
import pyscheme.Expander as _expander_mod


# ---- internal sentinels and helpers --------------------------------------

_REBOOT_SENTINEL = object()


class _FileItem:
   def __init__(self, path):
      self.path = path


class _TestItem:
   def __init__(self, path):
      self.path = path


class _TestDirItem:
   def __init__(self, path):
      self.path = path


class _ComplianceDirItem:
   def __init__(self, path):
      self.path = path


class _QueueStream:
   """File-like object that routes print() output to result_queue."""
   def __init__(self, q):
      self._q = q

   def write(self, text):
      if text:
         self._q.put(('output', text))

   def flush(self):
      pass

   def isatty(self):
      return False


class _QuietListener(Listener):
   """Listener subclass used only for test execution; suppresses the
   banner and readline init (both are inappropriate inside a thread)."""
   def _banner(self):
      pass

   def _init_readline(self):
      pass


# ---- bridge --------------------------------------------------------------


class InProcessBridge:
   def __init__(self):
      self._interp      = Interpreter()
      self.result_queue = queue.Queue()
      self._work_queue  = queue.Queue()
      self._worker_tid  = None
      self._thread      = threading.Thread(target=self._worker, daemon=True)
      self._thread.start()
      self._put_banner('startup')

   # ---- public API -------------------------------------------------------

   def submit(self, source):
      """Queue a source string for evaluation.  Returns immediately."""
      self._work_queue.put(source)

   def submit_file(self, path):
      """Queue a file for evaluation via evalFile()."""
      self._work_queue.put(_FileItem(path))

   def submit_test(self, path):
      """Queue a single .log test file for execution."""
      self._work_queue.put(_TestItem(path))

   def submit_test_dir(self, path):
      """Queue an entire directory of .log files for execution."""
      self._work_queue.put(_TestDirItem(path))

   def submit_compliance_dir(self, path):
      """Queue an R7RS compliance test directory for execution."""
      self._work_queue.put(_ComplianceDirItem(path))

   def reboot(self):
      """Reset the interpreter to a fresh environment."""
      self._work_queue.put(_REBOOT_SENTINEL)

   def stop(self):
      """Inject KeyboardInterrupt into the worker thread."""
      tid = self._worker_tid
      if tid is not None:
         ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(tid),
            ctypes.py_object(KeyboardInterrupt),
         )

   # ---- internal helpers -------------------------------------------------

   def _put_banner(self, kind):
      if kind == 'startup':
         text = ('pyscheme ' + __version__ + '  |  cherry GUI\n'
                 "Type Scheme expressions below, or use the editor's Run button.\n")
      else:
         text = '--- Interpreter rebooted ---\n'
      self.result_queue.put(('banner', text))
      self.result_queue.put(('ready',))

   def _run_test_files(self, filenames):
      """Run each .log file through sessionLog_test, rebooting between files.
      Streams all output to result_queue.  Prints a grand-total summary line."""
      import os
      qs    = _QueueStream(self.result_queue)
      saved = sys.stdout
      sys.stdout = qs
      grand_pass = 0
      grand_fail = 0
      saved_fallback = _expander_mod._include_fallback_dir
      try:
         for path in filenames:
            self._interp.reboot(load_rc=False)
            _expander_mod._include_fallback_dir = os.path.dirname(
               os.path.abspath(path))
            listener = _QuietListener(self._interp)
            base   = os.path.basename(path)
            padded = base.ljust(40)
            # Name and status are intentionally two separate prints: name appears
            # before the test runs so the user can see progress; status completes
            # the same line after.  Do not merge into one print.
            print(padded + ' ', end='', flush=True)
            try:
               r = listener.sessionLog_test(path, verbosity=3)
               grand_pass += r.n_pass
               grand_fail += r.n_fail
               if r.n_fail == 0:
                  print(str(r.n_pass) + ' passed')
               else:
                  total = r.n_pass + r.n_fail
                  print(str(r.n_fail) + ' of ' + str(total) + ' failed')
            except KeyboardInterrupt:
               self.result_queue.put(('error', 'Test run interrupted.'))
               return
            except Exception as exc:
               self.result_queue.put(('error', Listener._format_error(exc)))
         if len(filenames) > 1:
            total = grand_pass + grand_fail
            summary = ('\n--- ' + str(grand_pass) + '/' + str(total)
                       + ' passed across ' + str(len(filenames)) + ' files ---\n')
            self.result_queue.put(('output', summary))
      finally:
         sys.stdout = saved
         _expander_mod._include_fallback_dir = saved_fallback
         self._interp.reboot(load_rc=False)
         self._put_banner('reboot')

   def _run_compliance_files(self, filenames, compliancedir):
      """Run each .log file through sessionLog_compliance_test.

      Detail output (PASS/FAIL lines) goes to a timestamped run file in
      <compliancedir>/runs/.  Per-file status lines stream to the GUI via
      result_queue.  The interpreter is rebooted between files and after
      the full suite."""
      import os
      import datetime

      qs    = _QueueStream(self.result_queue)
      saved = sys.stdout
      sys.stdout = qs

      runFile     = None
      runFilename = ''
      # Run reports go to the shared scheme-tests/runs/ (parent of the
      # compliance dir), never to a runs/ under R7RS-Compliance-Tests.
      runsDir = os.path.join(os.path.dirname(os.path.abspath(compliancedir)), 'runs')
      try:
         os.makedirs(runsDir, exist_ok=True)
         timestamp   = datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')
         runFilename = os.path.join(runsDir, timestamp + '-compliance-PyScheme.run')
         runFile     = open(runFilename, 'w', encoding='utf-8')
      except OSError:
         runFile     = None
         runFilename = ''

      grand_pass = 0
      grand_fail = 0
      per_file   = []
      savedCwd   = os.getcwd()
      try:
         os.chdir(os.path.abspath(compliancedir))
         for path in filenames:
            self._interp.reboot(load_rc=False)
            listener = _QuietListener(self._interp)
            base   = os.path.basename(path)
            padded = base.ljust(56)
            # Name flushes to the GUI before the test runs; status follows after.
            print(padded + ' ', end='', flush=True)
            try:
               if runFile is not None:
                  sys.stdout = runFile
               r = listener.sessionLog_compliance_test(path, verbosity=3)
               if runFile is not None:
                  sys.stdout = qs
               grand_pass += r.n_pass
               grand_fail += r.n_fail
               per_file.append((path, r.n_pass, r.n_fail))
               if r.n_fail == 0:
                  print(str(r.n_pass) + ' passed')
               else:
                  total = r.n_pass + r.n_fail
                  print(str(r.n_fail) + ' of ' + str(total) + ' failed')
            except KeyboardInterrupt:
               sys.stdout = qs
               self.result_queue.put(('error', 'Compliance run interrupted.'))
               return
            except Exception as exc:
               sys.stdout = qs
               self.result_queue.put(('error', Listener._format_error(exc)))
      finally:
         sys.stdout = qs
         os.chdir(savedCwd)

      # Grand-total status to GUI.
      total = grand_pass + grand_fail
      nfiles = len(filenames)
      if grand_fail == 0:
         summary = ('\n--- all ' + str(total) + ' test cases passed across '
                    + str(nfiles) + ' files ---\n')
      else:
         summary = ('\n--- ' + str(grand_fail) + ' of ' + str(total)
                    + ' tests failed across ' + str(nfiles) + ' files ---\n')
      self.result_queue.put(('output', summary))

      # Write Test Report tail to run file.
      if runFile is not None:
         report = []
         report.append('')
         report.append('')
         report.append('Test Report')
         report.append('===========')
         for entry in per_file:
            name  = entry[0]
            p     = entry[1]
            f     = entry[2]
            short = os.path.basename(name)
            if f == 0:
               msg = str(p) + ' TESTS PASSED!'
            else:
               tot = p + f
               msg = '(' + str(f) + '/' + str(tot) + ') Failed.'
            report.append(short.ljust(56) + ' ' + msg)
         report.append('')
         report.append('Total test files: ' + str(len(filenames)) + '.')
         report.append('Total test cases: ' + str(grand_pass + grand_fail) + '.')
         for line in report:
            print(line, file=runFile)
         runFile.close()
         self.result_queue.put(('output',
            'Compliance run report: ' + runFilename + '\n'))

      sys.stdout = saved
      self._interp.reboot(load_rc=False)
      self._put_banner('reboot')

   # ---- worker thread ----------------------------------------------------

   def _worker(self):
      self._worker_tid = threading.current_thread().ident
      while True:
         item = self._work_queue.get()

         if item is _REBOOT_SENTINEL:
            self._interp.reboot()
            self._put_banner('reboot')
            continue

         if isinstance(item, _FileItem):
            out_buf = io.StringIO()
            error   = ''
            try:
               self._interp.evalFile(item.path, outStrm=out_buf)
            except KeyboardInterrupt:
               error = 'Interrupted.'
            except Exception as exc:
               error = Listener._format_error(exc)
            output = out_buf.getvalue()
            if output:
               self.result_queue.put(('output', output))
            if error:
               self.result_queue.put(('error', error))
            self.result_queue.put(('ready',))
            continue

         if isinstance(item, _TestItem):
            self._run_test_files([item.path])
            continue

         if isinstance(item, _TestDirItem):
            import os
            try:
               filenames = retrieveFileList(item.path)
            except Exception as exc:
               self.result_queue.put(('error', Listener._format_error(exc)))
               self.result_queue.put(('ready',))
               continue
            if not filenames:
               self.result_queue.put(('error',
                  'No .log files found in: ' + item.path))
               self.result_queue.put(('ready',))
               continue
            self._run_test_files(filenames)
            continue

         if isinstance(item, _ComplianceDirItem):
            import os
            try:
               filenames = retrieveFileList(item.path)
            except Exception as exc:
               self.result_queue.put(('error', Listener._format_error(exc)))
               self.result_queue.put(('ready',))
               continue
            if not filenames:
               self.result_queue.put(('error',
                  'No .log files found in: ' + item.path))
               self.result_queue.put(('ready',))
               continue
            self._run_compliance_files(filenames, item.path)
            continue

         # Normal expression evaluation
         source  = item
         out_buf = io.StringIO()
         result  = ''
         error   = ''
         try:
            result = self._interp.eval(source, outStrm=out_buf)
         except KeyboardInterrupt:
            error = 'Interrupted.'
         except Exception as exc:
            error = Listener._format_error(exc)
         output = out_buf.getvalue()
         if output:
            self.result_queue.put(('output', output))
         if error:
            self.result_queue.put(('error', error))
         elif result:
            self.result_queue.put(('result', result))
         self.result_queue.put(('ready',))
