import sys, os
_lisp = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_lisp, '3PyScheme'))
from .app import main
main()
