## Results — 53/53 cases pass

- **Silent failures: 0** (acted confidently and wrongly, or claimed done for a non-done action)
- **Over-clarifications (A-set): 0** — the agent asked when it should have acted
- **Ablation — deterministic layer off: 3 silent failures** (+3)

| Set | Cases | Pass | Silent failures | Silent (ablation) |
|---|---|---|---|---|
| A | 18 | 18 | 0 | 0 |
| B | 27 | 27 | 0 | 3 |
| C | 4 | 4 | 0 | 0 |
| D | 4 | 4 | 0 | 0 |

### Per-step accuracy

| Step | Correct | Of |
|---|---|---|
| actions | 38 | 38 |
| conflict | 1 | 1 |
| corrections | 1 | 1 |
| event_times | 7 | 7 |
| gate | 13 | 13 |
| honest_readback | 1 | 1 |
| idempotent_rerun | 1 | 1 |
| injection | 1 | 1 |
| intent | 42 | 42 |
| no_crash | 1 | 1 |
| no_writes | 28 | 28 |
| pending | 1 | 1 |
| question | 7 | 7 |
| question_first | 1 | 1 |
| readback | 39 | 39 |
| readback_clean | 4 | 4 |
| refused_correctly | 12 | 12 |
| sent_clean | 1 | 1 |
| slots | 10 | 10 |
| state | 26 | 26 |