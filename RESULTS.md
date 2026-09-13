## Results — 57/57 cases pass

- **Silent failures: 0** (acted confidently and wrongly, or claimed done for a non-done action)
- **Over-clarifications (A-set): 0** — the agent asked when it should have acted
- **Ablation — deterministic layer off: 3 silent failures** (+3)

| Set | Cases | Pass | Silent failures | Silent (ablation) |
|---|---|---|---|---|
| A | 19 | 19 | 0 | 0 |
| B | 30 | 30 | 0 | 3 |
| C | 4 | 4 | 0 | 0 |
| D | 4 | 4 | 0 | 0 |

### Per-step accuracy

| Step | Correct | Of |
|---|---|---|
| actions | 40 | 40 |
| conflict | 1 | 1 |
| corrections | 1 | 1 |
| event_times | 9 | 9 |
| gate | 13 | 13 |
| honest_readback | 1 | 1 |
| idempotent_rerun | 1 | 1 |
| injection | 1 | 1 |
| intent | 43 | 43 |
| no_crash | 1 | 1 |
| no_writes | 29 | 29 |
| pending | 1 | 1 |
| question | 8 | 8 |
| question_first | 1 | 1 |
| readback | 41 | 41 |
| readback_clean | 5 | 5 |
| refused_correctly | 13 | 13 |
| sent_clean | 1 | 1 |
| slots | 10 | 10 |
| state | 30 | 30 |