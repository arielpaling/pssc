"""C0 end-to-end admission for PSS software-executor semantics."""

import re
import shutil

import pytest

from ._verilator import HAVE_VERILATOR, verilator_build_run
from .test_sv_native_uh0_e2e import _compile_native


_CORE_EXECUTOR_PSS = """
import std_pkg::*;
import executor_pkg::*;

struct software_executor_trait_s : executor_trait_s {
  rand bit[1] personality;
}

component cva6_executor_c : executor_c<software_executor_trait_s> {}
component mbv_executor_c : executor_c<software_executor_trait_s> {}

extend component executor_base_c {
  target function void mark_executor(bit[8] marker);
}

resource software_execution_context_r : executor_claim_s<software_executor_trait_s> {
  constraint trait.personality == instance_id;
}

component pss_top {
  cva6_executor_c cva6;
  mbv_executor_c mbv;
  executor_group_c<software_executor_trait_s> executors;
  pool [2] software_execution_context_r contexts;
  bind contexts *;

  exec init_down {
    cva6.trait.personality = 0;
    mbv.trait.personality = 1;
    executors.add_executor(cva6);
    executors.add_executor(mbv);
  }

  abstract action software_step {
    lock software_execution_context_r core_ctx;
  }

  action cva6_step : software_step {
    exec body { executor().mark_executor(8'hc6); }
  }

  action mbv_step : software_step {
    exec body { executor().mark_executor(8'hb6); }
  }

  action Root {
    cva6_step cva6_run;
    activity {
      cva6_run;
    }
  }

  action RootMpv {
    mbv_step mbv_run;
    activity {
      mbv_run;
    }
  }
}
"""


_CORE_EXECUTOR_TB = """\
module tb;
  import zsp_rt_pkg::*;
  import zsp_gen_pkg::*;
  class cva6_target implements zsp_executor_target_if;
    virtual task mark_executor(bit [7:0] marker);
      $display("C0_EXECUTOR=CVA6:%0h", marker);
    endtask
  endclass
  class mbv_target implements zsp_executor_target_if;
    virtual task mark_executor(bit [7:0] marker);
      $display("C0_EXECUTOR=MBV:%0h", marker);
    endtask
  endclass
  initial begin
    export_api_impl ep = new(null);
    cva6_target cva6_binding = new();
    mbv_target mbv_binding = new();
    ep.cva6.target_if = cva6_binding;
    ep.mbv.target_if = mbv_binding;
    if (ep.executors.size() != 2) $fatal(1, "C0 executor group was not initialized");
    if (ep.cva6.trait.personality != 0 || ep.mbv.trait.personality != 1)
      $fatal(1, "C0 executor traits were not initialized");
    executor_pkg__executor_base_c::set_current(ep.cva6);
    ep.Root();
    executor_pkg__executor_base_c::set_current(ep.mbv);
    ep.RootMpv();
    $finish;
  end
endmodule
"""


_LOCKED_EXECUTOR_PSS = """
import executor_pkg::*;

struct lock_trait_s : executor_trait_s {}
component lock_executor_c : executor_c<lock_trait_s> {}

extend component executor_base_c {
  target function void mark_executor(bit[3] marker);
  target function void hold_executor();
}

resource lock_context_r : executor_claim_s<lock_trait_s> {}

component pss_top {
  lock_executor_c executor_instance;
  executor_group_c<lock_trait_s> executors;
  pool [1] lock_context_r contexts;
  bind contexts *;

  exec init_down { executors.add_executor(executor_instance); }

  abstract action serialized_step { lock lock_context_r core_ctx; }
  action one : serialized_step {
    exec body {
      executor().mark_executor(1);
      executor().hold_executor();
      executor().mark_executor(2);
    }
  }
  action two : serialized_step {
    exec body {
      executor().mark_executor(3);
      executor().hold_executor();
      executor().mark_executor(4);
    }
  }
  action Root { activity { parallel { do one; do two; } } }
}
"""


_LOCKED_EXECUTOR_TB = """\
module tb;
  import zsp_rt_pkg::*;
  import zsp_gen_pkg::*;
  class target_impl implements zsp_executor_target_if;
    virtual task mark_executor(bit [2:0] marker);
      $display("C0_LOCK_MARK=%0d", marker);
    endtask
    virtual task hold_executor();
      #1;
    endtask
  endclass
  initial begin
    static export_api_impl ep = new(null);
    static target_impl target = new();
    ep.executor_instance.target_if = target;
    executor_pkg__executor_base_c::set_current(ep.executor_instance);
    ep.Root();
    $finish;
  end
endmodule
"""


_NESTED_POLICY_PSS = """
component pss_top {
  action checkpoint {
    rand bit required;
  }
  action Root {
    checkpoint child;
    constraint child.required == 1;
    activity { child; }
  }
}
"""


