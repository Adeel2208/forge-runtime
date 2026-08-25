# FORGE failure-injection report

- runtime version: `0.1.0`
- seed: `1729` (every trial is reproducible)
- generated: 2026-08-25T18:21:51+00:00
- trials: 63
- **total spend: $0.00**

| Injected failure | Trials | Task success | Recovered | Contained | Dup effects | +Latency (ms) | +Tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| `none` | 9 | 100% | 100% | 100% | 0 | +0.0 | +0.0 |
| `llm_timeout` | 9 | 100% | 100% | 100% | 0 | +142.4 | +0.0 |
| `malformed_output` | 9 | 100% | 100% | 100% | 0 | +124.7 | +160.0 |
| `policy_denial` | 9 | 100% | 100% | 100% | 0 | -36.3 | +0.0 |
| `repeated_action_loop` | 9 | 33% | 33% | 100% | 0 | +288.0 | +213.3 |
| `tool_timeout` | 9 | 100% | 100% | 100% | 0 | +149.1 | +0.0 |
| `worker_crash` | 9 | 100% | 100% | 100% | 0 | +100.7 | +106.7 |

**Reading this table.**

- **Recovered** - the run reached `COMPLETED` despite the fault.
- **Contained** - the runtime responded *correctly*, which is not always the same thing. For `repeated_action_loop` the correct response is a deliberate halt, so a bounded run counts as contained but not recovered. Both columns are reported because either alone would flatter the system.
- **Dup effects** - external effects observed more than once. This is the column that must read `0` for crash-resume to be safe; it is the whole point of the idempotency machinery.
- Latency and token deltas are measured against the `none` control arm, which is why that row is always `+0.0`.
