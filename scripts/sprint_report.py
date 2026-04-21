"""Aggregate metrics from a sprint watch run.

Usage:
    python scripts/sprint_report.py [--since ISO8601] [--log data/sprint-1hour.log]

Outputs a markdown summary to stdout and optionally to a file.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def parse_iso(s: str) -> datetime:
    # Accept "Z" or offset
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO timestamp; only include entries after this", default=None)
    ap.add_argument("--log", default="data/sprint-1hour.log")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    since = parse_iso(args.since) if args.since else None

    root = Path(__file__).resolve().parents[1]
    run_log = root / "data" / "run_log.jsonl"
    log_file = root / args.log

    entries: list[dict] = []
    if run_log.exists():
        for line in run_log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = e.get("timestamp") or e.get("ts")
            if since and ts:
                try:
                    if parse_iso(ts) < since:
                        continue
                except ValueError:
                    pass
            entries.append(e)

    total = len(entries)
    passed = sum(1 for e in entries if e.get("success") is True)
    failed = sum(1 for e in entries if e.get("success") is False)

    def _cost(e: dict) -> float:
        v = e.get("premium_cost")
        return float(v) if isinstance(v, (int, float)) else 0.0

    def _usd(e: dict) -> float:
        v = e.get("cost_usd")
        return float(v) if isinstance(v, (int, float)) else 0.0

    total_mult = sum(_cost(e) for e in entries)
    total_usd = sum(_usd(e) for e in entries)
    total_turns = sum(e.get("num_turns", 0) for e in entries)

    by_model: dict[str, dict] = {}
    for e in entries:
        m = e.get("model") or "unknown"
        slot = by_model.setdefault(m, {"n": 0, "pass": 0, "fail": 0, "mult": 0.0, "turns": 0})
        slot["n"] += 1
        if e.get("success") is True:
            slot["pass"] += 1
        elif e.get("success") is False:
            slot["fail"] += 1
        slot["mult"] += _cost(e)
        slot["turns"] += e.get("num_turns", 0)

    by_goal: dict[str, dict] = {}
    for e in entries:
        g = e.get("goal_id") or "(no goal)"
        slot = by_goal.setdefault(g, {"n": 0, "pass": 0, "fail": 0, "mult": 0.0})
        slot["n"] += 1
        if e.get("success") is True:
            slot["pass"] += 1
        elif e.get("success") is False:
            slot["fail"] += 1
        slot["mult"] += _cost(e)

    # Scan log for sprint-specific events
    events = {"auto_approved": 0, "auto_rejected": 0, "promoted": 0, "rollback": 0, "ci_passed": 0, "ci_failed": 0}
    if log_file.exists():
        text = log_file.read_text(encoding="utf-8", errors="replace")
        events["auto_approved"] = text.count("Auto-approved")
        events["auto_rejected"] = text.count("stale_auto_reject")
        events["promoted"] = text.count("Promoted proposal") + text.count("promoted proposal")
        events["rollback"] = text.count("AUTO-GRADUATION ROLLBACK")
        events["ci_passed"] = text.count("CI passed") + text.count("All checks have passed")
        events["ci_failed"] = text.count("CI failed")

    lines = []
    lines.append("# Sprint Report")
    lines.append(f"- Since: {args.since or 'all time'}")
    lines.append(f"- Log: `{args.log}`")
    lines.append(f"- Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Task outcomes")
    lines.append(f"- Total tasks: **{total}**")
    lines.append(f"- Passed: **{passed}**")
    lines.append(f"- Failed: **{failed}**")
    if total:
        lines.append(f"- Success rate: **{passed/total*100:.1f}%**")
    lines.append(f"- Total premium-equiv multiplier: **{total_mult:.2f}x** (0 GH premium if proxy+prefix on)")
    lines.append(f"- Paid-mode equivalent: **${total_usd:.2f} USD** (${total_usd*1.44:.2f} CAD)")
    lines.append(f"- Total tool turns: **{total_turns}**")
    if passed:
        lines.append(f"- Cost-per-success (paid-mode projection): **${total_usd/passed:.3f} USD** / **${total_usd*1.44/passed:.3f} CAD**")
    lines.append("")
    lines.append("## By model")
    for m, s in sorted(by_model.items(), key=lambda kv: -kv[1]["n"]):
        rate = (s["pass"] / s["n"] * 100) if s["n"] else 0.0
        lines.append(f"- `{m}`: {s['n']} tasks, {s['pass']} pass / {s['fail']} fail ({rate:.0f}%), {s['mult']:.2f}x mult, {s['turns']} turns")
    lines.append("")
    lines.append("## By goal")
    for g, s in sorted(by_goal.items(), key=lambda kv: -kv[1]["n"]):
        rate = (s["pass"] / s["n"] * 100) if s["n"] else 0.0
        lines.append(f"- `{g}`: {s['n']} tasks, {s['pass']}/{s['fail']} ({rate:.0f}%), {s['mult']:.2f}x mult")
    lines.append("")
    lines.append("## Sprint events (from log)")
    for k, v in events.items():
        lines.append(f"- {k.replace('_', ' ')}: {v}")

    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
