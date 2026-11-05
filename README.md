# LogQL (Parts 1 & 2)

`python logql.py --log-file logs.ndjson --query 'SELECT service, COUNT(*) AS n, AVG(latency_ms) FROM logs WHERE x >= 1 GROUP BY service' [--output out.json]`

No third-party dependencies (`requirements.txt` is empty); standard library only.
Errors are written to stderr as `{"error": {"code": "...", "message": "...", "position": N?}}` with exit code 1.
Codes: `E_ARGS`, `E_PARSE`, `E_UNKNOWN_TABLE`, `E_DUPLICATE_FIELD`, `E_SEMANTIC`, `E_IO`, `E_INPUT`.

Part 2 adds `AS` aliases, `GROUP BY`, and aggregates `COUNT(*)`, `COUNT/SUM/AVG(AVERAGE)/MIN/MAX/UNIQUE(field)`.
`AS`, `GROUP`, `BY` and function names are matched case-insensitively in context (not reserved words).
