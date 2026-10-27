"""Top-level entry point: IR Context -> List[SV IR nodes].

Orchestrates all lowering passes in dependency order.
"""
from __future__ import annotations

from typing import Any, List, Tuple

import zuspec.ir.core as ir
from zuspec.be.sv.ir.sv import (
    SVArg,
    SVClass,
    SVClassField,
    SVForwardDecl,
    SVFunctionDecl,
    SVInterfaceClass,
    SVTaskDecl,
)

from ...ast2ir import AstToIrContext
from .context import LoweringContext
from .lower_types import lower_enum, lower_struct
from .lower_components import lower_component
from .lower_actions import lower_action
from .lower_imports import lower_import_interface


# PSS stdlib packages whose types are provided by zsp_rt_pkg.sv and must
# not be emitted into user-generated SV.
_STDLIB_PREFIXES = (
    "executor_pkg::",
    "addr_reg_pkg::",
    "sync_pkg::",
    "std_pkg::",
)

# Bare stdlib names that appear without a package prefix in the type_map
_STDLIB_BARE = frozenset([
    "array", "list", "set", "map",
    "bool", "int", "string",
    "endianness_e", "message_verbosity_e", "reg_access",
    "channel_c", "actor_c",
    "executor_trait_s", "empty_executor_trait_s",
    "executor_base_c", "executor_c", "executor_group_c", "executor_group_default_c",
    "executor_claim_s",
    "addr_space_base_c", "addr_space_group_c",
    "addr_trait_s", "empty_addr_trait_s", "addr_handle_t",
    "addr_region_base_s", "addr_region_s", "transparent_addr_region_s",
    "addr_claim_base_s", "addr_claim_s", "transparent_addr_claim_s",
    "sizeof_s", "sized_addr_handle_s",
    "contiguous_addr_space_c", "transparent_addr_space_c",
    "reg_c", "reg_group_c",
    "packed_s",
])

_STANDARD_EXECUTOR_FUNCTIONS = frozenset([
    "get_context", "set_target_execution_unit", "get_target_execution_unit",
    "addr_value", "addr_value_solve", "addr_value_abs",
    "read8", "read16", "read32", "read64",
    "write8", "write16", "write32", "write64",
    "read_bytes", "write_bytes",
])

_ADDRESS_CLAIM_BASES = frozenset([
    "addr_claim_base_s", "addr_claim_s", "transparent_addr_claim_s",
    "addr_reg_pkg::addr_claim_base_s", "addr_reg_pkg::addr_claim_s",
    "addr_reg_pkg::transparent_addr_claim_s",
])


def _is_stdlib(qname: str) -> bool:
    """Return True if *qname* belongs to a PSS stdlib package."""
    if any(qname.startswith(pfx) for pfx in _STDLIB_PREFIXES):
        return True
    # Bare aliases for stdlib types start with bit[ or int[
    if qname.startswith(("bit[", "int[")):
        return True
    return qname in _STDLIB_BARE


def _executor_extensions(ir_ctx: AstToIrContext):
    """Return user-declared executor-base target prototypes.

    The standard executor base has target primitives of its own. They are not
    an sv-native binding surface: only a PSS ``extend component
    executor_base_c`` prototype requests a generated target adapter.
    """
    base = (ir_ctx.type_map.get("executor_pkg::executor_base_c")
            or ir_ctx.type_map.get("executor_base_c"))
    if base is None:
        return []
    return [
        fn for fn in getattr(base, "functions", [])
        if getattr(fn, "is_target", False)
        and (getattr(fn, "metadata", {}) or {}).get("component_prototype")
        and (getattr(fn, "metadata", {}) or {}).get("component_extension")
        and fn.name not in _STANDARD_EXECUTOR_FUNCTIONS
    ]


def _configure_executor_lowering(ctx: LoweringContext) -> None:
    """Record the one concrete executor trait type sv-native can represent.

    PSS templates permit distinct trait specializations. The class-based
    native runtime intentionally does not pretend that one untyped field is
    equivalent to those specializations: mixed specializations are rejected
    before emitting SV.
    """
    traits = {
        getattr(dtype, "executor_trait_name")
        for dtype in ctx.ir_ctx.type_map.values()
        if isinstance(dtype, ir.DataTypeComponent)
        and getattr(dtype, "executor_trait_name", None)
    }
    if len(traits) > 1:
        raise ValueError(
            "sv-native supports one concrete executor_c trait specialization; "
            f"found {', '.join(sorted(traits))}")
    ctx.executor_trait_sv = (
        ctx.pss_type_to_sv_type_str(ir.DataTypeRef(ref_name=next(iter(traits))))
        if traits else None)
    ctx.executor_target_functions = {fn.name for fn in _executor_extensions(ctx.ir_ctx)}


