"""PSS constructs in operation bodies, held to one trace on every op-model target.

Each case is a small model and the memory-access trace its ``run()`` must
produce, worked out by hand from the LRM -- never taken from a backend, since
an address a backend computed proves only that the backends agree. Every case
runs on op-model-c, -cpp, -py and -sv (`trace_harness`).

A case a target does not handle yet is a STRICT xfail naming its defect, so a
fix that makes it pass has to remove the marker too. The defects and their
fixes are in docs/design/op-model-field-defects-plan.md (D1-D9).

Integer expressions are evaluated at their PSS bit width (LRM 8.7), not at the
host language's: a call argument and an assignment are assignment-like
contexts (8.7.2) whose target width propagates into the expression, and
otherwise operands are propagated per Table 22. The D9 cases pin both
directions.
"""
from __future__ import annotations

import json
import re
from typing import Dict, NamedTuple, Optional, Sequence

import pytest

from pssc.driver import CompileError

from . import trace_harness as th
from .conftest import diagnostic_text

#: Register groups the cases share. `ga_c` puts CTL at 0x0 and STS at 0x4;
#: `gb_c` puts CTL at 0x10, so a group bound at the wrong base shows up as a
#: wrong address rather than a coincidentally right one.
_REGS = """
struct ctl_s : packed_s<> { bit[1] en; bit[11] rsvd; bit[20] base_lo; }
pure component ga_c : reg_group_c {
  reg_c<bit[32], READWRITE, 32> CTL;
  reg_c<bit[32], READWRITE, 32> STS;
  function bit[64] get_offset_of_instance(string name) {
    match (name) { ["CTL"]: return 0x0; ["STS"]: return 0x4; }
    return 0xFFFFFFFFFFFFFFFF;
  }
}
pure component gb_c : reg_group_c {
  reg_c<bit[32], READWRITE, 32> CTL;
  function bit[64] get_offset_of_instance(string name) {
    match (name) { ["CTL"]: return 0x10; }
    return 0xFFFFFFFFFFFFFFFF;
  }
}
pure component gs_c : reg_group_c {
  reg_c<ctl_s, READWRITE, 32> CTL;
  function bit[64] get_offset_of_instance(string name) {
    match (name) { ["CTL"]: return 0x0; }
    return 0xFFFFFFFFFFFFFFFF;
  }
}
pure component g64_c : reg_group_c {
  reg_c<bit[64], READWRITE, 64> W;
  function bit[64] get_offset_of_instance(string name) {
    match (name) { ["W"]: return 0x0; }
    return 0xFFFFFFFFFFFFFFFF;
  }
}
"""

#: The root binds one `ga_c` at its one handle; most cases need no more.
_ONE = """
  ga_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
"""


class Refused(NamedTuple):
    """The model is refused as a user error whose text matches every pattern."""
    patterns: Sequence[str]


class Case(NamedTuple):
    pss: str
    want: object                         # trace lines, or Refused
    args: Sequence[int] = (0x1000,)
    mem: Optional[Dict[int, int]] = None
    xfail: Dict[str, str] = {}           # target -> defect it is waiting on



_ALL = ("c", "cpp", "py", "sv")


def _on(targets, why):
    return {t: why for t in targets}


#: Python builds a sub-component whose type has a constructor AT its
#: `initialize` call (`_sub_storage`), and it is `None` until then.
_PY_LAZY_SUB = "py: a sub-component with a constructor is None until called"
#: SV's init lowering (`sv/lower_init.py`) refuses an assignment to a field
#: of a sub-component.
_SV_INIT_SUB_FIELD = "sv: init refuses assigning a sub-component's field"


