"""Quick metrics summary from run_log.jsonl."""
import json

entries = []
with open("data/run_log.jsonl", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            entries.append(json.loads(line))

print(f"Total tasks: {len(entries)}")
opus = [e for e in entries if e.get("tier") == "high" and e.get("success")]
son = [e for e in entries if e.get("tier") == "medium" and e.get("success")]
haiku = [e for e in entries if e.get("tier") == "low" and e.get("success")]

for label, group in [("Opus", opus), ("Sonnet", son), ("Haiku", haiku)]:
    if not group:
        print(f"{label}: 0 tasks")
        continue
    turns = sum(e.get("num_turns", 0) for e in group)
    tools = sum(len(e.get("tools_used", [])) for e in group)
    tpt = tools / turns if turns else 0
    avg_turns = turns / len(group)
    avg_tools = tools / len(group)
    print(f"{label} ({len(group)} tasks): avg_turns={avg_turns:.1f}, avg_tools={avg_tools:.1f}, tools/turn={tpt:.2f}")

# Last 5 Opus specifically
print("\nLast 5 Opus tasks:")
for e in opus[-5:]:
    t = e.get("num_turns", 0)
    tc = len(e.get("tools_used", []))
    tpt_val = tc / t if t else 0
    dur = e.get("duration_s", 0)
    print(f"  turns={t} tools={tc} t/t={tpt_val:.1f} dur={dur:.0f}s")
