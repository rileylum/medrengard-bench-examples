# LogQL (Parts 1–4)

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

Part 4 adds `GLOSS [STRICT] { name := x.a | y.b ... [DEFAULT lit], ... }` and `CANON.<name>` references
(SELECT, GROUP BY, aggregates, WHERE, UPON). Notes:
- GLOSS is accepted after WHERE / before or after the CONFLATE clauses (once). Candidates must be
  qualified with an alias of this query (`E_SEMANTIC` otherwise); redeclaration → `E_SEMANTIC`;
  `CANON.*` → `E_PARSE`; undeclared label → `E_SEMANTIC`.
- A label resolves to the first non-null candidate, else DEFAULT (null if absent). Candidates on
  aliases that are null-padded or not yet joined count as null.
- STRICT: two non-null candidates that are not deep-equal → stderr
  `{"error": "LOGQL_ERROR: conflict in CANON.<name>", "code": "E_RUNTIME"}` (flat shape, as in the spec),
  raised when the label is evaluated.
- WHERE may use a label only if all its candidates are on the FROM alias.
- In UPON each operand is evaluated on one side of the join: plain refs by their alias; a CANON
  whose candidates are all on one side takes that side; a mixed one takes the side opposite its
  partner (left/right by position when both are mixed) and uses only the candidates visible on
  that side. Null matches null, as in Part 3.
- `INTERSECTING` remains an inner join (Part 3 semantics); spec example 4 shows LEFT-join output for it.
