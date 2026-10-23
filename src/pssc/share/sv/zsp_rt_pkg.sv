// ==========================================================================
// zsp_rt_pkg -- PSS-to-SV Runtime Library
//
// Base classes and utilities for generated PSS SystemVerilog code.
// This package is simulator-independent (VCS, Questa, Xcelium, etc.).
// ==========================================================================

`ifndef ZSP_RT_PKG_SV
`define ZSP_RT_PKG_SV

// --------------------------------------------------------------------------
// Trace macros (usable outside the package)
// --------------------------------------------------------------------------

`define ZSP_TRACE(msg) \
    if (zsp_rt_pkg::zsp_rt_verbosity >= 1) \
        $display("%0t: [ZSP] %s", $time, msg)

`define ZSP_TRACE_ACTION(act_name, comp_name) \
    if (zsp_rt_pkg::zsp_rt_verbosity >= 1) \
        $display("%0t: [ZSP] Action %s on %s", $time, act_name, comp_name)

`define ZSP_TRACE_RESOURCE(op, pool_name, id) \
    if (zsp_rt_pkg::zsp_rt_verbosity >= 2) \
        $display("%0t: [ZSP] Resource %s %s[%0d]", $time, op, pool_name, id)

package zsp_rt_pkg;

    // ----------------------------------------------------------------------
    // Verbosity control (0 = silent, 1 = actions, 2 = resources)
    // ----------------------------------------------------------------------
    int zsp_rt_verbosity = 1;

    // ======================================================================
    // zsp_component -- base for all generated component classes
    // ======================================================================
    class zsp_component;
        string name;
        zsp_component parent;
        semaphore atomic_sem;

        function new(string name = "", zsp_component parent = null);
            this.name = name;
            this.parent = parent;
            this.atomic_sem = new(1);
        endfunction

        function string get_full_name();
            if (parent != null)
                return {parent.get_full_name(), ".", name};
            return name;
        endfunction
    endclass

    // ======================================================================
    // Executor support
    //
    // Generated code uses mangled PSS standard-library names because the
    // sv-native backend emits one package. A concrete executor binding lives
    // in generated SV; this runtime owns only identity, grouping, and the
    // current target context.
    // ======================================================================
    class executor_pkg__executor_trait_s;
    endclass

    class executor_pkg__executor_base_c extends zsp_component;
        static executor_pkg__executor_base_c current_executor;

        function new(string name = "", zsp_component parent = null);
            super.new(name, parent);
        endfunction

        static function void set_current(executor_pkg__executor_base_c value);
            current_executor = value;
        endfunction

        static function executor_pkg__executor_base_c current();
            return current_executor;
        endfunction
    endclass

    class executor_pkg__executor_c extends executor_pkg__executor_base_c;
        function new(string name = "", zsp_component parent = null);
            super.new(name, parent);
        endfunction
    endclass

    class executor_pkg__executor_group_c extends zsp_component;
        executor_pkg__executor_base_c executors[$];

        function new(string name = "", zsp_component parent = null);
            super.new(name, parent);
        endfunction

        function void add_executor(executor_pkg__executor_base_c value);
            executors.push_back(value);
        endfunction

        function int size();
            return executors.size();
        endfunction

        function executor_pkg__executor_base_c get(int index);
            if (index < 0 || index >= executors.size()) begin
                $fatal(1, "executor group index %0d is unbound", index);
                return null;
            end
            return executors[index];
        endfunction
    endclass

    function automatic executor_pkg__executor_base_c executor();
        executor_pkg__executor_base_c value;
        value = executor_pkg__executor_base_c::current();
        if (value == null)
            $fatal(1, "PSS executor() used without a bound executor context");
        return value;
    endfunction

    // ======================================================================
    // pss_action_run_if -- IP-independent generic-run facade (Command pattern).
    //
    // A generated <Action>_runner binds one export-API entry point (and, in
    // future, its arguments) behind this uniform run(), so a testbench can build
    // and replay a heterogeneous, ordered list of action invocations:
    //   pss_action_run_if program[$];
    //   program.push_back(pss_top::Entry_runner::create(ep));
    //   foreach (program[i]) program[i].run();
    // ======================================================================
    interface class pss_action_run_if;
        pure virtual task run();
    endclass

    // ======================================================================
    // zsp_action -- base for all generated action classes
    // ======================================================================
    class zsp_action;
        zsp_component comp_base;

        virtual task body();
        endtask

        // Default no-op activity. Compound actions override this with a
        // generated traversal; atomic actions inherit this default which
        // falls back to body(), so activity() is callable regardless.
        virtual task activity();
            body();
        endtask

        virtual function void pre_solve();
        endfunction

        virtual function void post_solve();
        endfunction
    endclass

    // ======================================================================
    // Flow-object base classes
    // ======================================================================
    class zsp_resource;
        int instance_id;
    endclass

    class executor_pkg__executor_claim_s extends zsp_resource;
    endclass

    class zsp_buffer;
    endclass

    class zsp_stream;
    endclass

    class zsp_state;
    endclass

    // ======================================================================
    // zsp_resource_pool -- parameterized pool with lock/share semantics
    // ======================================================================
    class zsp_resource_pool #(type T = zsp_resource);
        T instances[];
        bit lock_held[];
        int share_count[];
        semaphore lock_sem[];

        function new(int pool_size);
            instances   = new[pool_size];
            lock_held   = new[pool_size];
            share_count = new[pool_size];
            lock_sem    = new[pool_size];
            foreach (instances[i]) begin
                instances[i] = new();
                instances[i].instance_id = i;
                lock_held[i]   = 0;
                share_count[i] = 0;
                lock_sem[i]    = new(1);
            end
        endfunction

        function int pool_size();
            return instances.size();
        endfunction

        // Non-blocking lock attempt
        function bit try_lock(int id);
            if (!lock_held[id] && share_count[id] == 0) begin
                lock_held[id] = 1;
                return 1;
            end
            return 0;
        endfunction

        // Blocking lock (waits on semaphore)
        task lock(int id);
            lock_sem[id].get(1);
            lock_held[id] = 1;
        endtask

        // Immediate lock (head-action: resource already pre-assigned)
        function void force_lock(int id);
            lock_held[id] = 1;
            void'(lock_sem[id].try_get(1));
        endfunction

        function void unlock(int id);
            lock_held[id] = 0;
            lock_sem[id].put(1);
        endfunction

        // Return a handle to the resource at index id
        function T get(int id);
            return instances[id];
        endfunction

        // Non-blocking share attempt
        function bit try_share(int id);
            if (!lock_held[id]) begin
                share_count[id]++;
                return 1;
            end
            return 0;
        endfunction

        function void unshare(int id);
            if (share_count[id] > 0)
                share_count[id]--;
        endfunction

        // Claim any free instance for exclusive use; returns its id, or -1 if
        // none is available. Atomic (a function, so concurrent callers in a
        // fork serialize and obtain distinct ids).
        function int claim();
            for (int i = 0; i < instances.size(); i++)
                if (try_lock(i)) return i;
            return -1;
        endfunction

        // Claim any instance for shared (read) use; returns its id, or -1.
        function int claim_shared();
            for (int i = 0; i < instances.size(); i++)
                if (try_share(i)) return i;
            return -1;
        endfunction
    endclass

    // ======================================================================
    // zsp_stream_channel -- mailbox-based channel for stream flow objects
    // ======================================================================
    class zsp_stream_channel #(type T = zsp_stream);
        mailbox #(T) mbx;

        function new();
            mbx = new(1);
        endfunction

        task put(T item);
            mbx.put(item);
        endtask

        task get(output T item);
            mbx.get(item);
        endtask
    endclass

    // ======================================================================
    // zsp_state_pool -- persistent state with read/write access
    // ======================================================================
    class zsp_state_pool #(type T = zsp_state);
        T current;
        T previous;
        bit initialized;
        semaphore rw_sem;

        function new();
            current = new();
            initialized = 0;
            rw_sem = new(1);
        endfunction

        task write(T new_val);
            rw_sem.get(1);
            previous = current;
            current = new_val;
            initialized = 1;
            rw_sem.put(1);
        endtask

        function T read();
            return current;
        endfunction

        function bit is_initialized();
            return initialized;
        endfunction
    endclass

endpackage : zsp_rt_pkg

`endif // ZSP_RT_PKG_SV
