"""Guard for the public profile page.

Fails when a tracked public file contains:
  - letters outside the English alphabet (the page is English only);
  - an e-mail address or a mailto: link (contact goes through forms, never addresses).

Usage: python scripts/check_public_text.py [files...]
Without arguments it checks README.md and every SVG under stats/.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"mailto:", re.IGNORECASE)


def default_files() -> list[Path]:
    files = [ROOT / "README.md"]
    files += sorted((ROOT / "stats").glob("*.svg"))
    return [f for f in files if f.exists()]


def check(path: Path) -> list[str]:
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), 1):
        for ch in line:
            if ord(ch) > 127 and unicodedata.category(ch).startswith("L"):
                problems.append(f"{path.name}:{lineno}: non-English letter U+{ord(ch):04X}")
                break
        if EMAIL_RE.search(line):
            problems.append(f"{path.name}:{lineno}: e-mail address")
        if MAILTO_RE.search(line):
            problems.append(f"{path.name}:{lineno}: mailto link")
    return problems


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] if argv else default_files()
    problems: list[str] = []
    for f in files:
        problems += check(f)
    print(f"check_public_text: {len(files)} files checked")
    for p in problems:
        print(f"  FAIL {p}")
    if problems:
        return 1
    print("check_public_text: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
