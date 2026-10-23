"""UH0 end-to-end gates for sv-native component/resource/stream semantics."""

from pathlib import Path
import re
import shutil

import pytest

import pssc

from ._verilator import HAVE_VERILATOR, verilator_build_run


def _compile_native(tmp_path, pss_text, tb_text, *, export_actions=None):
    src = tmp_path / "model.pss"
    src.write_text(pss_text)
    out = tmp_path / "out"
    result = pssc.compile(
        src,
        target="sv-native",
        output_dir=str(out),
        export_actions=export_actions or ["Root"],
        raise_on_error=False,
    )
    assert not result.errors, result.errors
    (out / "tb.sv").write_text(tb_text)
    return out


def _copy_generated_sources(src, dst):
    dst.mkdir()
    for name in ("zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"):
        shutil.copy(src / name, dst / name)


_COMPONENT_STEER_PSS = """
import std_pkg::*;

component uart_endpoint_c {
  int endpoint_id;
  action run {
    exec body { message(NONE, "UH0_COMP_ENDPOINT=%0d", comp.endpoint_id); }
  }
}

component pss_top {
  uart_endpoint_c uart_a, uart_b;

  action Root {
    uart_endpoint_c::run child;
    activity {
      child with { comp == this.comp.uart_a; };
    }
  }
}
"""

_COMPONENT_STEER_TB = """\
module tb;
  import zsp_rt_pkg::*;
  import zsp_gen_pkg::*;
  initial begin
    export_api_impl ep = new(null);
    ep.uart_a.endpoint_id = 11;
    ep.uart_b.endpoint_id = 22;
    ep.Root();
    $finish;
  end
endmodule
"""


