# babel_code_goat

```
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python babel_code_goat.py generate <tests_dir> --entrypoint <name> --lang python|javascript|typescript|cpp|rust
python babel_code_goat.py test <solution> <tests_dir> --lang python|javascript|typescript|cpp|rust [--timeout SECONDS] [--tol FLOAT]
```

JavaScript/TypeScript testers require Node.js (TypeScript uses Node's built-in type stripping, Node >= 22.6).

C++ (`tester.cpp`, needs `g++`/`clang++`, C++17+; override with `$CXX`) and Rust (`tester.rs`, needs `rustc` 1.70+):
`test` errors if the generated tester is missing. It splices the solution file into the tester, compiles it, and
runs one native process per entrypoint call, so all assertion semantics match the Python target. Arguments are
converted to the solution's declared parameter types and results back to Python values, with `None` <-> `std::optional`/
`std::nullopt` / `Option`. Supported C++ types: integers, `bool`, `char`, `float`/`double`/`long double`
(Decimal -> `long double`), `std::string`(`_view`), `optional`, `vector`/`list`/`deque`/`array`, `set`/`unordered_set`/
`multiset`, `map`/`unordered_map`, `pair`/`tuple`, `variant`, smart pointers. Rust: integers, `f32`/`f64`, `bool`, `char`,
`String`/`&str`, `Option`, `Vec`/`&[T]`/`VecDeque`/arrays, `HashSet`/`BTreeSet`, `HashMap`/`BTreeMap`, tuples, `Box`,
`Result` (an `Err` counts as a raised exception). Non-const `T&`/`T*` (C++) and `&mut T` (Rust) arguments are read
back after the call (mutation tests). Entrypoints may be free functions, class members (`Solution.method` or found
automatically; snake_case/camelCase variants are tried), keyword arguments are mapped by parameter name, and C++
default arguments work. C++ exceptions and Rust panics match any expected exception type; message checks use
`what()` / the panic message. The tester provides all standard headers plus `using namespace std` (C++), and a
prelude of common `std::collections`/`cmp` items (Rust). Its `HashMap` also accepts owned keys in lookups
(`get_mut(String::from("items"))`); `use std::collections::HashMap` in the solution resolves to it.

Supported test-file values: any hashable dict keys, `set`/`frozenset`, `collections.Counter`/`deque`/`defaultdict`/`OrderedDict`, `decimal.Decimal`.
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

Discovery: every `.py` file under `<tests_dir>` (recursively; hidden dirs, `__pycache__` and the generated
`tester.*` at the root are skipped) is parsed. Test IDs are `<relative/path.py>:<line>` (`#k` for several on one
line). Non-`.py` files with test-like names (`test*`, `*_test`, `tests`, `*_tests`) or no tests at all are
discovery errors.

Mutation-style tests: `f(a)` (statement) or `r = f(a)` immediately followed by asserts; inside that assert block,
names passed to the call refer to the arguments after the call (each assert re-runs the call on fresh copies).
For the statement form, it must be followed by at least one assert, and every assert must reference a passed
variable and must not call the entrypoint; otherwise discovery fails.
