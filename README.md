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

Assertions may wrap the single entrypoint call in primitive operations, e.g.
`assert 3 == add(1, 2)`, `assert add(1, 2) in [1, 2, 3]`, `assert sorted(f([3, 1, 2])) == [1, 2, 3]`,
`len(...)`, indexing/slicing, arithmetic/bitwise ops, chained comparisons, `and`/`or`/`not`,
`min`/`max`/`sum`/`sorted(key=, reverse=)`/`set`/`list`/`tuple`/`dict`/`Counter`/`str`/`int`/`float`/`round`/`abs`,
`isinstance`, common `math.*` functions and str/list/dict/set methods. Constant sub-expressions are folded
at generation time (also usable as call arguments, e.g. `f(list(range(5)))`).
Names may be bound (`r = f(x)` / `a, b = f(x)` / `xs = [1, 2]`) and used by later asserts in the same scope.
Each test must trace to exactly one entrypoint invocation; multiple calls in one test are rejected.

Loops (`for`, incl. `enumerate`/`range(len(...))`/`zip`/`.items()`, and `while` with `i += 1`) are unrolled at
generation time. Each loop statement is a test (`tests.py:<line>`, plus enclosing iteration indices when nested)
that passes iff it iterates at least once (zero iterations or an unevaluable iterable fail, body skipped).
Assertions in the body get IDs `tests.py:<line>:<i>` (nested: `tests.py:<line>:<outer>:<inner>`) and must each
trace to exactly one entrypoint call. `*args`/`**kwargs` unpacking of constants, constant `if`, `break`/`continue`
are supported.