_NESTED_POLICY_TB = """\
module tb;
  import zsp_rt_pkg::*;
  import zsp_gen_pkg::*;
  initial begin
    export_api_impl ep = new(null);
    ep.Root();
    $finish;
  end
endmodule
"""


def test_sv_native_core_executor_resource_model_runs(tmp_path):
    """PSS 3 executor traits may back a lockable processor resource."""
    out = _compile_native(
        tmp_path, _CORE_EXECUTOR_PSS, _CORE_EXECUTOR_TB,
        export_actions=["Root", "RootMpv"])
    generated = (out / "zsp_gen_pkg.sv").read_text()
    assert "software_execution_context_r" in generated
    if not HAVE_VERILATOR:
        return
    rc, log = verilator_build_run(
        out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc == 0, log
    assert "C0_EXECUTOR=CVA6:c6" in log
    assert "C0_EXECUTOR=MBV:b6" in log

    # The target adapter is a required binding, not decorative type plumbing.
    # Swapping one adapter must change the observed executor route.
    bad_out = tmp_path / "swapped_executor_binding"
    bad_out.mkdir()
    for name in ("zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"):
        shutil.copy(out / name, bad_out / name)
    tb = (bad_out / "tb.sv").read_text()
    tb = tb.replace("ep.cva6.target_if = cva6_binding;",
                    "ep.cva6.target_if = mbv_binding;")
    (bad_out / "tb.sv").write_text(tb)
    rc, log = verilator_build_run(
        bad_out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc == 0, log
    assert "C0_EXECUTOR=MBV:c6" in log
    assert "C0_EXECUTOR=CVA6:c6" not in log


def test_sv_native_rejects_mixed_executor_trait_specializations(tmp_path):
    """Do not erase different PSS executor traits into one untyped runtime."""
    pss = """
import executor_pkg::*;
struct cva6_trait_s : executor_trait_s { bit cva6; }
struct mbv_trait_s : executor_trait_s { bit mbv; }
component cva6_executor_c : executor_c<cva6_trait_s> {}
component mbv_executor_c : executor_c<mbv_trait_s> {}
component pss_top {
  cva6_executor_c cva6;
  mbv_executor_c mbv;
  action Root { exec body {} }
}
"""
    with pytest.raises(ValueError, match="one concrete executor_c trait specialization"):
        _compile_native(tmp_path, pss, _CORE_EXECUTOR_TB)


def test_sv_native_executor_claim_lock_serializes_target_calls(tmp_path):
    """An executor claim, unlike executor assignment, serializes stateful work."""
    out = _compile_native(tmp_path, _LOCKED_EXECUTOR_PSS, _LOCKED_EXECUTOR_TB)
    rc, log = verilator_build_run(
        out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc == 0, log
    marks = [line.rsplit("=", 1)[1] for line in log.splitlines()
             if line.startswith("C0_LOCK_MARK=")]
    assert marks in (["1", "2", "3", "4"], ["3", "4", "1", "2"]), log

    bad_out = tmp_path / "missing_executor_lock"
    bad_out.mkdir()
    for name in ("zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"):
        shutil.copy(out / name, bad_out / name)
    generated = (bad_out / "zsp_gen_pkg.sv").read_text()
    mutated = re.sub(
        r"(?m)^(\s*).*\.contexts\.(?:lock|unlock)\([^\n]+\);$",
        r"\1// C0 mutation: executor-context lock omitted", generated)
    assert mutated != generated
    (bad_out / "zsp_gen_pkg.sv").write_text(mutated)
    rc, log = verilator_build_run(
        bad_out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc == 0, log
    marks = [line.rsplit("=", 1)[1] for line in log.splitlines()
             if line.startswith("C0_LOCK_MARK=")]
    assert marks in (["1", "3", "2", "4"], ["3", "1", "4", "2"]), log


def test_sv_native_nested_policy_constraint_is_qualified_once(tmp_path):
    """A compound constraint addresses its child once, not recursively."""
    out = _compile_native(tmp_path, _NESTED_POLICY_PSS, _NESTED_POLICY_TB)
    generated = (out / "zsp_gen_pkg.sv").read_text()
    assert "child.required == 1" in generated
    assert "child.child.required" not in generated
    rc, log = verilator_build_run(
        out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc == 0, log


def test_sv_native_refuses_unimplemented_address_claim_allocation(tmp_path):
    """An address claim cannot become an unallocated SV class handle."""
    pss = """
import addr_reg_pkg::*;
component pss_top {
  action Root {
    rand transparent_addr_claim_s<> image;
    exec body {}
  }
}
"""
    with pytest.raises(ValueError, match="does not support address-claim allocation"):
        _compile_native(tmp_path, pss, _CORE_EXECUTOR_TB)
