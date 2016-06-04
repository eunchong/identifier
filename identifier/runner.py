import simuvex
import simuvex.s_options as so
from simuvex.s_type import SimTypeFunction, SimTypeInt
from .custom_callable import Callable
from angr.errors import AngrCallableMultistateError, AngrCallableError
import claripy
from tracer.simprocedures import FixedOutTransmit, FixedInReceive

import logging
l = logging.getLogger("identifier.runner")
l.setLevel("DEBUG")


class Runner(object):
    def __init__(self, project):
        self.project = project

    def setup_state(self, function, test_data, initial_state=None):
        # FIXME fdwait should do something concrete...
        # FixedInReceive and FixedOutReceive always are applied
        simuvex.SimProcedures['cgc']['transmit'] = FixedOutTransmit
        simuvex.SimProcedures['cgc']['receive'] = FixedInReceive

        fs = {'/dev/stdin': simuvex.storage.file.SimFile(
            "/dev/stdin", "r",
            size=len(test_data.preloaded_stdin))}

        options = set()
        options.add(so.CGC_ZERO_FILL_UNCONSTRAINED_MEMORY)
        options.add(so.CGC_NO_SYMBOLIC_RECEIVE_LENGTH)
        options.add(so.REPLACEMENT_SOLVER)

        # try to enable unicorn, continue if it doesn't exist
        try:
            options.add(so.UNICORN)
            options.add(so.UNICORN_FAST)
            l.info("unicorn tracing enabled")
        except AttributeError:
            pass

        remove_options = so.simplification | set(so.LAZY_SOLVES) | so.symbolic
        add_options = options
        if initial_state is None:
            entry_state = self.project.factory.entry_state(
                    fs=fs,
                    add_options=add_options,
                    remove_options=remove_options)
            entry_state.ip = function.startpoint.addr
        else:
            entry_state = initial_state.copy()
            entry_state.options -= remove_options
            entry_state.options |= add_options

        # set stdin
        entry_state.cgc.input_size = len(test_data.preloaded_stdin)
        if len(test_data.preloaded_stdin) > 0:
            entry_state.posix.files[0].content.store(0, test_data.preloaded_stdin)

        if initial_state is None:
            # map the CGC flag page
            cgc_flag_data = claripy.BVS('cgc-flag-data', 0x1000 * 8)

            # PROT_READ region
            entry_state.memory.map_region(0x4347c000, 0x1000, 1)
            entry_state.memory.store(0x4347c000, cgc_flag_data)

        # make sure unicorn will run
        for k in dir(entry_state.regs):
            r = getattr(entry_state.regs, k)
            if r.symbolic:
                setattr(entry_state.regs, k, 0)
        # FIXME make the cooldowns configurable so we can set them to 0 here
        entry_state.unicorn._register_check_count = 100
        entry_state.unicorn._runs_since_symbolic_data = 100
        entry_state.unicorn._runs_since_unicorn = 100

        return entry_state

    def test(self, function, test_data):
        curr_buf_loc = 0x1000
        mapped_input = []
        s = self.setup_state(function, test_data)

        for i in test_data.input_args:
            if isinstance(i, str):
                s.memory.store(curr_buf_loc, i + "\x00")
                mapped_input.append(curr_buf_loc)
                curr_buf_loc += min(len(i), 0x1000)
            else:
                if not isinstance(i, (int, long)):
                    raise Exception("Expected int/long got %s", type(i))
                mapped_input.append(i)

        inttype = SimTypeInt(self.project.arch.bits, False)
        func_ty = SimTypeFunction([inttype] * len(mapped_input), inttype)
        cc = self.project.factory.cc(func_ty=func_ty)
        try:
            call = Callable(self.project, function.startpoint.addr, concrete_only=True,
                            cc=cc, base_state=s, max_steps=test_data.max_steps)
            result = call(*mapped_input)
            result_state = call.result_state
        except AngrCallableMultistateError as e:
            l.info("multistate error: %s", e.message)
            return False
        except AngrCallableError as e:
            l.info("other callable error: %s", e.message)
            return False

        # check matches
        outputs = []
        for i, out in enumerate(test_data.expected_output_args):
            if isinstance(out, str):
                if len(out) == 0:
                    raise Exception("len 0 out")
                outputs.append(result_state.memory.load(mapped_input[i], len(out)))
            else:
                outputs.append(None)

        tmp_outputs = outputs
        outputs = []
        for out in tmp_outputs:
            if out is None:
                outputs.append(None)
            elif result_state.se.symbolic(out):
                l.info("symbolic memory output")
                return False
            else:
                outputs.append(result_state.se.any_str(out))

        if outputs != test_data.expected_output_args:
            # print map(lambda x: x.encode('hex'), [a for a in outputs if a is not None]), map(lambda x: x.encode('hex'), [a for a in test_data.expected_output_args if a is not None])
            l.info("mismatch output")
            return False

        if result_state.se.symbolic(result):
            l.info("result value sybolic")
            return False

        if test_data.expected_return_val is not None and test_data.expected_return_val < 0:
            test_data.expected_return_val &= (2**self.project.arch.bits - 1)
        if test_data.expected_return_val is not None and \
                result_state.se.any_int(result) != test_data.expected_return_val:
            l.info("return val mismatch got %#x, expected %#x", result_state.se.any_int(result), test_data.expected_return_val)
            return False

        if result_state.se.symbolic(result_state.posix.files[1].pos):
            l.info("symbolic stdout pos")
            return False

        if result_state.se.any_int(result_state.posix.files[1].pos) == 0:
            stdout = ""
        else:
            stdout = result_state.posix.files[1].content.load(0, result_state.posix.files[1].pos)
            if stdout.symbolic:
                l.info("symbolic stdout")
                return False
            stdout = result_state.se.any_str(stdout)

        stdout = stdout[:len(test_data.expected_stdout)]
        if stdout != test_data.expected_stdout:
            l.info("mismatch stdout")
            return False

        return True

    def get_out_state(self, function, test_data, initial_state=None):
        curr_buf_loc = 0x1000
        mapped_input = []
        s = self.setup_state(function, test_data, initial_state)

        for i in test_data.input_args:
            if isinstance(i, str):
                s.memory.store(curr_buf_loc, i)
                mapped_input.append(curr_buf_loc)
                curr_buf_loc += max(len(i), 0x1000)
            else:
                if not isinstance(i, (int, long)):
                    raise Exception("Expected int/long got %s", type(i))
                mapped_input.append(i)

        inttype = SimTypeInt(self.project.arch.bits, False)
        func_ty = SimTypeFunction([inttype] * len(mapped_input), inttype)
        cc = self.project.factory.cc(func_ty=func_ty)
        try:
            call = Callable(self.project, function.startpoint.addr, concrete_only=True,
                            cc=cc, base_state=s, max_steps=test_data.max_steps)
            _ = call(*mapped_input)
            result_state = call.result_state
        except AngrCallableMultistateError as e:
            l.info("multistate error: %s", e.message)
            return None
        except AngrCallableError as e:
            l.info("other callable error: %s", e.message)
            return None

        return result_state