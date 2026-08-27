from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mask_noncode(source: str) -> str:
    """Mask comments and quoted/here-string content while preserving newlines."""
    out: list[str] = []
    i = 0
    state = "code"
    here_end = ""
    while i < len(source):
        ch = source[i]
        nxt = source[i : i + 2]
        if state == "line_comment":
            if ch == "\n":
                state = "code"
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue
        if state == "block_comment":
            if nxt == "#>":
                out.extend("  ")
                i += 2
                state = "code"
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue
        if state in {"single", "double"}:
            quote = "'" if state == "single" else '"'
            if ch == "`" and i + 1 < len(source):
                out.extend("  ")
                i += 2
            elif ch == quote:
                out.append(" ")
                i += 1
                state = "code"
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue
        if state == "here":
            line_end = source.find("\n", i)
            if line_end < 0:
                line_end = len(source)
            line = source[i:line_end].strip()
            if line == here_end:
                out.extend(" " * (line_end - i))
                state = "code"
            else:
                out.extend(" " * (line_end - i))
            if line_end < len(source):
                out.append("\n")
            i = line_end + (1 if line_end < len(source) else 0)
            continue

        if nxt == "<#":
            out.extend("  ")
            i += 2
            state = "block_comment"
        elif ch == "#":
            out.append(" ")
            i += 1
            state = "line_comment"
        elif nxt in {"@'", '@"'}:
            here_end = "'@" if nxt == "@'" else '"@'
            out.extend("  ")
            i += 2
            state = "here"
        elif ch == "'":
            out.append(" ")
            i += 1
            state = "single"
        elif ch == '"':
            out.append(" ")
            i += 1
            state = "double"
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def validate(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8-sig")
    code = _mask_noncode(source)
    failures: list[str] = []
    pairs = {"}": "{", ")": "(", "]": "["}
    stack: list[tuple[str, int]] = []
    for index, ch in enumerate(code):
        if ch in "{([":
            stack.append((ch, index))
        elif ch in "})]":
            if not stack or stack[-1][0] != pairs[ch]:
                line = code.count("\n", 0, index) + 1
                failures.append(f"{path.relative_to(ROOT)}:{line}: unmatched {ch}")
                break
            stack.pop()
    if stack:
        ch, index = stack[-1]
        line = code.count("\n", 0, index) + 1
        failures.append(f"{path.relative_to(ROOT)}:{line}: unclosed {ch}")
    duplicate_else = re.search(r"}\s*else\s*{\s*}\s*else\s*{", code, flags=re.IGNORECASE)
    if duplicate_else:
        line = code.count("\n", 0, duplicate_else.start()) + 1
        failures.append(f"{path.relative_to(ROOT)}:{line}: duplicate empty else branch")

    # Windows PowerShell 5.1 binds TrimStart/TrimEnd character arguments to
    # System.Char. A single-quoted '\\' literal contains two characters in
    # PowerShell (backslash is not an escape character) and therefore throws at
    # runtime: "String must be exactly one character long." This exact defect
    # escaped source validation in v109.0.1, so reject it structurally anywhere
    # in packaged PowerShell. The same doubled literal is also wrong for
    # String.Replace when the intent is one Windows path separator.
    risky_literals = (
        ".TrimStart('\\\\'",
        ".TrimEnd('\\\\'",
        ".Replace('\\\\',",
    )
    for token in risky_literals:
        start = 0
        while True:
            index = source.find(token, start)
            if index < 0:
                break
            line = source.count("\n", 0, index) + 1
            failures.append(f"{path.relative_to(ROOT)}:{line}: unsafe multi-character backslash literal in char/path method: {token}")
            start = index + len(token)
    return failures


def main() -> int:
    files = sorted(ROOT.rglob("*.ps1"))
    failures = [failure for path in files for failure in validate(path)]
    report = {"ok": not failures, "files_scanned": len(files), "failures": failures}
    print(json.dumps(report, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
