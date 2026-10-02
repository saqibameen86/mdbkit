"""Read what people actually paste: mongosh output and Windows-encoded files.

`db.serverStatus()` or `.explain()` printed by mongosh is JavaScript, not
JSON — unquoted keys, single quotes, `Long('42')`, `ISODate('...')`,
`Timestamp({ t: 1, i: 2 })`, trailing commas. And a file redirected in
Windows PowerShell 5 is UTF-16 with a byte-order mark. Asking someone to
re-export during an incident is a bad answer, so this converts both.

It is a small tokenizer, not an evaluator: nothing in the input is ever
executed. Values that only matter to the shell (ObjectIds, dates, binary)
become plain strings.
"""

from __future__ import annotations

import json
from typing import List

NUMERIC_CTORS = {"long", "numberlong", "int32", "numberint", "int64", "double",
                 "decimal128", "numberdecimal", "float"}
STRING_CTORS = {"isodate", "date", "objectid", "uuid", "bindata", "hexdata",
                "md5", "regexp", "symbol", "code"}
NULL_WORDS = {"undefined", "nan", "infinity", "minkey", "maxkey"}


def read_text_file(path: str) -> str:
    """Read a text file whatever its encoding: UTF-8, UTF-8 with BOM, or
    UTF-16 (with or without BOM, as PowerShell 5 writes it)."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    # UTF-16 without a BOM: every other byte of ASCII text is NUL.
    sample = data[:200]
    if len(sample) >= 4 and sample[1::2].count(0) > len(sample) // 4:
        return data.decode("utf-16-le", "replace")
    if len(sample) >= 4 and sample[0::2].count(0) > len(sample) // 4:
        return data.decode("utf-16-be", "replace")
    return data.decode("utf-8", "replace")


def _read_string(src: str, i: int) -> tuple:
    """Read a quoted string starting at src[i]; return (json_string, next_i)."""
    quote = src[i]
    i += 1
    chars: List[str] = []
    while i < len(src):
        c = src[i]
        if c == "\\" and i + 1 < len(src):
            nxt = src[i + 1]
            if nxt == "'" and quote == "'":
                chars.append("'")
            else:
                chars.append(c + nxt)
            i += 2
            continue
        if c == quote:
            i += 1
            break
        if c == '"' and quote == "'":
            chars.append('\\"')
        else:
            chars.append(c)
        i += 1
    return '"' + "".join(chars) + '"', i


def _matching_paren(src: str, i: int) -> int:
    """Index of the ')' matching the '(' at src[i]."""
    depth = 0
    while i < len(src):
        c = src[i]
        if c in "'\"":
            _, i = _read_string(src, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(src) - 1


def _split_args(src: str) -> List[str]:
    """Split a JSON-ish argument list on top-level commas."""
    out, depth, start, i = [], 0, 0, 0
    while i < len(src):
        c = src[i]
        if c == '"':
            _, i = _read_string(src, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(src[start:i].strip())
            start = i + 1
        i += 1
    tail = src[start:].strip()
    if tail:
        out.append(tail)
    return out


def _unquote(arg: str) -> str:
    if len(arg) >= 2 and arg[0] == '"' and arg[-1] == '"':
        return json.loads(arg)
    return arg


def _ctor(name: str, inner: str) -> str:
    low = name.lower()
    args = _split_args(inner)
    if low in NUMERIC_CTORS:
        if not args:
            return "0"
        value = _unquote(args[0]).strip()
        try:
            float(value)
            return value
        except ValueError:
            return json.dumps(value)
    if low == "timestamp":
        if len(args) == 1 and args[0].startswith("{"):
            return args[0]
        if len(args) >= 2:
            return '{"t": %s, "i": %s}' % (args[0], args[1])
        return "null"
    if low == "dbref" and len(args) >= 2:
        return '{"$ref": %s, "$id": %s}' % (args[0], args[1])
    if low in NULL_WORDS:
        return "null"
    if low in STRING_CTORS or not args:
        if not args:
            return "null"
        return json.dumps(_unquote(args[-1]))
    return args[0] if len(args) == 1 else "[" + ", ".join(args) + "]"


def shell_to_json(src: str) -> str:
    """Convert mongosh/legacy-shell output to strict JSON text."""
    out: List[str] = []
    stack: List[str] = []          # open brackets, to know object vs array
    i, n = 0, len(src)
    expect_key = False

    def last_significant() -> str:
        for piece in reversed(out):
            stripped = piece.strip()
            if stripped:
                return stripped[-1]
        return ""

    while i < n:
        c = src[i]
        if c in "'\"":
            s, i = _read_string(src, i)
            # mongosh prints long strings as 'part one' +\n 'part two'.
            while True:
                k = i
                while k < n and src[k] in " \t\r\n":
                    k += 1
                if k < n and src[k] == "+":
                    k += 1
                    while k < n and src[k] in " \t\r\n":
                        k += 1
                    if k < n and src[k] in "'\"":
                        more, i = _read_string(src, k)
                        s = s[:-1] + more[1:]
                        continue
                break
            out.append(s)
            expect_key = False
            continue
        if c in "{,":
            out.append(c)
            if c == "{":
                stack.append("{")
            expect_key = c == "{" or (c == "," and bool(stack) and stack[-1] == "{")
            i += 1
            continue
        if c == "[":
            stack.append("[")
            out.append(c)
            i += 1
            continue
        if c in "}]":
            if stack:
                stack.pop()
            # drop a trailing comma
            while out and not out[-1].strip():
                out.pop()
            if out and out[-1] == ",":
                out.pop()
            out.append(c)
            i += 1
            continue
        if c == "/" and last_significant() in ":,[(":
            j = i + 1
            while j < n and src[j] != "/":
                j += 2 if src[j] == "\\" else 1
            pattern = src[i + 1:j]
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            out.append(json.dumps(pattern))
            i = j
            continue
        if c.isalpha() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$."):
                j += 1
            word = src[i:j]
            k = j
            while k < n and src[k] in " \t\r\n":
                k += 1
            if word == "new":
                i = k
                continue
            if k < n and src[k] == "(":
                end = _matching_paren(src, k)
                inner = shell_to_json(src[k + 1:end])
                out.append(_ctor(word, inner))
                i = end + 1
                expect_key = False
                continue
            if k < n and src[k] == ":" and expect_key:
                out.append(json.dumps(word))
                i = j
                continue
            if word in ("true", "false", "null"):
                out.append(word)
            elif word.lower() in NULL_WORDS:
                out.append("null")
            else:
                out.append(json.dumps(word))
            i = j
            expect_key = False
            continue
        if c == "-" and src[i + 1:i + 9] == "Infinity":
            out.append("null")
            i += 9
            continue
        out.append(c)
        if c == ":":
            expect_key = False
        i += 1
    return "".join(out)


def loads_lenient(src: str):
    """json.loads, falling back to the mongosh converter."""
    src = src.lstrip("﻿")
    try:
        return json.loads(src)
    except ValueError:
        return json.loads(shell_to_json(src))
