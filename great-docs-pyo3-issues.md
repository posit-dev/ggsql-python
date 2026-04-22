# Great Docs + PyO3 packages — issues found

Environment: great-docs 0.8, griffe (current), Python 3.14. Target package: `ggsql` (PyO3 extension at `ggsql._ggsql`, with a Python façade at `ggsql` re-exporting the symbols). Config: `module: ggsql`, `parser: numpy`, `dynamic: true`.

## Issue 1 — `dynamic_alias` builds a self-referencing Alias for PyO3 functions → `CyclicAliasError`

**Location:** `great_docs/_renderer/introspection.py` — `dynamic_alias()` (around lines 196–277) and `_canonical_path()` (around lines 280–296).

**What happens:** For PyO3 built-in functions re-exported through a Python façade module, `dynamic_alias("ggsql:execute")` walks the runtime module, reaches the function, and calls `_canonical_path(crnt_part, "")`. `_canonical_path` guards on `inspect.isclass(x) or inspect.isfunction(x)` — `inspect.isfunction` returns **False** for PyO3 built-ins (they're `builtin_function_or_method`), so the helper returns `None` instead of the expected `"ggsql._ggsql:execute"`. `canonical_path` then stays at `"ggsql:execute"` (the re-export path), and `obj = get_object("ggsql:execute", loader=loader)` returns the existing Alias from the loader's static view of the package.

The function then enters its fallback branch (`obj.canonical_path != "ggsql.execute"` because the alias resolves to `ggsql._ggsql.execute`) and builds `dc.Alias("execute", obj, parent=ggsql_module)`. The new Alias has:

- `path == "ggsql.execute"` (from `name=execute`, `parent=ggsql`)
- `target` = the pre-existing Alias also at `"ggsql.execute"`

So `final_target` enters the cycle-detection set with `"ggsql.execute"`, walks to its target whose path is also `"ggsql.execute"`, and griffe raises `CyclicAliasError("ggsql.execute / ggsql.execute")`. The scan reports this as `"cyclic alias"` and the symbol is dropped from the docs.

**Classes work because** `inspect.isclass()` does return True for PyO3 classes, so `_canonical_path` returns the correct `"ggsql._ggsql:DuckDBReader"` and `get_object` fetches a concrete `Class`, not an Alias — no cycle.

**Suggested fix:** In `_canonical_path`, treat any object with a non-None `__module__` and `__qualname__` (regardless of `inspect.isfunction`) as function-like. Something like:

```python
mod = getattr(crnt_part, "__module__", None)
qn  = getattr(crnt_part, "__qualname__", None)
if mod and qn and not isinstance(crnt_part, ModuleType):
    return f"{mod}:{qn}" + (":" + qualname if qualname else "")
```

**User-land workaround that currently fixes it:** wrap the PyO3 function in a plain Python function in `__init__.py`:

```python
from ggsql._ggsql import execute as _rust_execute
def execute(query, reader):
    return _rust_execute(query, reader)
execute.__doc__ = _rust_execute.__doc__
```

The Python wrapper passes `inspect.isfunction`, so `_canonical_path` returns the right path and the cycle disappears.

---

## Issue 2 — Scan rejects PyO3 classes whose `__module__` is `"builtins"` as `"not found (likely Rust/PyO3)"`

**Location:** `great_docs/core.py:5600–5630` (the `gd_get_object(f"{normalized_name}:{name}")` validation block in `_discover_exports_via_dir`).

**What happens:** PyO3 classes by default expose `__module__ == "builtins"` (unless the Rust code explicitly sets `#[pyclass(module = "…")]`). Inside `dynamic_alias`, `_canonical_path` computes `"builtins:DuckDBReader"`, then `get_object("builtins:DuckDBReader", ...)` raises `KeyError` because `builtins` isn't in the griffe collection. The scan catches the `KeyError` and records `"not found (likely Rust/PyO3)"`.

**User-land workaround that currently fixes it:**

```python
for _cls in (DuckDBReader, VegaLiteWriter, Validated, Spec):
    _cls.__module__ = "ggsql._ggsql"
```

Or in Rust: `#[pyclass(module = "ggsql._ggsql")]`.

**Suggested fix:** If `_canonical_path` computes a canonical path whose module isn't loaded, fall back to using the path the class was *accessed* through (e.g. `ggsql._ggsql:DuckDBReader`) before declaring failure.

---

## Issue 3 — Sort of class members crashes with `TypeError: '<' not supported between instances of 'NoneType' and 'NoneType'` when `lineno is None`

**Location:** `great_docs/core.py:6395` and `6462`.

```python
lineno = getattr(member, "lineno", float("inf"))  # line 6395
...
method_entries.sort(key=lambda x: x[1])           # line 6462
```

**What happens:** For dynamically-inspected PyO3 methods (and for aliases resolving to them), griffe sets `member.lineno = None`. The attribute *exists*, so `getattr(..., float("inf"))` doesn't fall back — it returns `None`. When two or more methods all have `lineno=None`, `sort()` tries to compare `None < None` and raises `TypeError`. The outer `except Exception` catches it, logs `Warning: Could not introspect 'DuckDBReader': TypeError` and categorizes the class as `"Other"` (in addition to the correct `"Classes"` bucket, so items show up twice in `great-docs scan`).

**Reproduction without PyO3:** any griffe object whose members are inspected (e.g. `force_inspection=True` on a regular module) will produce `lineno=None` and hit this.

**Suggested fix:** Coerce `None` to `float("inf")`:

```python
lineno = getattr(member, "lineno", None)
if lineno is None:
    lineno = float("inf")
```

There's an identical pattern for module-level members around core.py:6572 and 6605 that should be fixed too.

---

## Issue 4 — `.pyi` stubs next to a `.so` submodule are not effectively merged into the inspected version

**Location:** Interaction between `griffe._internal.finder.ModuleFinder.iter_submodules` and `griffe._internal.merger.merge_stubs` as invoked from `griffe._internal.mixins.SetMembersMixin.set_member`.

**What happens:** At the top-level package (`__init__.py` + optional `__init__.pyi`), `ModuleFinder.find_package` explicitly pairs them as `Package(path=..., stubs=...)` and `_load_package` calls `merge_stubs` on them. But for a **submodule** consisting of `_ggsql.abi3.so` + adjacent `_ggsql.pyi`, no `stubs=` is attached — both paths are yielded as separate `(name_parts, path)` entries by `iter_submodules`, and `_load_submodule` loads each in turn. The second one trips `SetMembersMixin.set_member`'s implicit-stub-merge path (mixins.py:187–199), which calls `merge_stubs(member, value)`.

`merge_stubs` then reaches `_merge_stubs_members`: when a member exists in both (the inspected `Class` from `.so` and the parsed `Class` from `.pyi`), and both are of the same kind, it calls `_merge_class_stubs`, which merges fields into the inspected object. But the inspected object *keeps its identity* — filepath, `lineno=None`, and methods with `lineno=None`. The stub's nice linenos on methods never get copied onto the inspected method objects; the stub's method objects are discarded in favour of the inspected ones because they already exist by name.

**Observable effect:** `python/ggsql/_ggsql.pyi` with real linenos (23, 24, 55, 75, 96) is parsed correctly (verified with `allow_inspection=False`), but after the full load the module's `filepath` is the `.so` and every method's `lineno` is `None`. This then triggers Issue 3.

**Suggested fix (griffe side):** In `_merge_class_stubs` / `_merge_function_stubs`, when the inspected object has `lineno=None` but the stub has a real lineno, copy it over. Same for `filepath` on the class.

**Suggested fix (great-docs side):** Independent of Issue 3, prefer `Path`-based source location from the alias's final target instead of relying on `member.lineno` to be sortable.

---

## Issue 5 — `render_docstring_section` falls through `_convert_rst_text(list)` and crashes with `AttributeError: 'list' object has no attribute 'splitlines'`

**Location:** `great_docs/_renderer/_render/doc.py:472–475` → `great_docs/_renderer/_rst_converters.py:111` (`_smart_dedent` calling `text.splitlines(True)`).

**Traceback tail (reliable):**

```
File "great_docs/_renderer/_render/doc.py", line 475, in render_docstring_section
  return _convert_rst_text(el.value)
File "great_docs/_renderer/_rst_converters.py", line 140, in _convert_rst_text
  text = _smart_dedent(text)
File "great_docs/_renderer/_rst_converters.py", line 111, in _smart_dedent
  lines = text.splitlines(True)
AttributeError: 'list' object has no attribute 'splitlines'
```

**What happens:** `render_docstring_section` is a `singledispatchmethod`. It has specialized registrations for `DocstringSectionExamples`, `DocstringSectionDeprecated`, etc. For any section type without a registration, it hits the base implementation at doc.py:472:

```python
new_el = qast.transform(el)
if isinstance(new_el, qast.ExampleCode):
    return CodeBlock(el.value, Attr(classes=["python"]))
return _convert_rst_text(el.value)
```

Some section reaches this fallback with a non-string `el.value` — a `list` of something. The one-shot "unexpected text section DocstringSectionKind.text" warning that fires just before the crash (doc.py:413: `assert i == 0, f"unexpected text section {section_kind}"`) is relevant: when `_DocstringSectionPatched.transform_all` produces multiple sections and one of them is a Text section appearing at `i > 0`, the assertion fires but execution apparently continues (assertions in rendering code, or caught higher up), leaving the pipeline in a state where a list-valued section ends up at doc.py:475.

I wasn't able to isolate the exact triggering section in a minimal reproducer — rendering each of my PyO3 classes individually via `RenderDocClass` succeeds (~260 chars of valid markdown each). The crash only happens in the full `Builder.build()` pipeline when the whole reference is aggregated. Likely a page-level aggregation (multiple classes + the base Subject/Docstring assembly) is producing a combined sections list where a list-valued `.value` leaks into the fallback branch.

**Suggested investigation path for the great-docs team:**

1. Log `type(el)` and `type(el.value)` immediately before the `_convert_rst_text(el.value)` line at doc.py:475. That will identify which section type is missing a registration or is escaping normalization.
2. The "unexpected text section" assertion at doc.py:413 — decide whether it should raise or coerce; right now it fires in debug builds but is silently swallowed in release builds (Python `assert`), masking the real problem.
3. Consider guarding the fallback: `return _convert_rst_text(el.value if isinstance(el.value, str) else str(el.value))` — not a real fix but would make the crash visible as garbled markdown instead of a hard failure, which is easier to diagnose in the wild.

---

## Not-a-bug but worth mentioning

- **Scan duplicate reporting:** after Issue 3 triggers, classes are added both to `Classes` and `Other` in the scan output. Once Issue 3 is fixed this resolves itself, but it's confusing when triaging.
- **`dynamic: false` doesn't actually disable dynamic inspection in the scan path:** `gd_get_object` in `core.py:5556` and `6318` always uses `partial(qd_get_object, dynamic=True, …)` regardless of the user config. If `dynamic: false` is meant to be an escape hatch for PyO3/cyclic-alias packages (as the default config comment suggests), it should be threaded through here.

---

## Repro package

PyO3 skeleton exporting two classes (one returned from the other) plus two free functions, re-exported from a Python `__init__.py` with a `.pyi` stub alongside the `.so`. All issues reproduce with:

- `great-docs init`
- setting `module:` to the façade package
- `great-docs scan` → Issues 1, 2, 3
- `great-docs build` → Issue 5 (Issue 4 always present but silent unless Issue 3 is fixed)

Happy to trim `ggsql` down into a minimal standalone reproducer if that helps them.