def _refers_to_address_claim(ctx: LoweringContext, dtype, seen=None) -> bool:
    """Whether *dtype* is, or derives from, an address-claim type."""
    if seen is None:
        seen = set()
    if isinstance(dtype, ir.DataTypeRef):
        name = dtype.ref_name
        if name in _ADDRESS_CLAIM_BASES:
            return True
        dtype = ctx.ir_ctx.type_map.get(name)
        if dtype is None:
            dtype = next((value for key, value in ctx.ir_ctx.type_map.items()
                          if key.endswith(f"::{name}")), None)
    if dtype is None or id(dtype) in seen:
        return False
    seen.add(id(dtype))
    name = getattr(dtype, "name", None)
    if name in _ADDRESS_CLAIM_BASES:
        return True
    return _refers_to_address_claim(ctx, getattr(dtype, "super", None), seen)


def _reject_unlowered_address_claims(ctx: LoweringContext) -> None:
    """Fail closed until sv-native has a real address-claim allocator.

    Address claims are allocations, not scalar fields. Emitting a class handle
    without allocation, overlap, or address-space semantics would compile while
    certifying a fabricated address. C0 probes this boundary separately.
    """
    for name, dtype in ctx.ir_ctx.type_map.items():
        if _is_stdlib(name):
            continue
        if _refers_to_address_claim(ctx, dtype):
            raise ValueError(
                f"sv-native does not support address-claim allocation for '{name}'")
        for field in getattr(dtype, "fields", []):
            if _refers_to_address_claim(ctx, getattr(field, "datatype", None)):
                raise ValueError(
                    "sv-native does not support address-claim allocation for "
                    f"'{name}.{field.name}'")


def _lower_executor_dispatch(ctx: LoweringContext):
    """Emit the explicit target binding used by ``executor().target_fn()``.

    sv-native owns no software implementation. A PSS executor-base extension
    therefore becomes an interface supplied by the testbench/integration
    binding. The dispatch class fails loudly when it is unbound.
    """
    funcs = _executor_extensions(ctx.ir_ctx)
    if not funcs:
        return []

    tasks, iface_funcs, dispatch_tasks, dispatch_funcs = [], [], [], []
    for fn in funcs:
        args = [SVArg(name=a.arg, dtype=ctx.pss_type_to_sv_type_str(a.annotation))
                for a in getattr(getattr(fn, "args", None), "args", [])]
        is_task = getattr(fn, "returns", None) is None
        ret = (ctx.pss_type_to_sv_type_str(fn.returns)
               if getattr(fn, "returns", None) is not None else "void")
        if is_task:
            tasks.append(SVTaskDecl(name=fn.name, args=args))
            call_args = ", ".join(a.name for a in args)
            dispatch_tasks.append(SVTaskDecl(
                name=fn.name, args=args, is_virtual=True,
                body_lines=[
                    f'if (target_if == null) $fatal(1, "executor target {fn.name} is unbound");',
                    f"target_if.{fn.name}({call_args});",
                ]))
        else:
            iface_funcs.append(SVFunctionDecl(name=fn.name, args=args, return_type=ret))
            call_args = ", ".join(a.name for a in args)
            dispatch_funcs.append(SVFunctionDecl(
                name=fn.name, args=args, return_type=ret, is_virtual=True,
                body_lines=[
                    f'if (target_if == null) $fatal(1, "executor target {fn.name} is unbound");',
                    f"return target_if.{fn.name}({call_args});",
                ]))

    return [
        SVInterfaceClass(
            name="zsp_executor_target_if", tasks=tasks, functions=iface_funcs),
        SVClass(
            name="zsp_executor_dispatch_c",
            extends_name="executor_pkg__executor_c",
            fields=[SVClassField(name="target_if", dtype="zsp_executor_target_if")],
            tasks=dispatch_tasks,
            functions=[SVFunctionDecl(
                name="new",
                args=[SVArg(name="name", dtype="string"),
                      SVArg(name="parent", dtype="zsp_component")],
                return_type="",
                body_lines=["super.new(name, parent);"]),
                       SVFunctionDecl(
                name="current",
                return_type="zsp_executor_dispatch_c",
                is_static=True,
                body_lines=[
                    "zsp_executor_dispatch_c value;",
                    "if (!$cast(value, executor())) begin",
                    '  $fatal(1, "active executor does not implement the generated target interface");',
                    "  return null;",
                    "end",
                    "return value;",
                ])] + dispatch_funcs,
        ),
    ]


def pss_to_sv(ir_ctx: AstToIrContext) -> List[Any]:
    """Lower a Zuspec IR context to a list of SV IR nodes.

    Thin wrapper around :func:`pss_to_sv_with_ctx` that discards the lowering
    context. Prefer :func:`pss_to_sv_with_ctx` when the caller needs the context
    (e.g. for name mangling during export-API / top-module generation).
    """
    return pss_to_sv_with_ctx(ir_ctx)[0]