CASES = {
    # --- D1: each register group at its own handle ---------------------------
    "two groups at two handles": Case("""
component pss_top {
  ga_c a;
  gb_c b;
  solve function void initialize(addr_handle_t a_base, addr_handle_t b_base) {
    a.set_handle(a_base);
    b.set_handle(b_base);
  }
  target function void run() { a.STS.write_val(1); b.CTL.write_val(2); }
}""", ["write 32 0x1004 0x1", "write 32 0x2010 0x2"],
        args=(0x1000, 0x2000)),

    "a group at a handle made from another": Case("""
component pss_top {
  ga_c a;
  gb_c b;
  solve function void initialize(addr_handle_t base) {
    a.set_handle(base);
    b.set_handle(make_handle_from_handle(base, 0x1000));
  }
  target function void run() { a.STS.write_val(1); b.CTL.write_val(2); }
}""", ["write 32 0x1004 0x1", "write 32 0x2010 0x2"]),

    "three groups bound in reverse order": Case("""
component pss_top {
  ga_c a;
  ga_c b;
  gb_c c;
  solve function void initialize(addr_handle_t ha, addr_handle_t hb,
                                 addr_handle_t hc) {
    c.set_handle(hc);
    b.set_handle(hb);
    a.set_handle(ha);
  }
  target function void run() {
    a.CTL.write_val(1); b.CTL.write_val(2); c.CTL.write_val(3);
  }
}""", ["write 32 0x1000 0x1", "write 32 0x2000 0x2", "write 32 0x3010 0x3"],
        args=(0x1000, 0x2000, 0x3000)),

    "a group no set_handle reaches": Case("""
component pss_top {
  ga_c a;
  gb_c b;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() { a.STS.write_val(1); b.CTL.write_val(2); }
}""", Refused([r"\bb\b", r"never bound"])),

    # --- Names: a generated API names a PSS type as PSS does -------------------
    # C named a component `<prefix>_t` and a sub-component's accessor
    # `<prefix>_<sub>`, so a sub-component `t` declared the type's name a
    # second time and the header did not compile.
    "a sub-component named t": Case("""
component leaf_c {
  ga_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void poke(bit[32] v) { a.STS.write_val(v); }
}
component pss_top {
  leaf_c t;
  leaf_c u;
  solve function void initialize(addr_handle_t base) {
    t.initialize(base);
    u.initialize(make_handle_from_handle(base, 0x100));
  }
  target function void run() { t.poke(1); u.poke(2); }
}""", ["write 32 0x1004 0x1", "write 32 0x1104 0x2"]),

    # C and C++ rewrote `_c`, `_s` and `_e` to `_t`, which made these three
    # PSS types one C type.
    "a component, a struct and an enum with one stem": Case("""
enum x_e { XA = 1, XB = 2 }
struct x_s { bit[32] v; }
component x_c {
  target function bit[32] f(x_s p, x_e e) {
    if (e == XB) { return p.v; }
    return 0;
  }
}
component pss_top {""" + _ONE + """
  x_c x;
  target function void run() {
    x_s v;
    v.v = 5;
    a.STS.write_val(x.f(v, XB));
  }
}""", ["write 32 0x1004 0x5"]),

    # C named the handle parameter `s` (`self` in `_init`) and its bus pointer
    # `bus`, so a model's own `s` was declared twice: `x_f(x_c *s, x_s s)`.
    # The generated names are now `_self`/`_bus`, and a model's `_self` is
    # renamed like a C keyword.
    "names the generated code also uses": Case("""
component leaf_c {
  ga_c a;
  bit[32] bus;
  bit[32] _bus;
  solve function void initialize(addr_handle_t self, bit[32] _self) {
    a.set_handle(self);
    bus = _self;
    _bus = _self + 1;
  }
  target function bit[32] f(bit[32] s, bit[32] _self) {
    bit[32] self = s + _self;
    bit[32] _bus = self + bus;
    return _bus + this._bus;
  }
  target function void poke(bit[32] s) { a.STS.write_val(f(s, 1)); }
}
component pss_top {
  leaf_c s;
  solve function void initialize(addr_handle_t base) {
    s.initialize(base, 0x10);
  }
  target function void run() { s.poke(2); }
}""", ["write 32 0x1004 0x24"]),

    # ...and the ROOT's constructor parameters, which every target forwards
    # from a factory that takes the platform (`imp`, `imports`, `_bus`).
    "root constructor parameters named like generated ones": Case("""
component pss_top {
  ga_c a;
  bit[32] k;
  solve function void initialize(addr_handle_t imp, bit[32] self,
                                 bit[32] imports, bit[32] bus) {
    a.set_handle(imp);
    k = self + imports + bus;
  }
  target function void run() { a.STS.write_val(k); }
}""", ["write 32 0x1004 0x7"], args=(0x1000, 1, 2, 4)),

    # --- construction (LRM 20.1.2) -------------------------------------------
    # Every instance is constructed, whether or not a constructor reaches it.
    # C embeds a child by value, so a child its parent's constructor never
    # named read zeroes -- `int a = 5;` was 0, silently.
    "sub-components nobody initializes": Case("""
component inner_c { int k = 3; }
component sub_c {
  int a = 5;
  inner_c in1;
  target function int sum() { return a + in1.k; }
}
component pss_top {
  ga_c a;
  sub_c s;
  sub_c arr[2];
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() { a.STS.write_val(s.sum() + arr[1].sum()); }
}""", ["write 32 0x1004 0x10"]),

    # ...including one whose type has a constructor nobody calls.
    "a sub-component whose constructor nobody calls": Case("""
component cfg_c {
  int a = 10;
  int b = 1;
  solve function void initialize(int x) { b = a + x; }
  target function int get() { return a + b; }
}
component pss_top {
  ga_c a;
  cfg_c c;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() { a.STS.write_val(c.get()); }
}""", ["write 32 0x1004 0xb"], xfail=_on(["py"], _PY_LAZY_SUB)),

    # A child is constructed before its parent's body runs, so what that body
    # writes into it (`c.a = 20`) is there when the child's constructor runs.
    "a constructor writes into a child before constructing it": Case("""
component cfg_c {
  int a = 10;
  int b;
  solve function void initialize(int x) { b = a + x; }
}
component pss_top {
  ga_c a;
  cfg_c c;
  solve function void initialize(addr_handle_t base) {
    a.set_handle(base);
    c.a = 20;
    c.initialize(1);
  }
  target function void run() { a.STS.write_val(c.b); }
}""", ["write 32 0x1004 0x15"],
        xfail={"py": _PY_LAZY_SUB, "sv": _SV_INIT_SUB_FIELD}),

    # --- D2: operations of a sub-component -------------------------------------
    "calling a sub-component's operation": Case("""
component sub_c {
  ga_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void poke() { a.STS.write_val(1); }
}
component pss_top {
  sub_c s;
  solve function void initialize(addr_handle_t base) { s.initialize(base); }
  target function void run() { s.poke(); }
}""", ["write 32 0x1004 0x1"]),

    "a sub-component's register written from above": Case("""
component sub_c {
  ga_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
}
component pss_top {
  sub_c s;
  solve function void initialize(addr_handle_t base) { s.initialize(base); }
  target function void run() { s.a.STS.write_val(1); }
}""", ["write 32 0x1004 0x1"]),

    "operations of a sub-component array": Case("""
component sub_c {
  ga_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void poke() { a.STS.write_val(1); }
}
component pss_top {
  sub_c s[2];
  solve function void initialize(addr_handle_t base) {
    foreach (s[i]) { s[i].initialize(base + 0x100 * i); }
  }
  target function void run() {
    s[1].poke();
    foreach (s[i]) { s[i].poke(); }
  }
}""", ["write 32 0x1104 0x1", "write 32 0x1004 0x1", "write 32 0x1104 0x1"]),

    "a sub-component's value in a condition": Case("""
component sub_c {
  ga_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function bit[32] sts() { return a.STS.read_val(); }
  target function void poke() { a.STS.write_val(1); }
}
component pss_top {
  sub_c s;
  solve function void initialize(addr_handle_t base) { s.initialize(base); }
  target function void run() { if (s.sts() != 0) { s.poke(); } }
}""", ["read 32 0x1004 0x5", "write 32 0x1004 0x1"], mem={0x1004: 5}),

    # --- D3: bit and part selects (LRM 8.5.x; Table 21: unsigned, hi-lo+1) ----
    "a part select read": Case("""
component pss_top {
  gs_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() {
    bit[64] x = 0xABCDE123;
    ctl_s c;
    c.base_lo = x[31:12];
    a.CTL.write(c);
  }
}""", ["write 32 0x1000 0xabcde000"]),

    "a bit select and the high half of 64 bits": Case("""
component pss_top {""" + _ONE + """
  target function void run() {
    bit[64] x = 0x1234567800000008;
    a.STS.write_val(x[3]);
    a.STS.write_val(x[2]);
    a.STS.write_val(x[63:32]);
  }
}""", ["write 32 0x1004 0x1", "write 32 0x1004 0x0",
       "write 32 0x1004 0x12345678"]),

    "a bit select with a run-time index": Case("""
component pss_top {""" + _ONE + """
  target function void run() {
    bit[8] x = 0xA;
    int i = 0;
    while (i < 4) { a.STS.write_val(x[i]); i += 1; }
  }
}""", ["write 32 0x1004 0x0", "write 32 0x1004 0x1",
       "write 32 0x1004 0x0", "write 32 0x1004 0x1"]),

    "a part and a bit select assigned": Case("""
component pss_top {""" + _ONE + """
  target function void run() {
    bit[32] v = 0xFFFFFFFF;
    v[7:4] = 0;
    v[0] = 0;
    v[31:28] = 0x5;
    a.STS.write_val(v);
  }
}""", ["write 32 0x1004 0x5fffff0e"]),

    "a part select of a register field assigned": Case("""
component pss_top {
  gs_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() {
    ctl_s c = a.CTL.read();
    c.base_lo[3:0] = 0xA;
    a.CTL.write(c);
  }
}""", ["read 32 0x1000 0xfffff001", "write 32 0x1000 0xffffa001"],
        mem={0x1000: 0xFFFFF001}),

    # --- D4: match arms -------------------------------------------------------
    # An enum item reaches the IR as the constant it names; what C, C++ and
    # Python could not lower was the `default:` arm (a wildcard pattern).
    "a match with a default arm": Case("""
component pss_top {""" + _ONE + """
  target function void go(int x) {
    match (x) {
      [1]: a.STS.write_val(10);
      [2, 3]: a.STS.write_val(20);
      default: a.STS.write_val(30);
    }
  }
  target function void run() { go(1); go(3); go(7); }
}""", ["write 32 0x1004 0xa", "write 32 0x1004 0x14", "write 32 0x1004 0x1e"]),

    "match on enum items": Case("""
enum mode_e { M0, M1, M2, M3 }
component pss_top {""" + _ONE + """
  target function void go(mode_e m) {
    match (m) {
      [M1]: a.STS.write_val(1);
      [M0, M2]: a.STS.write_val(2);
      default: a.STS.write_val(3);
    }
  }
  target function void run() { go(M0); go(M1); go(M2); go(M3); }
}""", ["write 32 0x1004 0x2", "write 32 0x1004 0x1",
       "write 32 0x1004 0x2", "write 32 0x1004 0x3"]),

    "match on items of a package's enum": Case("""
package modes_pkg { enum mode_e { M0, M1, M2 } }
import modes_pkg::*;
component pss_top {""" + _ONE + """
  target function void go(mode_e m) {
    match (m) {
      [M2]: a.STS.write_val(2);
      default: a.STS.write_val(0);
    }
  }
  target function void run() { go(M2); go(M0); }
}""", ["write 32 0x1004 0x2", "write 32 0x1004 0x0"]),

    # --- D5: offset functions are evaluated, whatever their form ---------------
    "offsets from an if chain": Case("""
pure component gi_c : reg_group_c {
  reg_c<bit[32], READWRITE, 32> CTL;
  reg_c<bit[32], READWRITE, 32> STS;
  function bit[64] get_offset_of_instance(string name) {
    if (name == "CTL") {
      return 0x0;
    } else if (name == "STS") {
      return 0x4;
    }
    return 0xFFFFFFFFFFFFFFFF;
  }
}
component pss_top {
  gi_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() { a.CTL.write_val(1); a.STS.write_val(2); }
}""", ["write 32 0x1000 0x1", "write 32 0x1004 0x2"]),

    "offsets from an if chain, out of order and sparse": Case("""
pure component gi_c : reg_group_c {
  reg_c<bit[32], READWRITE, 32> CTL;
  reg_c<bit[32], READWRITE, 32> STS;
  function bit[64] get_offset_of_instance(string name) {
    if (name == "CTL") { return 0x28; }
    if (name == "STS") { return 0x8; }
    return 0xFFFFFFFFFFFFFFFF;
  }
}
component pss_top {
  gi_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() { a.CTL.write_val(1); a.STS.write_val(2); }
}""", ["write 32 0x1028 0x1", "write 32 0x1008 0x2"]),

    "an array-offset function that answers no for everything": Case("""
pure component gi_c : reg_group_c {
  reg_c<bit[32], READWRITE, 32> CTL;
  function bit[64] get_offset_of_instance(string name) {
    if (name == "CTL") { return 0x20; }
    return 0xFFFFFFFFFFFFFFFF;
  }
  function bit[64] get_offset_of_instance_array(string name, int index) {
    return 0xFFFFFFFFFFFFFFFF;
  }
}
component pss_top {
  gi_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() { a.CTL.write_val(1); }
}""", ["write 32 0x1020 0x1"]),

    "an if chain with no case for a register": Case("""
pure component gi_c : reg_group_c {
  reg_c<bit[32], READWRITE, 32> CTL;
  reg_c<bit[32], READWRITE, 32> STS;
  function bit[64] get_offset_of_instance(string name) {
    if (name == "CTL") { return 0x0; }
    return 0xFFFFFFFFFFFFFFFF;
  }
}
component pss_top {
  gi_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() { a.STS.write_val(2); }
}""", Refused([r"gi_c", r"STS"])),

    "an offset function the build cannot evaluate": Case("""
import target function bit[64] plat_offset();
pure component gi_c : reg_group_c {
  reg_c<bit[32], READWRITE, 32> CTL;
  function bit[64] get_offset_of_instance(string name) {
    if (name == "CTL") { return plat_offset(); }
    return 0xFFFFFFFFFFFFFFFF;
  }
}
component pss_top {
  gi_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() { a.CTL.write_val(1); }
}""", Refused([r"gi_c", r"get_offset_of_instance"])),

    # --- D6: enum-typed fields in a packed register struct ---------------------
    "an enum field in a packed register struct": Case("""
enum e2_e : bit[2] { Z0, Z1, Z2, Z3 }
struct fld_s : packed_s<> { bit[1] lo; e2_e mode; bit[29] hi; }
pure component ge_c : reg_group_c {
  reg_c<fld_s, READWRITE, 32> F;
  function bit[64] get_offset_of_instance(string name) {
    match (name) { ["F"]: return 0x0; }
    return 0xFFFFFFFFFFFFFFFF;
  }
}
component pss_top {
  ge_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() {
    fld_s v = a.F.read();
    v.mode = Z2;
    a.F.write(v);
  }
}""", ["read 32 0x1000 0xfffffff9", "write 32 0x1000 0xfffffffd"],
        mem={0x1000: 0xFFFFFFF9}),

    # Refused by the front end (pssparser), with its location: pinned here so
    # no target starts defaulting a width.
    "an unsized enum in a packed register struct": Case("""
enum u_e { UA, UB }
struct fld_s : packed_s<> { bit[1] lo; u_e mode; bit[30] hi; }
pure component ge_c : reg_group_c {
  reg_c<fld_s, READWRITE, 32> F;
  function bit[64] get_offset_of_instance(string name) {
    match (name) { ["F"]: return 0x0; }
    return 0xFFFFFFFFFFFFFFFF;
  }
}
component pss_top {
  ge_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void run() { fld_s v = a.F.read(); a.F.write(v); }
}""", Refused([r"fld_s", r"mode", r"u_e"])),

    # --- D7: the conditional operator ------------------------------------------
    "a conditional expression": Case("""
component pss_top {""" + _ONE + """
  target function void go(bit x) { a.STS.write_val((x == 1) ? 0 : 1); }
  target function void run() { go(0); go(1); }
}""", ["write 32 0x1004 0x1", "write 32 0x1004 0x0"]),

    "a nested conditional whose arm reads a register": Case("""
component pss_top {""" + _ONE + """
  target function bit[32] sts() { return a.STS.read_val(); }
  target function void go(bit[2] x) {
    a.CTL.write_val((x == 1) ? sts() : ((x == 0) ? 7 : 9));
  }
  target function void run() { go(1); go(0); go(2); }
}""", ["read 32 0x1004 0x5", "write 32 0x1000 0x5",
       "write 32 0x1000 0x7", "write 32 0x1000 0x9"],
        mem={0x1004: 5}),

    # --- D8: a value-returning operation inside an expression ------------------
    "a value-returning operation in a condition": Case("""
component pss_top {""" + _ONE + """
  target function bool busy() { return a.STS.read_val() != 0; }
  target function void run() { if (busy()) { a.STS.write_val(0); } }
}""", ["read 32 0x1004 0x3", "write 32 0x1004 0x0"], mem={0x1004: 3}),

    # --- D9: PSS bit widths (LRM 8.7) ------------------------------------------
    # Equality propagates the larger operand width (4) to both sides, so ~15
    # is 0 here, and equals q.
    "~ in a context narrower than int": Case("""
component pss_top {""" + _ONE + """
  target function void go(bit[4] p, bit[4] q) {
    if ((~p) == q) { a.STS.write_val(1); } else { a.STS.write_val(2); }
  }
  target function void run() { go(15, 0); go(0, 15); go(1, 1); }
}""", ["write 32 0x1004 0x1", "write 32 0x1004 0x1", "write 32 0x1004 0x2"]),

    # The 64-bit parameter width propagates to x BEFORE the inversion.
    "~ in a context wider than its operand": Case("""
component pss_top {
  g64_c a;
  solve function void initialize(addr_handle_t base) { a.set_handle(base); }
  target function void go(bit[32] x) { a.W.write_val(~x); }
  target function void run() { go(1); }
}""", ["write 64 0x1000 0xfffffffffffffffe"]),

    # The case as originally reported: 0xFFFFFFFE is RIGHT (8.7.2, Table 23),
    # not 0. Pinned so nobody "fixes" it to the operand's width.
    "~ on a bit passed to a 32-bit parameter": Case("""
component pss_top {""" + _ONE + """
  target function void go(bit x) { a.STS.write_val(~x); }
  target function void run() { go(1); go(0); }
}""", ["write 32 0x1004 0xfffffffe", "write 32 0x1004 0xffffffff"]),

    # A `bit[4]` holds 0..15: 15 + 1 is 0, whatever C type stores it.
    "a narrow local wraps at its own width": Case("""
component pss_top {""" + _ONE + """
  target function void run() {
    bit[4] n = 15;
    n = n + 1;
    a.STS.write_val(n);
    n += 15;
    a.STS.write_val(n);
  }
}""", ["write 32 0x1004 0x0", "write 32 0x1004 0xf"]),

    # `int[8]` 127 + 1 is -128, sign-extended into the 32-bit parameter.
    "a narrow signed value overflows to negative": Case("""
component pss_top {""" + _ONE + """
  target function void run() {
    int[8] v = 127;
    v = v + 1;
    a.STS.write_val(v);
  }
}""", ["write 32 0x1004 0xffffff80"]),

    # PSS `/` and `%` truncate toward zero (8.5.1): -7 / 2 is -3, -7 % 2 is -1.
    "signed division truncates toward zero": Case("""
component pss_top {""" + _ONE + """
  target function void go(int x) {
    a.STS.write_val(x / 2);
    a.STS.write_val(x % 2);
  }
  target function void run() { go(-7); }
}""", ["write 32 0x1004 0xfffffffd", "write 32 0x1004 0xffffffff"]),

    # The assignment propagates 8 bits to `(~x) >> 1` and so to x: 0x01 ->
    # 0xFE -> 0x7F. In int it is -2 >> 1 = -1 -> 0xFF; at x's own 4 bits
    # it is 0xE >> 1 = 0x7.
    "~ then a right shift, assigned to 8 bits": Case("""
component pss_top {""" + _ONE + """
  target function void go(bit[4] x) {
    bit[8] y = (~x) >> 1;
    a.STS.write_val(y);
  }
  target function void run() { go(1); }
}""", ["write 32 0x1004 0x7f"]),
}


