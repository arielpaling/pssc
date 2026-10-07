# Op-model findings from a second field model: triage and plan

Status: **in progress** (2026-10-07); rulings in section 4. Source: a list of 14 findings (`issues`, untracked
in the repo root), reported while projecting another field model through the
op-model targets. Each finding came with the workaround the model had used.

Every finding was re-run here against `8e030f6`, as a small model written fresh
for the purpose, on op-model-c, -cpp, -py and -sv (`trace_harness`). The
probes are not checked in. Each fix below adds its case to
`tests/progseq/test_op_model_construct_matrix.py`, with a hand-written trace.

## 1. Triage

| # | Finding (as reported) | Now | Fixed by / owner |
|---|---|---|---|
| 1 | `bit[256]` struct field emitted as `uint64_t` in C | **open, silent**, C and C++ (`std::uint64_t`); Python and SV are fine | W1 |
| 2 | struct and component with the same stem both `<x>_t` | fixed: types keep their PSS names | `a26ce7c` |
| 3 | `foo_status_e` and `foo_status_s` collide as `foo_status_t` | fixed, same cause | `a26ce7c` |
| 4 | a local `s` shadows the C handle `s` | fixed: the handle is `_self` | `7c59d71` |
| 5 | register struct returned by an exported function is private to the `.c` | **open**, C: the header names `st_s`, and only the `.c` defines it | W2 |
| 6 | no C lowering for `for`/`repeat`/`foreach`, part-select, `?:`, `match` on enum | part-select, `?:`, `match`, `foreach` fixed (`66ec5db`); **`repeat (N)` and `repeat (i : N)` still fail on C and C++** (`StmtFor`) | W4 |
| 7 | call into `child[i]` with a run-time index | fixed | `058fe82` |
| 8 | generated C has no array struct members | fixed in C and C++; **open in SV: an array field of a component is not declared** (`keys[1] = 5` -> "Can't find definition of variable") | W5 |
| 9 | `solve` functions other than `initialize` dropped from C | no longer silent: a call to one is **refused on every target** ("no function named 'setup'") | W6 |
| 10 | package-scope `import` functions not lowered by op-model-sv | fixed when called unqualified (`f(...)` after `import p_pkg::*`), on every target; **`p_pkg::f(...)` is refused on every target** | W7 |
| 11 | core `read32()` in a non-root component emitted bare in SV | fixed | — |
| 12 | `0xFFFFFFFF` emitted as signed decimal in SV | fixed: `64'd4294967295`, and compares agree on all four targets | `19ea04b` |
| 13 | `--yield import` emits `yield_()` without declaring it | **open**, C: `-Wimplicit-function-declaration` | W3 |
| 14 | `lock`, `select`, `event` collide with keywords | `lock` and `select` are PSS keywords, so the parser is right to refuse them; `event` works. **SV does not rename `new`** (a field `new` fails to parse), and SV keeps two keyword tables | W8 |

Six of the fourteen are fixed already, most by the D1-D9 work and the naming
rulings (`docs/design/op-model-field-defects-plan.md`). One of the eight still
open is silent (W1). Two are C compile failures (W2, W3), one is an SV compile
failure (W5), and the rest are refusals.

## 2. Work items, in order

Damage decides the order: silent first, then code that does not compile, then
code that is refused loudly.

**W1. Integers wider than 64 bits in C and C++ (silent).** DONE:
`targets/int_width.py` walks every field, the structs they reach, and each
function's parameters, result and body, and C and C++ refuse any integer wider
than 64 bits, naming each place. Found on the way, on every target: a
register wider than 64 bits was read and written with `read64`/`write64`,
half of it; the register walk (`reg_layout._mk`) now refuses it (LRM
21.14.5 a picks the primitive by size). Matrix cases: "a struct field wider
than 64 bits", "a local, parameter and result wider than 64 bits", "a register
wider than 64 bits". The field used to be
truncated to `uint64_t` with no diagnostic. Step one, in the same commit as
its test, is to refuse it: a `CompileError` naming the field and its width, as
soon as any C or C++ integer type is chosen for a width above 64
(`CIntSemantics` / the C type mapper). A second step was considered (Q1, declined):
represent a wide value as limbs (`uint32_t k[8]`), supporting
only what the reported model needs (store, copy, and a constant part-select
`k[31:0]`, which is how a key reaches 32-bit registers), and refuse arithmetic
on it.

**W2. Types a public signature names go in the header (C).** DONE:
`COpModelBackend.api_value_structs` now defaults to the layouts the header
names (public signatures, component fields, structs it declares) instead of
none. Matrix case: "register values an operation takes and returns". Planned:
A type an
exported operation takes or returns, at any depth (a struct field of struct
type), is declared in the header; today register value structs are always
private to the `.c`. One walk over the public signatures decides the set.
`--header-only` is unaffected. Test: the matrix case, plus a check that the
header compiles on its own.

**W3. `--yield import` declares `yield_()` (C).** DONE (`lower_imports`;
`c_names` reserves the name; test
`test_c_body_lowering.py::test_yield_import_is_declared_for_the_platform`).
Planned: it goes in the header's
import-API section, beside the other platform functions the model calls,
`void yield_(void);`. C++ already declares it. Test: compile the generated C
with `-Werror=implicit-function-declaration`, which the trace harness already
does with `-Werror`; add a matrix option so a case can set `--yield import`.

