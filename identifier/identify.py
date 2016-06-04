from angrop import rop_utils

from collections import defaultdict

from functions import Functions
from errors import IdentifierException
from runner import Runner

import logging
l = logging.getLogger("identifier.identify")
l.setLevel("DEBUG")

NUM_TESTS = 10

# FIXME CFGFAST??

class FuncInfo(object):
    def __init__(self):
        self.stack_vars = None
        self.stack_var_accesses = None
        self.frame_size = None
        self.pushed_regs = None
        self.stack_args = None
        self.stack_arg_accesses = None
        self.buffers = None
        self.var_args = None


class Identifier(object):

    _special_case_funcs = ["free", "realloc"]

    def __init__(self, project):
        self.project = project
        # FIXME use CFGFast when it works
        self._cfg = project.analyses.CFGAccurate()
        self._runner = Runner(project)

        # reg list
        a = self.project.arch
        self._sp_reg = a.register_names[a.sp_offset]
        self._bp_reg = a.register_names[a.bp_offset]
        self._ip_reg = a.register_names[a.ip_offset]
        self._reg_list = a.default_symbolic_registers
        self._reg_list = filter(lambda r: r != self._sp_reg, self._reg_list)
        self._reg_list = filter(lambda r: r != self._ip_reg, self._reg_list)

        self.matches = dict()

        self._func_info = dict()

        for f in self._cfg.functions.values():
            if f.is_syscall:
                continue
            match = self.identify_func(f)
            if match is not None:
                match_func = match
                match_name = match_func.get_name()
                if f.name is not None:
                    l.debug("Found match for function %s at %#x, %s", f.name, f.addr, match_name)
                else:
                    l.debug("Found match for function %#x, %s", f.addr, match_name)
                self.matches[f] = match_name, match_func
            else:
                if f.name is not None:
                    l.debug("No match for function %s at %#x", f.name, f.addr)
                else:
                    l.debug("No match for function %#x", f.addr)

        # Special case functions
        for name in Identifier._special_case_funcs:
            func = Functions[name]()
            for f in self._cfg.functions.values():
                if f in self.matches:
                    continue
                if f not in self._func_info:
                    continue
                if len(self._func_info[f].stack_args) != func.num_args():
                    continue

                if func.try_match(f, self, self._runner):
                    self.matches[f] = func.get_name(), func


    @staticmethod
    def constrain_all_zero(before_state, state, regs):
        for r in regs:
            state.add_constraints(before_state.registers.load(r) == 0)

    def identify_func(self, function):
        if function.is_syscall:
            return None
        try:
            func_info = self.find_stack_vars_x86(function)
            self._func_info[function] = func_info

        except IdentifierException as e:
            l.warning("Identifier Exception: %s", e.message)
            return None
        for name, f in Functions.iteritems():
            # generate an object of the class
            f = f()
            # test it
            if f.num_args() != len(func_info.stack_args) or f.var_args() != func_info.var_args:
                continue
            print "testing:", name
            if not self.check_tests(function, f):
                continue
            # match!
            return f

        return None

    def check_tests(self, cfg_func, match_func):
        if not match_func.pre_test(cfg_func, self._runner):
            return False
        for i in xrange(NUM_TESTS):
            test_data = match_func.gen_input_output_pair()
            if test_data is not None and not self._runner.test(cfg_func, test_data):
                print "failed test 2"
                return False
        return True

    @staticmethod
    def get_reg_name(arch, reg_offset):
        """
        :param arch: the architecture
        :param reg_offset: Tries to find the name of a register given the offset in the registers.
        :return: The register name
        """
        # todo does this make sense
        if reg_offset is None:
            return None

        original_offset = reg_offset
        while reg_offset >= 0 and reg_offset >= original_offset - (arch.bits/8):
            if reg_offset in arch.register_names:
                return arch.register_names[reg_offset]
            else:
                reg_offset -= 1
        return None

    @staticmethod
    def _make_regs_symbolic(input_state, reg_list, project):
        """
        converts an input state into a state with symbolic registers
        :return: the symbolic state
        """
        state = input_state.copy()
        # overwrite all registers
        for reg in reg_list:
            state.registers.store(reg, state.se.BVS("sreg_" + reg + "-", project.arch.bits))
        # restore sp
        state.regs.sp = input_state.regs.sp
        # restore bp
        state.regs.bp = input_state.regs.bp
        return state

    def find_stack_vars_x86(self, func):
        # could also figure out if args are buffers etc
        # doesn't handle dynamically allocated stack, etc
        if isinstance(func, (int, long)):
            func = self._cfg.functions[func]

        if func.startpoint is None:
            raise IdentifierException("Startpoint is None")

        initial_state = rop_utils.make_symbolic_state(self.project, self._reg_list)
        initial_state.regs.bp = initial_state.se.BVS("sreg_" + "ebp" + "-", self.project.arch.bits)

        reg_dict = dict()
        for r in self._reg_list + [self._bp_reg]:
            reg_dict[hash(initial_state.registers.load(r))] = r

        initial_state.regs.ip = func.startpoint.addr
        initial_path = self.project.factory.path(initial_state)

        # find index where stack value is constant
        initial_path.step()
        succ = (initial_path.successors + initial_path.unconstrained_successors)[0]

        if succ.state.scratch.jumpkind == "Ijk_Call":
            goal_sp = succ.state.se.any_int(succ.state.regs.sp + self.project.arch.bytes)
        elif succ.state.scratch.jumpkind == "Ijk_Ret":
            # here we need to know the min sp val
            min_sp = initial_state.se.any_int(initial_state.regs.sp)
            for i in xrange(self.project.factory.block(func.startpoint.addr).instructions):
                test_p = self.project.factory.path(initial_state)
                test_p.step(num_inst=i)
                succ = (initial_path.successors + initial_path.unconstrained_successors)[0]
                test_sp = succ.state.se.any_int(succ.state.regs.sp)
                if test_sp < min_sp:
                    min_sp = test_sp
                elif test_sp > min_sp:
                    break
            goal_sp = min_sp
        else:
            goal_sp = succ.state.se.any_int(succ.state.regs.sp)

        # find the end of the preamble
        num_preamble_inst = None
        succ = None
        for i in xrange(1, self.project.factory.block(func.startpoint.addr).instructions):
            test_p = self.project.factory.path(initial_state)
            test_p.step(num_inst=i)
            succ = (test_p.successors + test_p.unconstrained_successors)[0]
            test_sp = succ.state.se.any_int(succ.state.regs.sp)
            if test_sp == goal_sp:
                num_preamble_inst = i
                break
        min_sp = goal_sp
        initial_sp = initial_state.se.any_int(initial_state.regs.sp)
        frame_size = initial_sp - min_sp - self.project.arch.bytes
        if num_preamble_inst is None or succ is None:
            raise IdentifierException("preamble checks failed")

        if succ.state.se.any_n_int((initial_path.state.regs.sp - succ.state.regs.bp), 2) == \
                [self.project.arch.bytes]:
            bp_based = True
        else:
            bp_based = False

        main_state = self._make_regs_symbolic(succ.state, self._reg_list, self.project)
        if bp_based:
            main_state = self._make_regs_symbolic(main_state, [self._bp_reg], self.project)

        pushed_regs = []
        for a in succ.last_actions:
            if a.type == "mem" and a.action == "write":
                addr = succ.state.se.any_int(a.addr.ast)
                if min_sp <= addr <= initial_sp:
                    if hash(a.data.ast) in reg_dict:
                        pushed_regs.append(reg_dict[hash(a.data.ast)])
        pushed_regs = pushed_regs[::-1]
        # found the preamble

        # find the ends of the function
        ends = set()
        all_end_addrs = set()
        preamble_block = self.project.factory.block(func.startpoint.addr, num_inst=num_preamble_inst)
        preamble_addrs = set(preamble_block.instruction_addrs)
        end_preamble = func.startpoint.addr + preamble_block.vex.size
        for block in func.endpoints:
            addr = block.addr
            if addr in preamble_addrs:
                addr = end_preamble
            if self.project.factory.block(addr).vex.jumpkind == "Ijk_Ret":
                main_state.ip = addr
                test_p = self.project.factory.path(main_state)
                test_p.step()
                for a in (test_p.successors + test_p.unconstrained_successors)[0].last_actions:
                    if a.type == "reg" and a.action == "write":
                        if self.get_reg_name(self.project.arch, a.offset) == self._sp_reg:
                            ends.add(a.ins_addr)
                            all_end_addrs.update(set(self.project.factory.block(a.ins_addr).instruction_addrs))

        bp_sp_diff = None
        if bp_based:
            bp_sp_diff = main_state.se.any_int(main_state.regs.bp - main_state.regs.sp)

        all_addrs = set()
        for bl in func.blocks:
            all_addrs.update(set(self.project.factory.block(bl.addr).instruction_addrs))

        sp = main_state.se.BVS("sym_sp", self.project.arch.bits, explicit_name=True)
        main_state.regs.sp = sp
        bp = None
        if bp_based:
            bp = main_state.se.BVS("sym_bp", self.project.arch.bits, explicit_name=True)
            main_state.regs.bp = bp

        stack_vars = set()
        stack_var_accesses = defaultdict(set)
        buffers = set()
        possible_stack_vars = []
        for addr in all_addrs - all_end_addrs - preamble_addrs:
            main_state.ip = addr
            test_p = self.project.factory.path(main_state)
            test_p.step(num_inst=1)
            succ = (test_p.successors + test_p.unconstrained_successors)[0]

            # skip callsites
            if succ.jumpkind == "Ijk_Call":
                continue
            # we can get stack variables via memory actions
            for a in succ.last_actions:
                if a.type == "mem":
                    if "sym_sp" in a.addr.ast.variables or (bp_based and "sym_bp" in a.addr.ast.variables):
                        possible_stack_vars.append((addr, a.addr.ast, a.action))

            # stack variables can also be if a stack addr is loaded into a register, eg lea
            for r in self._reg_list:
                if r == self._bp_reg and bp_based:
                    continue
                ast = succ.state.registers.load(r)
                if "sym_sp" in ast.variables or (bp_based and "sym_bp" in ast.variables):
                    possible_stack_vars.append((addr, ast, "load"))

        for addr, ast, action in possible_stack_vars:
            if "sym_sp" in ast.variables:
                # constrain all to be zero so we detect the base address of buffers
                self.constrain_all_zero(test_p.state, succ.state, self._reg_list)
                if succ.state.se.symbolic(succ.state.se.simplify(ast - sp)):
                    is_buffer = True
                else:
                    is_buffer = False
                sp_off = succ.state.se.any_int(ast - sp)
                if sp_off > 2 ** (self.project.arch.bits - 1):
                    sp_off = 2 ** self.project.arch.bits - sp_off
                # todo what to do if not bp based
                if bp_based:
                    bp_off = sp_off - bp_sp_diff
                else:
                    bp_off = sp_off - (initial_sp-min_sp) + self.project.arch.bytes

                stack_var_accesses[bp_off].add((addr, action))
                stack_vars.add(bp_off)

                if is_buffer:
                    buffers.add(bp_off)
            else:
                self.constrain_all_zero(test_p.state, succ.state, self._reg_list)
                if succ.state.se.symbolic(succ.state.se.simplify(ast - bp)):
                    is_buffer = True
                else:
                    is_buffer = False
                bp_off = succ.state.se.any_int(ast - bp)
                if bp_off > 2 ** (self.project.arch.bits - 1):
                    bp_off = -(2 ** self.project.arch.bits - bp_off)
                stack_var_accesses[bp_off].add((addr, action))
                stack_vars.add(bp_off)
                if is_buffer:
                    buffers.add(bp_off)

        stack_args = list()
        stack_arg_accesses = defaultdict(set)
        for v in stack_vars:
            if v > 0:
                stack_args.append(v - self.project.arch.bytes * 2)
                stack_arg_accesses[v - self.project.arch.bytes * 2] = stack_var_accesses[v]
        stack_args = sorted(stack_args)
        stack_vars = sorted(stack_vars)

        if len(stack_args) > 0 and any(a[1] == "load" for a in stack_arg_accesses[stack_args[-1]]):
            # print "DETECTED VAR_ARGS"
            var_args = True
            del stack_arg_accesses[stack_args[-1]]
            stack_args = stack_args[:-1]
        else:
            var_args = False

        # return it all in a function info object
        func_info = FuncInfo()
        func_info.stack_vars = stack_vars
        func_info.stack_var_accesses = stack_var_accesses
        func_info.frame_size = frame_size
        func_info.pushed_regs = pushed_regs
        func_info.stack_args = stack_args
        func_info.stack_arg_accesses = stack_arg_accesses
        func_info.buffers = buffers
        func_info.var_args = var_args

        return func_info