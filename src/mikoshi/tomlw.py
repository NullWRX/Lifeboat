"""A minimal TOML writer for Mikoshi's own config.

Python ships a TOML reader (tomllib) but no writer. Mikoshi's config only
uses strings, booleans, integers, floats, lists of those, and nested tables,
so a small writer avoids an extra dependency. Output round-trips through
tomllib (checked in the tests).
"""

from __future__ import annotations

import re

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else _string(k)


def _string(s: str) -> str:
    out = ['"']
    for ch in s:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("NaN and infinity are not supported")
        return repr(v)
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, (list, tuple)):
        if any(isinstance(x, dict) for x in v):
            raise ValueError("arrays of tables are not supported")
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"unsupported TOML value: {type(v).__name__}")


def dumps(data: dict, header: str = "") -> str:
    lines: list[str] = []
    if header:
        lines.extend("# " + h if h else "#" for h in header.splitlines())
        lines.append("")
    _emit_table(lines, data, [])
    text = "\n".join(lines).strip("\n") + "\n"
    return text


def _emit_table(lines: list[str], table: dict, path: list[str]) -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in table.items() if isinstance(v, dict)}
    if path and (scalars or not tables):
        lines.append("[" + ".".join(_key(p) for p in path) + "]")
    for k, v in scalars.items():
        if v is None:
            continue
        lines.append(f"{_key(k)} = {_value(v)}")
    if path and (scalars or not tables):
        lines.append("")
    for k, v in tables.items():
        _emit_table(lines, v, path + [k])