**W4. `repeat (N)` and `repeat (i : N)` in C and C++.** This was item 2 of the
earlier list. Add `stmt_for` to the C `_BodyEmitter` (C++ inherits it). The
counter is a local of the PSS index type, and the count is evaluated once
before the first iteration, so it is hoisted into a temporary when it is not a constant. A
`break`/`continue` already lowers. Matrix cases: a constant count, a count from
a field, and an index used in the body.

**W5. Array fields of a component in SV.** DONE: `_data_fields` takes a
fixed-size array of data, declared as an unpacked array member. Matrix case:
"array fields of a component". Planned: declare them as
SV unpacked arrays of the element type, initialized like scalars, and give
`foreach` over one the same rendering Python and C use. Test: the existing
matrix construct (`keys[1] = 5; raw[2] = 6;`), and a `foreach` over a field.

**W6. Calling a solve function other than the constructor.** Ruled (Q2): every `solve function` of a component is
lowered as a private operation of that component, on every target, so the
constructor and other solve functions can call it. A `target` function calling
one stays refused, since PSS forbids that call. Under C the operation is
`static`; under Python and SV it takes the `_pss_` prefix and is not exported.
Construction order does not change: the constructor runs its calls where it
makes them.

**W7. A package-qualified call to a package function.** `p_pkg::f(...)`
should resolve through the linker to the same declaration that `f(...)` after
`import p_pkg::*` reaches. The gate (`validate_calls`) and each emitter
currently classify by the written name. Per
`fix-at-source-no-pssc-workarounds`, the call should carry the linker's
resolution from ast2ir. If pssparser does not resolve a qualified call, that
gets filed there rather than patched by name lookup in pssc. Applies to
imports and to ordinary package functions alike, on every target.

**W8. One SV keyword table, complete.** DONE: `sv/keywords.py` (Annex B,
whole); each renderer keeps its rename spelling; op-model-sv now renames
locals too. Matrix case: "fields, a local and a parameter named as SV
keywords". The SV testbench's `buf` field is now `_zsp_buf`. Planned: `sv/context.py` and
`sv/lower_progseq.py` each keep an `_SV_KEYWORDS`, and they disagree in what
they rename to (`_zsp_<name>` against `<name>_`). Keep one table, holding the
whole IEEE 1800-2023 Annex B list (`new` is missing today), in one module, and
have both places use it. Locals are renamed by it too. Test: a field and a
local named after a sample of SV-only keywords, run on Verilator.

**W9. A struct's defaults wherever a value of it is (C, C++; silent).**
Found while checking W5, not in the report. A member's initial value
(`struct p_s { bit[8] y = 7; }`) reached only a component's struct field, one
level deep: a local, a nested struct, or an array element held 0 in C and C++
where Python and SV hold 7. DONE: C++ puts the default in the type (a default
member initializer), so every value has it; C has none, so a local is declared
with a designated initializer (`{.y = 7, .q = {.z = 5}}`) and a field is
assigned member by member, through nested structs and arrays
(`_default_init`, `_default_assigns`). An enum default was rendered as its
number, which C++ and SV reject; it is now the item. Matrix cases: "struct
defaults in a local and a nested struct", "struct defaults in array elements".
The cpp-virtual golden moves `wb_dma_ch_caps_s`'s defaults from the
constructor into the type.

**W11. Default values (C, C++, SV; silent).** Found with W9. A value with
no initializer has its type's default (LRM 7.5): an enum's FIRST item, which
need not be 0. C, C++ and SV started an enum at 0 (SV got a local right); C
also left a scalar local indeterminate. DONE: `progseq_model.enum_first_item`
is the one answer, stated in each language where zero-initialisation would
differ; a C scalar local is declared with its default. Matrix case: "default
values: an enum's first item, a bare local". C goldens: locals gain `= 0` and
`= WB_DMA_DONE`.

**W10. An array initializer (`array<bit[32], 3> v = {1, 2, 3};`).** Found
with W9: every target fails with an internal error (`ExprList` has no
rendering). Not scheduled; the minimum is a `CompileError`.

## 3. Carried over from the earlier list

These remain from `op-model-field-defects-plan.md` §0 and are not in this
report:

* C does not lower `super.initialize(...)` in a constructor (strict xfail in
  `test_op_model_inherit_native.py`).
* `get_offset_of_instance` is classified by name alone on non-register
  components (corpus `types.string.match.001`).
* Python: a sub-component with a constructor is `None` until that constructor
  is called. SV: init refuses assigning a sub-component's field.

## 4. Rulings (2026-10-07)

* **Q1 (W1): refusing is enough.** An integer wider than 64 bits in C or C++ is
  a `CompileError`; no limb representation. W1 has no step two.
* **Q2 (W6): yes.** Every solve function is a private operation of its
  component, callable from the constructor and other solve functions; a call
  from a target function stays refused.

## 5. Proposed commits

One per item, each with its matrix cases: W1 (refusal) -> W2 -> W3 -> W8 ->
W5 -> W9 -> W11 -> W4 -> W7 -> W6 -> W10. Then C `super.initialize`.
