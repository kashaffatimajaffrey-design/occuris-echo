## Results — 36/36 cases pass

- **Silent failures: 0** (acted confidently and wrongly, or claimed done for a non-done action)
- **Over-clarifications (A-set): 0** — the agent asked when it should have acted
- **Ablation — deterministic layer off: 3 silent failures** (+3)

| Set | Cases | Pass | Silent failures | Silent (ablation) |
|---|---|---|---|---|
| A | 9 | 9 | 0 | 0 |
| B | 19 | 19 | 0 | 3 |
| C | 4 | 4 | 0 | 0 |
| D | 4 | 4 | 0 | 0 |

### Per-step accuracy

| Step | Correct | Of |
|---|---|---|
| actions | 29 | 29 |
| conflict | 1 | 1 |
| corrections | 1 | 1 |
| event_times | 2 | 2 |
| gate | 10 | 10 |
| honest_readback | 1 | 1 |
| idempotent_rerun | 1 | 1 |
| injection | 1 | 1 |
| intent | 30 | 30 |
| no_crash | 1 | 1 |
| no_writes | 21 | 21 |
| pending | 1 | 1 |
| question | 6 | 6 |
| readback | 23 | 23 |
| readback_clean | 2 | 2 |
| refused_correctly | 9 | 9 |
| sent_clean | 1 | 1 |
| slots | 10 | 10 |
| state | 15 | 15 |