def _params():
    for name in sorted(CASES):
        case = CASES[name]
        for tgt in _ALL:
            marks = []
            why = th.available(tgt)
            if why:
                marks.append(pytest.mark.skip(reason=why))
            if tgt in ("c", "cpp"):
                marks.append(pytest.mark.c_toolchain)
            if tgt == "sv":
                marks.append(pytest.mark.sim)
            if tgt in case.xfail:
                marks.append(pytest.mark.xfail(strict=True,
                                               reason=case.xfail[tgt]))
            yield pytest.param(name, tgt, marks=marks, id=f"{tgt}-{name}")


@pytest.mark.parametrize("name, target", list(_params()))
def test_construct(tmp_path, name, target):
    case = CASES[name]
    if isinstance(case.want, Refused):
        with pytest.raises(CompileError) as ei:
            th.compile_model(tmp_path, _REGS + case.pss, target)
        text = diagnostic_text(ei)
        for pat in case.want.patterns:
            assert re.search(pat, text), (pat, text)
        return
    got = th.run(target, tmp_path, _REGS + case.pss, case.args, case.mem)
    assert got == case.want


@pytest.mark.parametrize("target", _ALL)
def test_an_enum_field_has_its_base_types_width_in_the_manifest(tmp_path,
                                                                target):
    """`enum e2_e : bit[2]` makes the field two bits wide (D6). The layout is
    the manifest's statement, so a consumer packing the value by hand gets
    the same bits the generated accessors do."""
    man = tmp_path / "manifest.json"
    th.compile_model(tmp_path, _REGS + CASES[
        "an enum field in a packed register struct"].pss, target,
        progseq_manifest=str(man))
    doc = json.loads(man.read_text())
    (fld,) = [s for s in doc["value_structs"] if s["name"] == "fld_s"]
    assert fld["bits"] == 32
    assert [(f["name"], f["lsb"], f["width"]) for f in fld["fields"]] == [
        ("lo", 0, 1), ("mode", 1, 2), ("hi", 3, 29)]
