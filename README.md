# LogQL Part 1

`python logql.py --log-file logs.ndjson --query 'SELECT a, b.c FROM logs WHERE x >= 1' [--output out.json]`

No third-party dependencies (`requirements.txt` is empty); standard library only.
Errors are written to stderr as `{"error": {"code": "...", "message": "...", "position": N?}}` with exit code 1.
Codes: `E_ARGS`, `E_PARSE`, `E_UNKNOWN_TABLE`, `E_DUPLICATE_FIELD`, `E_IO`, `E_INPUT`.
