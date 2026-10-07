"""The address binding in an op-model constructor, as SV.

A PSS operation model binds its address map in its constructor (`initialize`,
`init` or `ctor`), a solve function the integrating environment runs while the
tree is built:

    solve function void \\init (addr_handle_t base) {
        regs.set_handle(base);
        foreach (ch[i]) {
            ch[i].\\init (i, make_handle_from_handle(base,
                              WB_DMA_CH_BASE + i * WB_DMA_CH_STRIDE));
        }
    }

It becomes a method of the component class, run after construction: `new()`
has already built every sub-component and put every register group at address
0 (design D2). The body is rendered like any other solve-context body
(`lower_progseq._CtorEmitter`); the calls here are the ones with no meaning
outside a constructor, and are rendered as what they do:

* `regs.set_handle(h)` REBUILDS the register group `regs` at `h`;
* `super.initialize(...)` is the base class's constructor method (design D3:
  not virtual, so `super` reaches it whatever this class's signature).

A sub-component's constructor, `ch[i].\\init(...)`, needs nothing special: it
is a method of the sub-component's class, called as one.

This module used to lower the whole body, statement by statement, and refuse
anything else -- a local, an `if`, an assignment into a sub-component -- on
the reasoning that a skipped statement is an unbound address. Rendering the
other statements as every other body renders them gives the same guarantee:
nothing is skipped, and a statement with no rendering is still an error.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from ..progseq_model import _dt_name


def _self_attr_name(e) -> Optional[str]:
    """`self.<name>` -> name, else None."""
    if (_dt_name(e) == "ExprAttribute"
            and _dt_name(getattr(e, "value", None)) == "TypeExprRefSelf"):
        return e.attr
    return None


def binding_call(call, *, members: Dict[str, str], reg_groups: List[str],
                 bus: str, expr: Callable[[object], str],
                 pad: str) -> Optional[List[str]]:
    """The lines a constructor's ``call`` statement becomes, if it is one of
    the address-binding calls above; None for any other call.

    ``members`` maps a PSS field name to its member name, ``bus`` is the
    expression a rebuilt register group is wired to, and ``expr`` renders an
    IR expression.
    """
    fn = call.func
    if _dt_name(fn) != "ExprAttribute":
        return None
    if _dt_name(fn.value) == "TypeExprRefSuper":
        args = ", ".join(expr(a) for a in call.args)
        return [f"{pad}super.{fn.attr}({args});"]
    if fn.attr == "set_handle":
        name = _self_attr_name(fn.value)
        if name in reg_groups:
            handle = expr(call.args[0]) if call.args else bus
            return [f"{pad}{members[name]} = new({bus}, {handle});"]
    return None
