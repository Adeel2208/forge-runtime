# coding - evaluation report

- case set: `1.0.0` (`cases\coding.yaml`)
- target: `coding` @ `forge/0.6.0 ollama/qwen3:8b`
- harness: `0.6.0` · seed `1729` · python `3.11.0`
- started: 2026-08-27T08:16:10+00:00

**5 / 6 passed** (83% of 6 judged cases, 6 executed)

| Outcome | Count | Means |
|---|---:|---|
| `PASSED` | 5 | every assertion held |
| `ASSERTION_FAILED` | 1 | the target ran and was wrong |

## Failed assertions

### `code.two-file-change`

> Add a function subtract(a, b) to src/calc.py returning a - b, and add a test named test_subtract to tests/test_calc.py that asserts subtract(5, 3) == 2.

- **PASS** `file_contains` — src/calc.py contains 'def subtract'
- **FAIL** `file_contains` — tests/test_calc.py does not contain 'def test_subtract'
- **FAIL** `files_changed` — never changed ['tests/test_calc.py']


---

Records: `records.jsonl` · Manifest: `manifest.json`. A result is interpretable only as (case-set version x target version).
