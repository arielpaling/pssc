# Op-model defects from a field device-init model: test and fix plan

Status: **done** (2026-10-07); open follow-ups in section 0. Source: an external report with nine minimal
repros (D1-D9), found while generating a device-initialization operation model
at pssc `6def361`. This plan re-runs every repro against the current tree
(`ce70ec7`), on all four op-model targets rather than the two in the report,
and orders the work by the damage each defect causes.

The repros are not checked in, and are not copied into the tests. Each test
below is written fresh, from the PSS constructs involved, with no naming taken
from the model that found it.

## 0. Progress

| Fix | State |
|---|---|
| Tests: `trace_harness.py`, `test_op_model_construct_matrix.py` | done (`32a2a52`) |
| F1 (D1), with Q2: one base per group in C; unbound group refused (`group_binding.py`) | done |
| F5 (D5): offset functions evaluated (`progseq_model._OffsetEval`); bad ones are a `CompileError` | done |
| F6 (D6): enum base type carried by ast2ir; one width rule (`reg_field_resolve.field_width`) | done |
| F3 (D3): ast2ir keeps a select on a local/constant; `bit_select.py` reads it, every emitter renders it (read, and read-modify-write) | done |
| F4 (D4): one pattern reading (`body_walker.match_values`) | done |
| F7 (D7): `?:` in C and C++ | done |
| F2 (D2): C calls a sub-component's operation on its handle; a register path through sub-components in C, C++ and Python | done |
| F9 (D9): Python's integer machinery moved to `int_semantics.py`; C and C++ use it (`CIntSemantics`) | done |

All nine defects are fixed; the construct matrix has no xfail left.

Found on the way:

* **D4 was not about enum items.** ast2ir folds an enum item in a pattern to
  its constant. What C, C++ and Python could not lower was the `default:` arm
  (a wildcard `PatternAs`), so ANY `match` with a `default:` failed on all
  three. corpus `types.enum.match.001` now passes on op-model-py.
* **F9 also corrected the shipped example model's C and C++.** These were
  silent before, so the golden diff was reviewed line by line:
  * `read_descriptor_residual` returns `bit[12]` but returned 16 bits
    (`(uint16_t)desc_csr`); it now masks to 12.
  * C++ converted `bit` with `static_cast<bool>(v)`, which maps 2 to true;
    PSS truncation keeps bit 0 (`v & 0x1`).
  * Every other change is equivalent in value: a cast written as a mask, or a
    64-bit literal suffix.
