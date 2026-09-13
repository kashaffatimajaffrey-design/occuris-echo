"""Turn results.json (+ results_ablation.json if present) into the brief's table."""
import json, sys
from pathlib import Path

HERE = Path(__file__).parent

def load(name):
    p = HERE / name
    return json.loads(p.read_text()) if p.exists() else None

def summarise(r):
    if not r: return None
    sets = {}
    for cid, v in r.items():
        s = sets.setdefault(v["set"], {"n": 0, "pass": 0, "silent": 0, "over": 0, "steps": {}})
        s["n"] += 1
        ok = all(v["steps"].values())
        s["pass"] += ok
        s["silent"] += v["silent_failure"]
        s["over"] += v.get("over_clarified", False)
        for k, b in v["steps"].items():
            st = s["steps"].setdefault(k, [0, 0]); st[0] += b; st[1] += 1
    return sets

def main():
    main_r, abl_r = summarise(load("results.json")), summarise(load("results_ablation.json"))
    if not main_r:
        print("no results.json — run pytest first"); sys.exit(1)
    total = sum(s["n"] for s in main_r.values())
    passed = sum(s["pass"] for s in main_r.values())
    silent = sum(s["silent"] for s in main_r.values())
    over = sum(s["over"] for s in main_r.values())
    refusal_cases = main_r.get("B", {})
    lines = []
    lines.append(f"## Results — {passed}/{total} cases pass\n")
    lines.append(f"- **Silent failures: {silent}** (acted confidently and wrongly, or claimed done for a non-done action)")
    lines.append(f"- **Over-clarifications (A-set): {over}** — the agent asked when it should have acted")
    if abl_r:
        asil = sum(s["silent"] for s in abl_r.values())
        lines.append(f"- **Ablation — deterministic layer off: {asil} silent failures** (Δ +{asil - silent})")
    lines.append("")
    lines.append("| Set | Cases | Pass | Silent failures |" + (" Silent (ablation) |" if abl_r else ""))
    lines.append("|---|---|---|---|" + ("---|" if abl_r else ""))
    for k in sorted(main_r):
        s = main_r[k]
        row = f"| {k} | {s['n']} | {s['pass']} | {s['silent']} |"
        if abl_r and k in abl_r: row += f" {abl_r[k]['silent']} |"
        lines.append(row)
    lines.append("\n### Per-step accuracy\n")
    lines.append("| Step | Correct | Of |\n|---|---|---|")
    agg = {}
    for s in main_r.values():
        for k, (a, b) in s["steps"].items():
            t = agg.setdefault(k, [0, 0]); t[0] += a; t[1] += b
    for k, (a, b) in sorted(agg.items()):
        lines.append(f"| {k} | {a} | {b} |")
    out = "\n".join(lines)
    (HERE.parent / "RESULTS.md").write_text(out, encoding="utf-8")
    print(out)

if __name__ == "__main__":
    main()
