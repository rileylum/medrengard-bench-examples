# babel_code_goat

```
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python babel_code_goat.py generate <tests_dir> --entrypoint <name> --lang python|javascript|typescript
python babel_code_goat.py test <solution> <tests_dir> --lang python|javascript|typescript [--timeout SECONDS] [--tol FLOAT]
```

JavaScript/TypeScript testers require Node.js (TypeScript uses Node's built-in type stripping, Node >= 22.6).

Supported in `tests.py` values: any hashable dict keys, `set`/`frozenset`, `collections.Counter`/`deque`/`defaultdict`/`OrderedDict`, `decimal.Decimal`.
Float comparisons use an absolute tolerance (`--tol`, default `1e-9`), applied recursively; integers compare exactly.
Per-assert overrides: `math.isclose(..., rel_tol=, abs_tol=)`, `abs(a - b) < tol` / `<= tol`.
Typed raises: `except T [as e]:` with `assert "s" in str(e)`, `str(e) == "s"`, `re.search/match/fullmatch(p, str(e)[, flags])`.