@pytest.mark.skipif(not HAVE_VERILATOR, reason="verilator not available")
def test_sv_native_named_child_component_constraint_routes_to_child(tmp_path):
    """LRM-style ``comp == this.comp.child`` binds a named child traversal."""
    out = _compile_native(tmp_path, _COMPONENT_STEER_PSS, _COMPONENT_STEER_TB)
    generated = (out / "zsp_gen_pkg.sv").read_text()
    assert "uart_endpoint_c__run child;" in generated
    assert "child.comp = comp.uart_a;" in generated
    assert "comp == comp.uart_a" not in generated

    rc, log = verilator_build_run(
        out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc == 0, log
    assert "UH0_COMP_ENDPOINT=11" in log

    # A child-target mutation must be observable; otherwise the positive run
    # could pass while the target constraint was merely ignored.
    bad_out = tmp_path / "bad_component_target"
    _copy_generated_sources(out, bad_out)
    generated = (bad_out / "zsp_gen_pkg.sv").read_text()
    mutated = generated.replace("child.comp = comp.uart_a;",
                                "child.comp = comp.uart_b;")
    assert mutated != generated
    (bad_out / "zsp_gen_pkg.sv").write_text(mutated)
    rc, log = verilator_build_run(
        bad_out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc == 0, log
    assert "UH0_COMP_ENDPOINT=22" in log
    assert "UH0_COMP_ENDPOINT=11" not in log


_COMPONENT_IMPORT_BINDING_PSS = """
package tb_api {
  import target function void mark(bit[8] value);
}

component uart_endpoint_c {
  import tb_api::*;
  int endpoint_id;

  action run {
    exec body { mark(comp.endpoint_id); }
  }
}

component pss_top {
  uart_endpoint_c uart_a, uart_b;

  action Root {
    uart_endpoint_c::run child;
    activity {
      child with { comp == this.comp.uart_a; };
    }
  }
}
"""


_COMPONENT_IMPORT_BINDING_TB = """\
module tb;
  import zsp_rt_pkg::*;
  import zsp_gen_pkg::*;
  class good_import extends import_api_base;
    virtual task mark(bit [7:0] value);
      $display("UH2_COMPONENT_ENDPOINT=%0d", value);
    endtask
  endclass
  class wrong_import extends import_api_base;
    virtual task mark(bit [7:0] value);
      $fatal(1, "UH2_WRONG_CHILD=%0d", value);
    endtask
  endclass
  initial begin
    export_api_impl ep = new(null);
    good_import good = new();
    wrong_import wrong = new();
    ep.uart_a.endpoint_id = 11;
    ep.uart_b.endpoint_id = 22;
    ep.uart_a.import_if = good;
    ep.uart_b.import_if = wrong;
    ep.Root();
    $finish;
  end
endmodule
"""


@pytest.mark.skipif(not HAVE_VERILATOR, reason="verilator not available")
def test_sv_native_child_component_import_binding_is_explicit_and_bites(tmp_path):
    """A target binding owns child imports; a swapped child trips its adapter."""
    out = _compile_native(
        tmp_path, _COMPONENT_IMPORT_BINDING_PSS, _COMPONENT_IMPORT_BINDING_TB)
    generated = (out / "zsp_gen_pkg.sv").read_text()
    endpoint = re.search(
        r"class uart_endpoint_c.*?endclass", generated, flags=re.DOTALL)
    assert endpoint is not None
    assert "import_api_if import_if;" in endpoint.group(0)
    assert "child.comp = comp.uart_a;" in generated

    rc, log = verilator_build_run(
        out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc == 0, log
    assert "UH2_COMPONENT_ENDPOINT=11" in log
    assert "UH2_WRONG_CHILD" not in log

    # This is a bite control, rather than an expected-failure-only smoke: the
    # mutation routes execution to a distinctly bound endpoint and must reach
    # its named rejecting adapter.
    bad_out = tmp_path / "bad_child_import_target"
    _copy_generated_sources(out, bad_out)
    generated = (bad_out / "zsp_gen_pkg.sv").read_text()
    mutated = generated.replace("child.comp = comp.uart_a;",
                                "child.comp = comp.uart_b;")
    assert mutated != generated
    (bad_out / "zsp_gen_pkg.sv").write_text(mutated)
    rc, log = verilator_build_run(
        bad_out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc != 0, log
    assert "UH2_WRONG_CHILD=22" in log


_SERIALIZED_EXECUTOR_PSS = """
package tb_api {
  import target function void mark(bit[3] value);
  import target function void hold();
}

enum uart_executor_tag_e { UART_CONTROL = 0 }

resource uart_executor_r {
  uart_executor_tag_e tag;
}

component pss_top {
  import tb_api::*;
  pool [1] uart_executor_r executor_pool;
  bind executor_pool *;

  abstract action lock_uart_control {
    lock uart_executor_r executor;
  }

  action register_write : lock_uart_control {
    exec body { mark(1); hold(); mark(2); }
  }

  action service_wire : lock_uart_control {
    exec body { mark(3); hold(); mark(4); }
  }

  action Root {
    activity {
      parallel {
        do register_write;
        do service_wire;
      }
    }
  }

  action RootSame {
    activity {
      parallel {
        do register_write;
        do register_write;
      }
    }
  }
}
"""

_SERIALIZED_EXECUTOR_TB = """\
module tb;
  import zsp_rt_pkg::*;
  import zsp_gen_pkg::*;
  class mark_impl #(type BaseT) extends BaseT;
    virtual task mark(bit [2:0] value);
      $display("UH0_MARK=%0d", value);
    endtask
  endclass
  class hold_impl #(type BaseT) extends BaseT;
    virtual task hold();
      #1;
    endtask
  endclass
  class import_api_tb extends hold_impl #(mark_impl #(import_api_base));
  endclass
  initial begin
    import_api_tb imp = new();
    export_api_if ep = pss_top::type_id().create(imp);
    $display("UH0_PHASE=DIFFERENT");
    ep.Root();
    $display("UH0_PHASE=SAME");
    ep.RootSame();
    $finish;
  end
endmodule
"""


@pytest.mark.skipif(not HAVE_VERILATOR, reason="verilator not available")
def test_sv_native_tagged_executor_lock_serializes_two_actor_classes(tmp_path):
    """A one-entry tagged executor pool forbids overlapping control actors."""
    out = _compile_native(
        tmp_path,
        _SERIALIZED_EXECUTOR_PSS,
        _SERIALIZED_EXECUTOR_TB,
        export_actions=["Root", "RootSame"],
    )
    rc, log = verilator_build_run(
        out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc == 0, log
    different, same = log.split("UH0_PHASE=SAME", 1)
    marks = [line.rsplit("=", 1)[1] for line in different.splitlines()
             if line.startswith("UH0_MARK=")]
    assert marks in (["1", "2", "3", "4"], ["3", "4", "1", "2"]), log
    marks = [line.rsplit("=", 1)[1] for line in same.splitlines()
             if line.startswith("UH0_MARK=")]
    assert marks == ["1", "2", "1", "2"], log

    # Removing the generated lock/unlock calls must expose the overlap the
    # one-entry executor pool forbids in the positive case.
    bad_out = tmp_path / "bad_executor_lock"
    _copy_generated_sources(out, bad_out)
    generated = (bad_out / "zsp_gen_pkg.sv").read_text()
    mutated = re.sub(r"(?m)^(\s*).*\.executor_pool\.(?:lock|unlock)\([^\n]+\);$",
                     r"\1// UH0 mutation: executor lock omitted", generated)
    assert mutated != generated
    (bad_out / "zsp_gen_pkg.sv").write_text(mutated)
    rc, log = verilator_build_run(
        bad_out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc == 0, log
    different, same = log.split("UH0_PHASE=SAME", 1)
    marks = [line.rsplit("=", 1)[1] for line in different.splitlines()
             if line.startswith("UH0_MARK=")]
    assert marks in (["1", "3", "2", "4"], ["3", "1", "4", "2"]), log
    marks = [line.rsplit("=", 1)[1] for line in same.splitlines()
             if line.startswith("UH0_MARK=")]
    assert marks == ["1", "1", "2", "2"], log


_STREAM_PSS = """
package tb_api {
  import target function void observe(bit[8] value);
}

stream uart_frame_s {
  rand bit[8] data;
}

component pss_top {
  import tb_api::*;
  pool uart_frame_s frames;
  bind frames *;

  action producer {
    output uart_frame_s frame;
    constraint data_value { frame.data == 8'ha5; }
  }

  action consumer {
    input uart_frame_s frame;
    exec body { observe(frame.data); }
  }

  action Root {
    producer p;
    consumer c;
    activity {
      parallel { p; c; }
      bind p.frame c.frame;
    }
  }
}
"""

_STREAM_TB = """\
module tb;
  import zsp_rt_pkg::*;
  import zsp_gen_pkg::*;
  class observe_impl #(type BaseT) extends BaseT;
    virtual task observe(bit [7:0] value);
      $display("UH0_STREAM=%0d", value);
    endtask
  endclass
  class import_api_tb extends observe_impl #(import_api_base);
  endclass
  initial begin
    import_api_tb imp = new();
    export_api_if ep = pss_top::type_id().create(imp);
    ep.Root();
    $finish;
  end
endmodule
"""


@pytest.mark.skipif(not HAVE_VERILATOR, reason="verilator not available")
def test_sv_native_stream_delivers_payload_before_consumer_body(tmp_path):
    """A stream binding delivers the producer value to its parallel consumer."""
    out = _compile_native(tmp_path, _STREAM_PSS, _STREAM_TB)
    rc, log = verilator_build_run(
        out,
        top_module="tb",
        files=["zsp_rt_pkg.sv", "zsp_gen_pkg.sv", "tb.sv"],
    )
    assert rc == 0, log
    assert "UH0_STREAM=165" in log