def pss_to_sv_with_ctx(ir_ctx: AstToIrContext) -> Tuple[List[Any], LoweringContext]:
    """Lower a Zuspec IR context to SV IR nodes, returning the context too.

    The returned list is in dependency order:
    1. Forward declarations
    2. Enums
    3. Structs (plain data types)
    4. Import interface classes
    5. Components
    6. Actions

    Uses the qualified name from the type_map key (e.g. ``top_c::hello``)
    for name mangling rather than the dtype's own ``name`` field, which
    may be unqualified (e.g. ``hello``).

    Args:
        ir_ctx: The translation context from ``AstToIrTranslator.translate()``.

    Returns:
        Tuple of (ordered list of SV IR nodes ready for ``SVEmitter``, the
        :class:`LoweringContext` used during lowering). The context exposes
        ``mangle_name()`` / ``sv_name_map`` for downstream generation.
    """
    ctx = LoweringContext(ir_ctx=ir_ctx)
    _reject_unlowered_address_claims(ctx)
    _configure_executor_lowering(ctx)
    result: List[Any] = []

    # Classify IR types, keeping the qualified name from the type_map key
    # --- Pre-pass: run activity analysis for every action that has an
    #     activity, so downstream lowering can reuse the results. ---
    try:
        from .analyze_activity import analyze_activity as _analyze_activity
        import zuspec.ir.core as _ir
        for _qname, _dtype in ir_ctx.type_map.items():
            if not isinstance(_dtype, _ir.DataTypeClass):
                continue
            _act_ir = getattr(_dtype, 'activity_ir', None)
            if _act_ir is None:
                continue
            try:
                _plan = _analyze_activity(ctx, _act_ir)
                ctx.activity_plans[_qname] = _plan
            except Exception:
                pass  # analysis failures are non-fatal
    except ImportError:
        pass  # analyze_activity module optional

    enums: List[Tuple[str, ir.DataTypeEnum]] = []
    structs: List[Tuple[str, ir.DataTypeStruct]] = []
    components: List[Tuple[str, ir.DataTypeComponent]] = []
    actions: List[Tuple[str, ir.DataTypeClass]] = []

    for name, dtype in ir_ctx.type_map.items():
        # Skip duplicates (same type registered under multiple names)
        if id(dtype) in ctx.emitted:
            continue
        ctx.emitted.add(id(dtype))

        # Skip PSS stdlib types: provided by zsp_rt_pkg.sv, not user code
        if _is_stdlib(name):
            continue

        if isinstance(dtype, ir.DataTypeEnum):
            enums.append((name, dtype))
        elif isinstance(dtype, ir.DataTypeComponent):
            components.append((name, dtype))
        elif isinstance(dtype, ir.DataTypeClass):
            actions.append((name, dtype))
        elif isinstance(dtype, ir.DataTypeStruct):
            structs.append((name, dtype))

    # 1. Enums
    for qname, e in enums:
        result.append(lower_enum(ctx, e))

    # 2. Structs (forward decls then definitions)
    for qname, s in structs:
        sv_name = ctx.mangle_name(qname)
        result.append(SVForwardDecl(class_name=sv_name))
    for qname, s in structs:
        # Temporarily set qualified name for lowering
        orig_name = s.name
        s.name = qname
        result.append(lower_struct(ctx, s))
        s.name = orig_name

    # The OO projection adds its package-scoped import API after this lowering
    # pass.  That API may use any PSS data type in its method signatures, so
    # retain the boundary after every user enum/struct declaration.  A caller
    # must insert the API after this prefix, before component definitions.
    ctx.user_type_node_count = len(result)

    # Executor target prototypes are a generated, explicit integration
    # boundary. It must precede user executor components that inherit it.
    result.extend(_lower_executor_dispatch(ctx))

    # 3. Import interfaces
    for qname, comp in components:
        orig_name = comp.name
        comp.name = qname
        imp_cls = lower_import_interface(ctx, comp)
        if imp_cls is not None:
            result.append(imp_cls)
        comp.name = orig_name

    # 4. Components (forward decls then definitions)
    for qname, comp in components:
        sv_name = ctx.mangle_name(qname)
        result.append(SVForwardDecl(class_name=sv_name))
    for qname, comp in components:
        orig_name = comp.name
        comp.name = qname
        result.append(lower_component(ctx, comp))
        comp.name = orig_name

    # 5. Actions (forward decls then definitions)
    for qname, act in actions:
        sv_name = ctx.mangle_name(qname)
        result.append(SVForwardDecl(class_name=sv_name))
    for qname, act in actions:
        comp_name = ir_ctx.parent_comp_names.get(qname)
        if not comp_name:
            # Fallback: try the unqualified name
            comp_name = ir_ctx.parent_comp_names.get(act.name)
        orig_name = act.name
        act.name = qname
        result.append(lower_action(ctx, act, comp_type_name=comp_name))
        act.name = orig_name

    return result, ctx
