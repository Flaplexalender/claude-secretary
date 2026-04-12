#!/usr/bin/env python3
"""Pre-commit hook: block commits that contain PII patterns.

Reads regex patterns from .pii-patterns (one per line, # = comment).
Scans staged file diffs for matches. Blocks commit if found.
Bypass: git commit --no-verify
"""
import re
import subprocess
import sys
from pathlib import Path

PATTERNS_FILE = ".pii-patterns"

# Files that legitimately contain PII patterns (the patterns file itself, etc.)
EXEMPT_FILES = {".pii-patterns", "hooks/pre-commit.py", "CONTRIBUTING.md"}


def load_patterns() -> list[re.Pattern]:
    path = Path(PATTERNS_FILE)
    if not path.exists():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line, re.IGNORECASE))
        except re.error:
            print(f"WARNING: invalid regex in {PATTERNS_FILE}: {line}")
    return patterns


def get_staged_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "--cached", "--diff-filter=d", "-U0"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return result.stdout


def scan_diff(diff_text: str, patterns: list[re.Pattern]) -> list[str]:
    hits = []
    current_file = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            parts = line.split(" b/")
            current_file = parts[-1] if len(parts) > 1 else ""
        # Skip exempt files (e.g., the patterns file itself)
        if current_file in EXEMPT_FILES:
            continue
        # Only check added lines (not removed ones or headers)
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pat in patterns:
            if pat.search(line):
                hits.append(f"  {current_file}: {line[:120]}")
                break  # one hit per line is enough
    return hits


def main() -> int:
    patterns = load_patterns()
    if not patterns:
        return 0

    diff = get_staged_diff()
    if not diff:
        return 0

    hits = scan_diff(diff, patterns)
    if hits:
        print()
        print("=" * 60)
        print("  PII DETECTED -- commit blocked")
        print("=" * 60)
        print()
        print("  Staged content matches patterns in .pii-patterns:")
        print()
        for hit in hits[:20]:
            print(hit)
        print()
        print("  Fix the content, or bypass with: git commit --no-verify")
        print()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
