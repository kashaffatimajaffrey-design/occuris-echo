## Results — 34/34 cases pass

- **Silent failures: 0** (acted confidently and wrongly, or claimed done for a non-done action)
- **Over-clarifications (A-set): 0** — the agent asked when it should have acted
- **Ablation — deterministic layer off: 3 silent failures** (+3)

| Set | Cases | Pass | Silent failures | Silent (ablation) |
|---|---|---|---|---|
| A | 9 | 9 | 0 | 0 |
| B | 17 | 17 | 0 | 3 |
| C | 4 | 4 | 0 | 0 |
| D | 4 | 4 | 0 | 0 |

### Per-step accuracy

| Step | Correct | Of |
|---|---|---|
| actions | 27 | 27 |
| conflict | 1 | 1 |
| corrections | 1 | 1 |
| event_times | 1 | 1 |
| gate | 9 | 9 |
| honest_readback | 1 | 1 |
| idempotent_rerun | 1 | 1 |
| injection | 1 | 1 |
| intent | 28 | 28 |
| no_crash | 1 | 1 |
| no_writes | 20 | 20 |
| pending | 1 | 1 |
| question | 6 | 6 |
| readback | 22 | 22 |
| readback_clean | 2 | 2 |
| refused_correctly | 9 | 9 |
| sent_clean | 1 | 1 |
| slots | 9 | 9 |
| state | 14 | 14 |