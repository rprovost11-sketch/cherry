"""Paren-matching utilities shared by the editor and REPL panes.

make_code_map(text) -> bytearray
    1 at every position that is live Scheme code (not inside a string
    literal or a line comment starting with ';').  Block comments (#|..|#)
    and datum comments (#;) are not handled -- they are rare enough that
    the visual artifact of a missed match is acceptable.

find_match(text, code, pos) -> int
    Return the index of the paren that matches the one at pos, or -1.
    Handles '(' / ')' and '[' / ']'.
"""

_OPEN_TO_CLOSE = {'(': ')', '[': ']'}
_CLOSE_TO_OPEN = {')': '(', ']': '['}


def make_code_map(text):
    n      = len(text)
    code   = bytearray(n)
    in_str = False
    escape = False
    i = 0
    while i < n:
        c = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if in_str:
            if c == '\\':
                escape = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            code[i] = 1
            i += 1
            continue
        if c == ';':
            while i < n and text[i] != '\n':
                i += 1
            continue
        code[i] = 1
        i += 1
    return code


def find_match(text, code, pos):
    """Return the index of the matching paren for the one at pos, or -1."""
    c = text[pos]
    if c in _OPEN_TO_CLOSE:
        close = _OPEN_TO_CLOSE[c]
        depth = 0
        i = pos
        n = len(text)
        while i < n:
            if code[i]:
                if text[i] == c:
                    depth += 1
                elif text[i] == close:
                    depth -= 1
                    if depth == 0:
                        return i
            i += 1
        return -1
    elif c in _CLOSE_TO_OPEN:
        open_c = _CLOSE_TO_OPEN[c]
        depth = 0
        i = pos
        while i >= 0:
            if code[i]:
                if text[i] == c:
                    depth += 1
                elif text[i] == open_c:
                    depth -= 1
                    if depth == 0:
                        return i
            i -= 1
        return -1
    return -1
