## Results — 40/40 cases pass

- **Silent failures: 0** (acted confidently and wrongly, or claimed done for a non-done action)
- **Over-clarifications (A-set): 0** — the agent asked when it should have acted
- **Ablation — deterministic layer off: 3 silent failures** (+3)

| Set | Cases | Pass | Silent failures | Silent (ablation) |
|---|---|---|---|---|
| A | 9 | 9 | 0 | 0 |
| B | 23 | 23 | 0 | 3 |
| C | 4 | 4 | 0 | 0 |
| D | 4 | 4 | 0 | 0 |

### Per-step accuracy

| Step | Correct | Of |
|---|---|---|
| actions | 29 | 29 |
| conflict | 1 | 1 |
| corrections | 1 | 1 |
| event_times | 3 | 3 |
| gate | 11 | 11 |
| honest_readback | 1 | 1 |
| idempotent_rerun | 1 | 1 |
| injection | 1 | 1 |
| intent | 33 | 33 |
| no_crash | 1 | 1 |
| no_writes | 23 | 23 |
| pending | 1 | 1 |
| question | 7 | 7 |
| question_first | 1 | 1 |
| readback | 26 | 26 |
| readback_clean | 2 | 2 |
| refused_correctly | 11 | 11 |
| sent_clean | 1 | 1 |
| slots | 10 | 10 |
| state | 18 | 18 |