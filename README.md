# LogQL (Parts 1–3)

`python logql.py --log-file logs.ndjson --query 'SELECT service, COUNT(*) AS n, AVG(latency_ms) FROM logs WHERE x >= 1 GROUP BY service' [--output out.json]`

`python logql.py --source a=a.ndjson --source b=b.ndjson --query 'SELECT a.*, b.status FROM a CONFLATE PRESERVING LEFT b UPON a.req = b.req'`

No third-party dependencies (`requirements.txt` is empty); standard library only.
Errors are written to stderr as `{"error": {"code": "...", "message": "...", "position": N?}}` with exit code 1.
Codes: `E_ARGS`, `E_PARSE`, `E_UNKNOWN_TABLE`, `E_DUPLICATE_FIELD`, `E_SEMANTIC`, `E_IO`, `E_INPUT`.

Part 2 adds `AS` aliases, `GROUP BY`, and aggregates `COUNT(*)`, `COUNT/SUM/AVG(AVERAGE)/MIN/MAX/UNIQUE(field)`.
`AS`, `GROUP`, `BY` and function names are matched case-insensitively in context (not reserved words).

Part 3 adds `--source alias=path` (repeatable; `--log-file p` ≡ `--source logs=p`) and
`CONFLATE [INTERSECTING | PRESERVING LEFT|RIGHT|BOTH] <alias> UPON x.f = y.g [AND ...]`.
Notes on choices where the spec is ambiguous:
- WHERE is applied after all conflations (SQL semantics); it is also accepted after the CONFLATE clauses.
- Row ordering follows the spec examples: LEFT emits unmatched anchor rows in place; RIGHT emits
  matched pairs in anchor order, then unmatched right rows; BOTH emits matched pairs in conflate-file
  order (anchor order within each), then unmatched left rows, then unmatched right rows.
- `alias.*` expands to the union of top-level keys across that source, in first-appearance order.
- Without CONFLATE, a leading `<from-alias>.` on a field path is treated as a qualifier.
- Unknown aliases in FROM/CONFLATE → `E_UNKNOWN_TABLE`; unqualified/out-of-scope refs → `E_SEMANTIC`.