* **C and C++ cannot lower `repeat (i : N)`** (`StmtFor`: "defines no
  stmt_for()"). Not in the report; not yet scheduled.
* **C, from running the inheritance cases (`test_op_model_inherit_native`)
  on it.** None of these is in the report, and none is scheduled yet:
  * **A sub-component is constructed only if its parent's constructor calls
    its `initialize`.** With no call, its field initializers never run:
    `int a = 5;` reads 0, so `virtual dispatch` prints `101 110 110` where
    PSS gives `106 117 117`. This one is SILENT. LRM 20.1.2 constructs every
    instance.
  * **A sub-component named `t` collides with the handle type.** The accessor
    `<prefix>_t()` redeclares the typedef `<prefix>_t`.
  * **`super.initialize(...)` in a constructor is not lowered.** It reports
    "no function named `_pss_super_base_c_initialize`".

T1d (a mutation check of the C base) is covered by history: the T1 cases were
strict xfails on C before F1 and pass after it.

## 1. Where each defect stood on 2026-10-07

`ok` = output checked by hand and correct. `rc=0 WRONG` = pssc exits 0 and the
output is wrong (the worst kind). `error` = pssc stops with an internal error
(a traceback, rc=2) rather than a located diagnostic.

| #  | Construct | op-model-c | op-model-cpp | op-model-py | op-model-sv |
|----|---|---|---|---|---|
| D1 | two register groups bound to different handles | **rc=0 WRONG** (one `self->base`; last `set_handle` wins) | ok (group built at its handle) | ok (`_pss_base_<group>`) | ok |
| D2 | operation calling a sub-component's operation; `s.a.R.write_val()` | error (`ExprAttribute` has no lowering) | call ok; `s.a.R` error | call ok; `s.a.R` error | ok |
| D3 | part select `x[31:12]`, bit select `x[3]` | error, or **rc=0 WRONG** | same | same | same |
| D4 | `match` with an enum-item pattern | error (`PatternAs`) | error | error | ok |
| D5 | `get_offset_of_instance` as an `if` chain | error (`OffsetFoldError`) | error | error | error |
| D6 | enum-typed field in a packed register struct | error (`DataTypeEnum` has no `bits`) | error | **rc=0 WRONG** (field laid out 0 bits wide; reads masked to the short struct width) | error |
| D7 | conditional `c ? a : b` | error (no `expr_if_exp`) | error | ok | ok |
| D8 | value-returning task call inside an expression (SV) | ok | ok | ok | ok (hoisted to `pssc_tmp_0` since the 2026-09-25 task-call lift) |
| D9 | integer widths (see below) | **rc=0 WRONG** | **rc=0 WRONG**; `~` on `bit` (a C++ `bool`) does not compile | ok | ok |

Notes:

* **op-model-sv now requires an exported API.** The report's repros export
  nothing, so on today's tree all nine stop with "exports nothing" before
  reaching the defect. The SV column above was run with
  `export target function go;` added. The tests below export explicitly.
* **D8 is fixed.** Keep a regression test (section 3, T8): the report's point
  was that pssc said yes and the SV compiler said no, so the test has to
  compile the output, not grep it.
* **D9 as reported is not a defect.** In `a.STS.write_val(~x)` with `bit x`,
  the call argument is an assignment-like context (LRM 8.7.2, Table 23). The
  32-bit parameter width propagates into `~x` and on to `x` (Table 22: "Unary
  arithmetic and bitwise operators: same as the operand; propagated to the
  operand"). So `x` is zero-extended to 32 bits and then inverted, and for
  `x == 1` the result is `0xFFFF_FFFE`. The report expected `0`. All four
  targets produce `0xFFFF_FFFE` there, which is correct.
* **D9 is real in other contexts.** Two cases are wrong in C and C++:
  * **Context narrower than `int`.** In `if ((~p) == q)` with `bit[4] p, q`,
    equality propagates 4 bits, so `p = 15, q = 0` is true. C emits
    `~(p) == q`, which promotes `p` to `int`, compares `-16` with `0`, and gets
    false. Python (`((~p) & 0xf) == q`) and SV are correct.
  * **Context wider than the operand's C type** (not yet confirmed by a run;
    T9 confirms it). Take `write64(~x)` with `bit[32] x`. PSS widens `x` to 64
    bits before inverting, giving `0xFFFF_FFFF_FFFF_FFFE`. C inverts a
    `uint32_t` and then widens it, giving `0x0000_0000_FFFF_FFFE`.

  The defect is not "mask `~` to its operand's width", which is what the
  report proposed and which would itself be wrong. The defect is that the
  C-family backends evaluate in C's integer-promotion width instead of the
  PSS context width.
* **D1 has a quieter twin in C and Python.** A group that no `set_handle`
  reaches is given the first handle parameter (`self->base = a_base;`, and
  Python's `self._pss_base_b = a_base`). Its registers then alias another
  group's. That also exits 0. Decided in section 5, Q2: it becomes a build-time error.

### 1.1 What the test matrix found beyond the report (2026-10-07)

Running the section 3 cases on all four targets turned up more than the
repros did:

* **D3 is silently wrong, not only unsupported.** ast2ir produces
  `ExprSubscript(x, ExprSlice(is_bit_slice=True))` for a select on a
  variable. `ExprTypes` has no rule for it, and every emitter's
  `expr_subscript` treats it as an array index.
  * A part select on a local is dropped: `c.base_lo = x[31:12]` writes the
    low 20 bits of `x`.
  * A part select as an assignment target replaces the whole field.
  * A bit select renders `x[3]`, which fails the C/C++ compile and is a
    `TypeError` in Python.
  * The report's form (a select on a parameter) reached a standalone
    `ExprSlice` and failed loudly.
* **D2 reaches C++ and Python too.** Writing a sub-component's register from
  the parent (`s.a.STS.write_val(1)`) fails `call_reg()` on C, C++ and Python.
  SV handles it.
* **D9 is confirmed in both directions.** C and C++ give
  `write64(~x)` = `0xFFFF_FFFE` for a 32-bit `x`, where PSS gives
  `0xFFFF_FFFF_FFFF_FFFE`. They also give `(bit[8])((~x) >> 1)` = `0xFF`,
  where PSS gives `0x7F`. C++ maps `bit` to `bool`, so `~x` on a `bit` does
  not compile under `-Werror`. Python and SV pass every width case.
* **D6 in Python also corrupts reads.** The value struct is 30 bits, so a
  read is masked to 30 bits.
* **Q4 is already enforced.** pssparser refuses an unsized enum in a packed
  struct, with its location. T6c pins this on all four targets.
* **Q2 meets an existing convention.** The bundled example model
  (`src/pssc/testing/models/dma_engine.pss`, a byte copy of
  `examples/export/programming_seqs`) has ONE register group and an empty
  `ctor(addr_handle_t base)`, documented as "the binding is the generator's
  job". C and Python bind that group to the first address parameter. The rule
  adopted keeps the convention where it cannot alias:
  * a component with exactly one register group and a constructor that binds
    none of them binds it implicitly to the first address parameter (today's
    behaviour);
  * every other unbound group is the T1e error: a constructor that binds some
    groups but not this one, or several groups with no binding at all.

## 2. Priorities

1. **Silent wrong output**: D1 (C), D6 (Python), D9 (C/C++). Each produces a
   model that builds clean and touches the wrong address or computes the wrong
   value. A golden snapshot would freeze the error (AGENTS.md: "Snapshots
   prove sameness, not correctness"), so each needs a behavioural test that
   runs the generated model.
2. **Blocks every model from a common register generator**: D5. Any register
   package written with `if` chains fails on all targets.
3. **Blocks the C target for hierarchical models**: D2.
4. **Missing lowering, loud failure**: D3, D4 (C/C++/py), D6 crash
   (C/C++/SV), D7 (C/C++). These work around easily, but each is an internal
   traceback where a user should see either working output or a located
   diagnostic.
5. **Regression only**: D8.

## 3. Tests

### 3.1 One cross-target trace harness

`test_op_model_inherit_native.py` already builds a model on py, cpp and SV
(SV under Verilator) and compares the message and memory-access traces. Move
its runners (`_run_py`, `_run_cpp`, `_run_sv` and the logging `mem` platform)
into `tests/progseq/trace_harness.py`, and add `_run_c` (gcc, the same
logging platform written against `pssc_mem_if`). Then every case below is one
PSS source, one entry call, and **one expected trace written by hand**,
checked on every target that is present. Writing the trace by hand is the
point: it states the address, and the test does not take the address from
any backend.

New file: `tests/progseq/test_op_model_construct_matrix.py`, parametrised
`(case, target)`. A target that does not support a case yet is marked
`xfail(strict=True)` with the defect ID, so a fix that makes it pass also has
to remove the marker. That is the same rule as `expected/*.toml` in the
compliance suite. SV cases carry the `sim` marker.

### 3.2 Cases

| Test | Model | Expected trace / outcome | Defect |
|---|---|---|---|
| T1a | two groups, two handles, `go()` writes one register in each | writes at `a_base+0x4` and `b_base+0x10` | D1 |
| T1b | one handle, second group at `make_handle_from_handle(h, 0x1000)` | second group's write at `h+0x1000+off` | D1 |
| T1c | three groups, `set_handle` in reverse declaration order | each at its own handle (catches a "first/last wins" fix) | D1 |
| T1e | a group no `set_handle` reaches | located build-time error on every target (Q2) | D1 |
| T1d | `test_op_model_behaviour_c.py`-style gate: the T1a C build with per-group bases collapsed back to one must FAIL the trace | (calibration, like `test_the_gate_fails_when_the_per_channel_base_is_dropped`) | D1 |
| T2a | `go()` calls `s.poke()` | sub-component's write at its base | D2 |
| T2b | `go()` writes `s.a.R` directly | same address as T2a | D2 |
| T2c | component array `s[2]`, `go()` calls `s[1].poke()` and loops `foreach` | per-element addresses | D2 |
| T2d | value-returning sub-op used in a condition | read then conditional write | D2 + D8 |
| T3a | `c.f = x[31:12]` rvalue, `x = 0xABCDE123` | written field = `0xABCDE` | D3 |
| T3b | bit select `x[3]` and `x[63:32]` on `bit[64]` | value, including the high half | D3 |
| T3c | part select and bit select as assignment targets, on a local and on a register value field | read-modify-write result; other bits preserved (Q3) | D3 |
| T4a | `match` on an enum with `[M1]`, `[M0, M2]`, `default` | each arm reached for its value | D4 |
| T4b | enum declared in a package, item written qualified | same | D4 |
| T5a | group whose `get_offset_of_instance` is an `if`/`else if` chain ending in `return 0xFFFF_FFFF_FFFF_FFFF` | registers at the chain's offsets | D5 |
| T5b | the same chain with offsets out of declaration order | non-dense, non-ordered addresses (the `offset_map` fallback would give dense ones) | D5 |
| T5c | `get_offset_of_instance_array` as a bare `return 0xFFFF_FFFF_FFFF_FFFF` in a group with no arrays | builds | D5 |
| T5d | a chain with no case for a declared register | located `OffsetFoldError` naming group and register | D5 |
| T5e | an offset function the folder cannot evaluate (say, one that calls a non-pure function) | located error, not a guess | D5 |
| T6a | `enum e : bit[2]` field between two `bit` fields in a packed struct | `read()`, then a field write of `e` lands in bits [2:1] | D6 |
| T6b | the manifest's value-struct layout for T6a | widths 1/2/29 summing to 32 | D6 |
| T6c | an enum with no explicit base type in a packed struct | located error on every target (Q4) | D6 |
| T7a | `write_val(c ? 0 : 1)` both ways | 0 / 1 | D7 |
| T7b | nested `?:`, and `?:` whose arm calls a value-returning operation | the right arm only, evaluated once (on SV the arm call is hoisted; check that the hoist does not evaluate both arms) | D7 + D8 |
| T8  | the D8 shape, compiled by Verilator | builds and produces the trace | D8 |
| T9a | `bit[4] p, q; if ((~p) == q)` with `p=15, q=0` | branch taken | D9 |
| T9b | `bit[32] x; write64(h, ~x)` with `x = 1` | `0xFFFF_FFFF_FFFF_FFFE` | D9 |
| T9c | `write_val(~x)` with `bit x = 1` (the report's case) | `0xFFFF_FFFE`: LRM 8.7.2, pinned so nobody "fixes" it | D9 |
| T9d | `bit[8] y = (~x) >> 1` with `bit[4] x = 1` | `0x7F` (C's arithmetic shift of `int -2` gives `0xFF`) | D9 |

D1, D2, D6 and D9 change register access or address computation, so their
fixes must also run `test_op_model_behaviour_c.py` and
`test_build_wb_dma_c.py` (AGENTS.md, golden snapshots).

## 4. Fixes

Order: T-cases land first as strict xfails (one commit), then each fix removes
its markers.

### F1. D1: one base per register group in C

* The C component struct gets one `pssc_addr_t` per register-group field,
  named through the style policy (`CStylePolicy`, default
  `base_<group>`), mirroring Python's `naming.group_base`.
* `set_handle` lowering (`c/lower_progseq.py` ~1519) assigns that group's
  base. The ctor prologue (~1724) stops writing a single `self->base`.
* Accessor address functions (`lower_reg_model`) read the group's base. The
  offset still comes from `reg_layout.collect_accessors`, so this adds no
  second offset walk; `RegAccessor` already knows its group.
* Golden snapshots change for any configuration with a register group. That
  regeneration is legitimate and goes in the same commit with the diff
  reviewed. Expect one renamed field per group and nothing else.
* Manifest: if it reports a component-level base today, report per-group
  bindings. That is adding a field, so no `VERSION` bump.

### F2. D2: calls into sub-components in C

* Add a `Disposition` for "operation of a sub-component instance" in
  `call_legality.py`, a `CallDispatch` hook for it
  (`test_dispatch_matches_registry` requires the pair), and
  `validate_calls.ops_for_call` resolution of `s.f` / `s[i].f` against the
  sub-component's declared type. Do NOT use a model-wide name set (AGENTS.md:
  that is how one component's `read32` captured another's).
* Lowering: `<prefix>_<sub>_<f>(&s->s, args)`, or `&s->s[i]`. The emitted
  per-sub API already exists.
* `call_reg()` accepts a register path that crosses sub-component instances,
  with the base taken from the owning sub-component's group (F1).

### F3. D3: part and bit selects, all targets

* An `expr_slice` hook in each `_BodyEmitter`. The type is unsigned with
  bit size `hi-lo+1` (LRM Table 21).
  * C/C++: `((x >> lo) & MASK)`, with the mask suffixed for 64 bits.
  * Python: `((x >> lo) & MASK)`.
  * SV: native `x[hi:lo]`.
* Bounds must be constant (8.5.x). A non-constant bound is a located error
  raised in the shared gate, not in each emitter.
* An lvalue part or bit select is a read-modify-write in C, C++ and Python,
  and native in SV (Q3).

### F4. D4: enum-item match patterns in C, C++ and Python

SV already maps a bare `PatternAs` (no sub-pattern) to the enum item it names
(`sv/lower_progseq.py:1394`). Hoist that into one language-neutral
`match_labels(pattern)` next to `body_walker` that returns IR values, and have
C, C++, Python and SV render the values. Four copies of the pattern switch is
how three of them came to be missing a case. A `PatternAs` that binds a name
stays a located refusal.

### F5. D5: fold offset functions by evaluating them

Replace the `match`-only reader in `progseq_model.scalar_offset` and
`array_base_stride` with a small evaluator over the pure function's IR. It
binds `name` to the instance-name literal (and the index parameter
symbolically, for the affine array form), runs `if`/`else if`/`match`/
`return` over constants, and returns a constant or an affine
`base + stride*i`.

* It is the one evaluator for both forms. `match` becomes just another
  statement it runs.
* Anything outside the subset (a call, a loop, a non-constant operand) raises
  `OffsetFoldError` naming the group, the function and the construct. It
  never falls back to `offset_map`: a fallback there is a dense layout and a
  wrong address.
* The `-1` / `0xFFFF_FFFF_FFFF_FFFF` sentinel means "no such instance". For
  a declared instance that is an error (T5d). For an `_array` function in a
  group with no arrays it is a correct answer (T5c).
* `reg_layout.collect_accessors` is unchanged: it already calls these two
  functions, so all targets pick up the fix at once. That is why offsets live
  in one walk.

### F6. D6: enum fields have the width of their base type

* One helper, `field_bits(dtype)`, in `reg_layout.py`, replaces the four
  `int(f.datatype.bits)` sites (`reg_layout.py:76`,
  `c/lower_reg_model.py:65`, `sv/lower_reg_model.py:79`, and the Python
  layout writer). An enum's width is its declared integer base type. Check
  whether ir-core's `DataTypeEnum` carries it. If not, the fix goes in
  ir-core and ast2ir (fix at the source), not in a pssc-side lookup by name.
* `field_bits` raises on a type it does not know. The Python
  zero-width layout is what a permissive `getattr(..., 0)` produces, and it
  must become impossible.
* Spelling of the field in each API (raw integer vs. the language's enum
  type) is a separate, later choice. First get the width right with the
  integer spelling.

### F7. D7: conditional expression in C and C++

An `expr_if_exp` hook: `((c) ? (a) : (b))`. Arm types follow LRM Table 22
(larger of the two, propagated), which F9's width handling supplies. A call
in an arm must not be hoisted out of the conditional unconditionally. SV
already handles this; check that its hoist keeps the arm conditional (T7b).

### F8. D8: no code change

Land T8.

### F9. D9: PSS integer widths in the C-family backends

The general fix is to evaluate in the PSS-propagated width, using the
`ExprTypes` information already used for literal typing (`int_literal_type`).

* Where the context width is narrower than C's promoted width, and the
  operator can set bits above it (`~`, unary `-`, `+`/`-`/`*`, `<<`), mask
  the result to the context width before it reaches a comparison, a shift
  right, a division or a modulo.
* Where the context width is wider than the operand's C type, cast the
  operand to the context type before the operator: `~(uint64_t)x`.
* Python already masks, so compare its rules with this one. The two should
  agree, and ideally share the width decision.

Land T9a-d first. C and C++ share the decision, and each backend only
renders it.

## 5. Decisions (2026-10-07)

* **Q1 (D1, C ABI): accepted.** Per-group base fields change the C struct
  layout of every generated component with register groups. That is fine at
  0.1.x with no deprecation step. Say so in the release notes.
* **Q2 (unbound group): build-time error.** A register group that no init
  path binds with `set_handle` is a located error ("group `b` of `dev_c` is
  never bound"). Today it silently takes the first handle parameter. The
  check runs in the language-neutral model build, so every target refuses the
  model the same way. Add case **T1e**: a model that never binds a group gets
  the located error on all four targets. Remove the C and Python
  "default to first handle" prologue lines. (Tentative: the build-time check
  sees only binding paths it can decide from the init bodies. If a real model
  binds a group conditionally, revisit with a run-time null-handle check.)
* **Q3 (lvalue slices): support.** `x[hi:lo] = v` and `x[i] = v` lower to a
  read-modify-write: `x = (x & ~(MASK << lo)) | ((v & MASK) << lo)` in C,
  C++ and Python, and native in SV. On a register value field this composes
  with the existing value-struct write. T3c expects the written value, not a
  refusal.
* **Q4 (unsized enum in a packed struct): refuse.** An enum with no integer
  base type has no width, so a packed struct cannot hold it. This is a located
  error naming the struct, the field and the enum. T6c expects it on all
  targets. `field_bits` raises it; no target defaults a width.
* **Q5 (D9): PSS semantics.** Generated code evaluates every integer
  expression at its PSS bit width (LRM 8.7, Tables 21-23), not at the host
  language's promoted width. The report's case (T9c, `0xFFFF_FFFE`) is
  correct under that rule and stays pinned. T9a, T9b and T9d define the
  required behaviour for F9.

## 6. Out of scope (noted in the report as a constraint, not a defect)

SV init lowering builds a sub-component array only inside a `foreach` whose
arguments are affine in the index (`lower_init._foreach`). Per-element handles
at irregular addresses cannot be passed, so users fall back to named scalar
sub-components. This is worth its own design note: it probably wants
`foreach` over a value-list literal of handles. It is not part of this plan.

## 7. Commands

```
# reproduce the matrix (SV needs `export target function go;` in dev_c)
PYTHONPATH=src:packages/zuspec-ir-core/src packages/python/bin/python -m pssc \
    compile -t op-model-{c,cpp,py,sv} --root dev_c [--prefix dev] regs.pss dN.pss -o OUT

direnv exec . packages/python/bin/python -m pytest tests/progseq/test_op_model_construct_matrix.py
direnv exec . packages/python/bin/python -m pytest tests/progseq -k "golden or behaviour or wb_dma"
```
