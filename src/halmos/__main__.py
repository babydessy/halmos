# SPDX-License-Identifier: AGPL-3.0

import faulthandler
import gc
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from collections.abc import Iterable, Iterator
from concurrent.futures import Future
from dataclasses import asdict, dataclass
from datetime import timedelta
from enum import Enum
from importlib import metadata

import rich
from z3 import (
    BitVec,
    ZeroExt,
    eq,
    set_option,
    unsat,
)

import halmos.traces

from .build import (
    build_output_iterator,
    import_libs,
    parse_build_out,
    parse_devdoc,
    parse_natspec,
)
from .bytevec import ByteVec
from .calldata import FunctionInfo, get_abi, mk_calldata
from .cheatcodes import snapshot_state
from .config import Config as HalmosConfig
from .config import arg_parser, default_config, resolve_config_files, toml_parser
from .constants import (
    VERBOSITY_TRACE_CONSTRUCTOR,
    VERBOSITY_TRACE_COUNTEREXAMPLE,
    VERBOSITY_TRACE_PATHS,
    VERBOSITY_TRACE_SETUP,
)
from .exceptions import FailCheatcode, HalmosException
from .logs import (
    COUNTEREXAMPLE_INVALID,
    COUNTEREXAMPLE_UNKNOWN,
    INTERNAL_ERROR,
    LOOP_BOUND,
    REVERT_ALL,
    debug,
    error,
    logger,
    logger_unique,
    progress_status,
    warn,
    warn_code,
)
from .mapper import BuildOut, DeployAddressMapper
from .processes import ExecutorRegistry, ShutdownError
from .sevm import (
    EMPTY_BALANCE,
    FOUNDRY_CALLER,
    FOUNDRY_ORIGIN,
    FOUNDRY_TEST,
    ONE,
    SEVM,
    ZERO,
    Block,
    CallContext,
    Contract,
    Exec,
    Message,
    Path,
    Profiler,
    SMTQuery,
    id_str,
    jumpid_str,
    mnemonic,
)
from .solve import (
    ContractContext,
    FunctionContext,
    PathContext,
    SolverOutput,
    solve_end_to_end,
    solve_low_level,
)
from .traces import render_trace, rendered_call_sequence, rendered_trace
from .utils import (
    EVM,
    Address,
    BitVecSort256,
    NamedTimer,
    address,
    color_error,
    con,
    create_solver,
    cyan,
    green,
    hexify,
    indent_text,
    red,
    uid,
    unbox_int,
    yellow,
)

faulthandler.enable()


# Python version >=3.8.14, >=3.9.14, >=3.10.7, or >=3.11
if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)

# we need to be able to process at least the max message depth (1024)
sys.setrecursionlimit(1024 * 4)

# sometimes defaults to cp1252 on Windows, which can cause UnicodeEncodeError
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass(frozen=True)
class TestResult:
    name: str  # test function name (funsig)
    exitcode: int
    num_models: int = None
    models: list[SolverOutput] = None
    num_paths: tuple[int, int, int] = None  # number of paths: [total, success, blocked]
    time: tuple[int, int, int] = None  # time: [total, paths, models]
    num_bounded_loops: int = None  # number of incomplete loops


class Exitcode(Enum):
    PASS = 0
    COUNTEREXAMPLE = 1
    TIMEOUT = 2
    STUCK = 3
    REVERT_ALL = 4
    EXCEPTION = 5


PASS = Exitcode.PASS.value
COUNTEREXAMPLE = Exitcode.COUNTEREXAMPLE.value


def with_devdoc(args: HalmosConfig, fn_sig: str, contract_json: dict) -> HalmosConfig:
    devdoc = parse_devdoc(fn_sig, contract_json)
    if not devdoc:
        return args

    overrides = arg_parser().parse_args(devdoc.split())
    return args.with_overrides(source=fn_sig, **vars(overrides))


def with_natspec(
    args: HalmosConfig, contract_name: str, contract_natspec: str
) -> HalmosConfig:
    if not contract_natspec:
        return args

    parsed = parse_natspec(contract_natspec)
    if not parsed:
        return args

    overrides = arg_parser().parse_args(parsed.split())
    return args.with_overrides(source=contract_name, **vars(overrides))


def load_config(_args) -> HalmosConfig:
    config = default_config()

    if not config.solver_command:
        warn(
            "could not find z3 on the PATH -- check your PATH/venv or pass --solver-command explicitly"
        )

    # parse CLI args first, so that can get `--help` out of the way and resolve `--debug`
    # but don't apply the CLI overrides yet
    cli_overrides = arg_parser().parse_args(_args)

    # then for each config file, parse it and override the args
    config_files = resolve_config_files(_args)
    for config_file in config_files:
        if not os.path.exists(config_file):
            error(f"Config file not found: {config_file}")
            sys.exit(2)

        overrides = toml_parser().parse_file(config_file)
        config = config.with_overrides(source=config_file, **overrides)

    # finally apply the CLI overrides
    config = config.with_overrides(source="command line args", **vars(cli_overrides))

    return config


def mk_block() -> Block:
    # foundry default values
    block = Block(
        basefee=ZERO,
        chainid=con(31337),
        coinbase=address(0),
        difficulty=ZERO,
        gaslimit=con(2**63 - 1),
        number=ONE,
        timestamp=ONE,
    )
    return block


def mk_addr(name: str) -> Address:
    return BitVec(name, 160)


def mk_solver(args: HalmosConfig, logic="QF_AUFBV", ctx=None):
    return create_solver(
        logic=logic,
        ctx=ctx,
        timeout=args.solver_timeout_branching,
        max_memory=args.solver_max_memory,
    )


def deploy_test(ctx: FunctionContext, sevm: SEVM) -> Exec:
    message = Message(
        target=FOUNDRY_TEST,
        caller=FOUNDRY_CALLER,
        origin=FOUNDRY_ORIGIN,
        value=0,
        data=ByteVec(),
        call_scheme=EVM.CREATE,
    )

    this = FOUNDRY_TEST

    ex = sevm.mk_exec(
        code={this: Contract(b"")},
        storage={this: sevm.mk_storagedata()},
        transient_storage={this: sevm.mk_storagedata()},
        balance=EMPTY_BALANCE,
        block=mk_block(),
        context=CallContext(message=message),
        pgm=None,  # to be added
        path=Path(ctx.solver),
    )

    # foundry default balance for the test contract
    ex.balance_update(this, con(0xFFFFFFFFFFFFFFFFFFFFFFFF))

    # deploy libraries and resolve library placeholders in hexcode
    contract_ctx = ctx.contract_ctx
    (creation_hexcode, _) = ex.resolve_libs(
        contract_ctx.creation_hexcode, contract_ctx.deployed_hexcode, contract_ctx.libs
    )

    # test contract creation bytecode
    creation_bytecode = Contract.from_hexcode(creation_hexcode)
    ex.pgm = creation_bytecode

    # create test contract
    exs = list(sevm.run(ex))

    # sanity check
    if len(exs) != 1:
        raise ValueError(f"constructor: # of paths: {len(exs)}")

    [ex] = exs

    if ctx.args.verbose >= VERBOSITY_TRACE_CONSTRUCTOR:
        print("Constructor trace:")
        render_trace(ex.context)

    error_output = ex.context.output.error
    returndata = ex.context.output.data
    if error_output:
        raise ValueError(
            f"constructor failed, error={error_output} returndata={returndata}"
        )

    deployed_bytecode = Contract(returndata)
    ex.set_code(this, deployed_bytecode)
    ex.pgm = deployed_bytecode

    # reset vm state
    ex.reset()

    return ex


def setup(ctx: FunctionContext) -> Exec:
    setup_timer = NamedTimer("setup")
    setup_timer.create_subtimer("decode")

    args, setup_info = ctx.args, ctx.info
    sevm = SEVM(args, setup_info)
    setup_ex = deploy_test(ctx, sevm)

    setup_timer.create_subtimer("run")

    setup_sig = setup_info.sig
    if not setup_sig:
        if args.statistics:
            print(setup_timer.report())
        return setup_ex

    # TODO: dyn_params may need to be passed to mk_calldata in run()
    calldata, dyn_params = mk_calldata(ctx.contract_ctx.abi, setup_info, args)
    setup_ex.path.process_dyn_params(dyn_params)

    setup_ex.context = CallContext(
        message=Message(
            target=FOUNDRY_TEST,
            caller=FOUNDRY_CALLER,
            origin=FOUNDRY_ORIGIN,
            value=0,
            data=calldata,
            call_scheme=EVM.CALL,
        ),
    )

    setup_exs_all = sevm.run(setup_ex)
    setup_exs_no_error: list[tuple[Exec, SMTQuery]] = []

    for path_id, setup_ex in enumerate(setup_exs_all):
        if args.verbose >= VERBOSITY_TRACE_SETUP:
            print(f"{setup_sig} trace #{path_id}:")
            render_trace(setup_ex.context)

        if err := setup_ex.context.output.error:
            opcode = setup_ex.current_opcode()
            if opcode not in [EVM.REVERT, EVM.INVALID]:
                warn_code(
                    INTERNAL_ERROR,
                    f"in {setup_sig}, executing {mnemonic(opcode)} failed with: {err}",
                )

            # only render the trace if we didn't already do it
            if VERBOSITY_TRACE_COUNTEREXAMPLE <= args.verbose < VERBOSITY_TRACE_SETUP:
                print(f"{setup_sig} trace:")
                render_trace(setup_ex.context)

        else:
            # note: ex.path.to_smt2() needs to be called at this point. The solver object is shared across paths,
            # and solver.to_smt2() will return a different query if it is called after a different path is explored.
            setup_exs_no_error.append((setup_ex, setup_ex.path.to_smt2(args)))

    setup_exs: list[Exec] = []

    match setup_exs_no_error:
        case []:
            pass
        case [(ex, _)]:
            setup_exs.append(ex)
        case _:
            for path_id, (ex, query) in enumerate(setup_exs_no_error):
                path_ctx = PathContext(
                    args=args,
                    path_id=path_id,
                    query=query,
                    solving_ctx=ctx.solving_ctx,
                )
                solver_output = solve_low_level(path_ctx)
                if solver_output.result != unsat:
                    setup_exs.append(ex)
                    if len(setup_exs) > 1:
                        break

    match len(setup_exs):
        case 0:
            raise HalmosException(f"No successful path found in {setup_sig}")
        case n if n > 1:
            debug("\n".join(map(str, setup_exs)))
            raise HalmosException(f"Multiple paths were found in {setup_sig}")

    [setup_ex] = setup_exs

    if args.print_setup_states:
        print(setup_ex)

    if sevm.logs.bounded_loops:
        warn_code(
            LOOP_BOUND,
            f"{setup_sig}: paths have not been fully explored due to the loop unrolling bound: {args.loop}",
        )
        debug("\n".join(jumpid_str(x) for x in sevm.logs.bounded_loops))

    if args.statistics:
        print(setup_timer.report())

    return setup_ex


def is_global_fail_set(context: CallContext) -> bool:
    hevm_fail = isinstance(context.output.error, FailCheatcode)
    return hevm_fail or any(is_global_fail_set(x) for x in context.subcalls())


def get_state_id(ex: Exec) -> bytes:
    """
    Computes the state snapshot hash, incorporating constraints on state variables.

    Assumes constraints on state variables have been precomputed by running Exec.path_slice() after completing a transaction.
    Do not use this during transaction execution.
    """
    return snapshot_state(ex, include_path=True).unwrap()


def run_target_contract(
    ctx: ContractContext, ex: Exec, addr: Address
) -> Iterator[Exec]:
    """
    Executes a given contract from a given input state and yields all output states.

    Args:
        ctx: The context of the test contract, which differs from the target contract to be executed.
        ex: The input state.
        addr: The address of the contract to be executed.

    Returns:
        A generator of output states.

    Raises:
        ValueError: If the contract name cannot be found for the given address.
    """
    args = ctx.args

    # retrieve the contract name and metadata from the given address
    code = ex.code[addr]
    contract_name = code.contract_name
    filename = code.filename

    if not contract_name:
        raise ValueError(f"couldn't find the contract name for: {addr}")

    contract_json = BuildOut().get_by_name(contract_name, filename)
    abi = get_abi(contract_json)
    method_identifiers = contract_json["methodIdentifiers"]

    # iterate over each function in the target contract
    for fun_sig, fun_selector in method_identifiers.items():
        fun_name = fun_sig.split("(")[0]
        fun_info = FunctionInfo(contract_name, fun_name, fun_sig, fun_selector)

        # skip if 'pure' or 'view' function that doesn't change the state
        state_mutability = abi[fun_sig]["stateMutability"]
        if state_mutability in ["pure", "view"]:
            if args.debug:
                print(f"Skipping {fun_name} ({state_mutability})")
            continue

        try:
            # initialize symbolic execution environment
            sevm = SEVM(args, fun_info)
            solver = mk_solver(args)
            path = Path(solver)
            path.extend_path(ex.path)

            # prepare calldata and dynamic parameters
            cd, dyn_params = mk_calldata(
                abi, fun_info, args, new_symbol_id=ex.new_symbol_id
            )
            path.process_dyn_params(dyn_params)

            # create a symbolic tx.origin
            tx_origin = mk_addr(
                f"tx_origin_{id_str(addr)}_{uid()}_{ex.new_symbol_id():>02}"
            )

            # create a symbolic msg.sender
            msg_sender = mk_addr(
                f"msg_sender_{id_str(addr)}_{uid()}_{ex.new_symbol_id():>02}"
            )

            # create a symbolic msg.value
            msg_value = BitVec(
                f"msg_value_{id_str(addr)}_{uid()}_{ex.new_symbol_id():>02}",
                BitVecSort256,
            )

            # construct the transaction message
            message = Message(
                target=addr,
                caller=msg_sender,
                origin=tx_origin,
                value=msg_value,
                data=cd,
                call_scheme=EVM.CALL,
                fun_info=fun_info,
            )

            # execute the transaction and yield output states
            yield from sevm.run_message(ex, message, path)

        except Exception as err:
            error(f"run_target_contract {addr} {fun_sig}: {type(err).__name__}: {err}")
            if args.debug:
                traceback.print_exc()
            continue

        finally:
            reset(solver)


def _compute_frontier(ctx: ContractContext, depth: int) -> Iterator[Exec]:
    """
    Computes the frontier states at a given depth.

    This function iterates over the previous frontier states at `depth - 1` and executes an arbitrary function of an arbitrary target contract from each state.
    The resulting states form the new frontier at the current depth, which are yielded and also stored in the frontier state cache.

    NOTE: this is internal, only to be called by get_frontier().

    Args:
        ctx: The contract context containing the previous frontier states and other information.
        depth: The current depth level for which the frontier states are being computed.

    Returns:
        A generator for frontier states at the given depth.
    """
    frontier_states = ctx.frontier_states

    # frontier states at the previous depth, which will be used as input for computing new frontier states at the current depth
    curr_exs = frontier_states[depth - 1]

    # the cache for the new frontier states
    next_exs = []
    frontier_states[depth] = next_exs

    visited = ctx.visited

    panic_error_codes = ctx.args.panic_error_codes

    for idx, pre_ex in enumerate(curr_exs):
        progress_status.update(
            f"depth: {cyan(depth)} | "
            f"starting states: {cyan(len(curr_exs))} | "
            f"unique states: {cyan(len(visited))} | "
            f"frontier states: {cyan(len(next_exs))} | "
            f"completed paths: {cyan(idx)} "
        )

        for addr in pre_ex.code:
            # skip the test contract
            if eq(addr, FOUNDRY_TEST):
                continue

            # execute a target contract
            post_exs = run_target_contract(ctx, pre_ex, addr)

            for post_ex in post_exs:
                subcall = post_ex.context

                # ignore and report if halmos-errored
                if subcall.is_stuck():
                    error(
                        f"{depth=}: addr={hexify(addr)}: {subcall.get_stuck_reason()}"
                    )
                    continue

                # ignore if reverted
                if subcall.output.error:
                    # ignore normal reverts
                    if not post_ex.is_panic_of(panic_error_codes):
                        continue

                    fun_info = post_ex.context.message.fun_info

                    # ignore if the probe has already been reported
                    if fun_info in ctx.probes_reported:
                        continue

                    ctx.probes_reported.add(fun_info)

                    # print error trace
                    sequence = (
                        rendered_call_sequence(post_ex.call_sequence) or "    (empty)\n"
                    )
                    trace = rendered_trace(post_ex.context)
                    msg = f"Assertion failure detected in {fun_info.contract_name}.{fun_info.sig}"
                    print(f"{msg}\nSequence:\n{sequence}\nTrace:\n{trace}")

                    # because this is a reverted state, we don't need to explore it further
                    continue

                # skip if already visited
                post_ex.path_slice()
                post_id = get_state_id(post_ex)
                if post_id in visited:
                    continue

                # update visited set
                # TODO: check path feasibility
                visited.add(post_id)

                # update call sequences
                post_ex.call_sequence = pre_ex.call_sequence + [subcall]

                # update timestamp
                timestamp_name = f"halmos_block_timestamp_depth{depth}_{uid()}"
                post_ex.block.timestamp = ZeroExt(192, BitVec(timestamp_name, 64))
                post_ex.path.append(post_ex.block.timestamp >= pre_ex.block.timestamp)

                # update the frontier states cache and yield the new frontier state
                next_exs.append(post_ex)
                yield post_ex


def get_frontier(ctx: ContractContext, depth: int) -> Iterable[Exec]:
    """
    Retrieves the frontier states at a given depth.

    If the frontier states have already been computed, the cached results are returned.
    Otherwise, the generator from _compute_frontier() is returned.

    NOTE: This is not thread-safe.
    """
    if (frontier := ctx.frontier_states.get(depth)) is not None:
        return frontier

    return _compute_frontier(ctx, depth)


def run_message(
    ctx: FunctionContext, sevm: SEVM, message: Message, dyn_params: list
) -> Iterator[Exec]:
    """
    Executes the given test against all frontier states.

    A frontier state is the result of executing a sequence of arbitrary txs starting from the initial setup state.
    These states are grouped by their tx depth (i.e., the number of txs in a sequence) and cached in ContractContext.frontier_states to avoid re-computation for other tests.

    The max tx depth to consider is specified in FunctionContext, which is given by --invariant-depth for invariant tests, and set to 0 for regular tests.

    For regular tests (where the max tx depth is 0), this function amounts to executing the given test against only the initial setup state.
    """
    args = ctx.args
    contract_ctx = ctx.contract_ctx
    for depth in range(ctx.max_call_depth + 1):
        for ex in get_frontier(contract_ctx, depth):
            try:
                solver = mk_solver(args)

                path = Path(solver)
                path.extend_path(ex.path)
                path.process_dyn_params(dyn_params)

                yield from sevm.run_message(ex, message, path)

            finally:
                # reset any remaining solver states from the default context
                reset(solver)


def run_test(ctx: FunctionContext) -> TestResult:
    args = ctx.args
    fun_info = ctx.info
    funname, funsig = fun_info.name, fun_info.sig
    if args.verbose >= 1:
        print(f"Executing {funname}")

    # set the config for every trace rendered in this test
    halmos.traces.config_context.set(args)

    #
    # prepare calldata
    #

    sevm = SEVM(args, fun_info)

    cd, dyn_params = mk_calldata(ctx.contract_ctx.abi, fun_info, args)

    message = Message(
        target=FOUNDRY_TEST,
        caller=FOUNDRY_CALLER,
        origin=FOUNDRY_ORIGIN,
        value=0,
        data=cd,
        call_scheme=EVM.CALL,
    )

    #
    # run
    #

    timer = NamedTimer("time")
    timer.create_subtimer("paths")

    exs = run_message(ctx, sevm, message, dyn_params)

    normal = 0
    potential = 0
    stuck = []

    def solve_end_to_end_callback(future: Future):
        # beware: this function may be called from threads other than the main thread,
        # so we must be careful to avoid referencing any z3 objects / contexts

        if e := future.exception():
            if isinstance(e, ShutdownError):
                if args.debug:
                    debug(
                        f"ignoring solver callback, executor has been shutdown: {e!r}"
                    )
                return

            error(f"encountered exception during assertion solving: {e!r}")

        #
        # we are done solving, process and triage the result
        #

        solver_output = future.result()
        result, model = solver_output.result, solver_output.model

        if ctx.solving_ctx.executor.is_shutdown():
            # if the thread pool is in the process of shutting down,
            # we want to stop processing remaining models/timeouts/errors, etc.
            return

        # keep track of the solver outputs, so that we can display PASS/FAIL/TIMEOUT/ERROR later
        ctx.solver_outputs.append(solver_output)

        if result == unsat:
            if solver_output.unsat_core:
                ctx.append_unsat_core(solver_output.unsat_core)
            return

        # model could be an empty dict here, so compare to None explicitly
        if model is None:
            warn_code(COUNTEREXAMPLE_UNKNOWN, f"Counterexample: {result}")
            return

        # print counterexample trace
        path_id = solver_output.path_id
        if args.verbose >= VERBOSITY_TRACE_COUNTEREXAMPLE:
            pid_str = f" #{path_id}" if args.verbose >= VERBOSITY_TRACE_PATHS else ""
            print(f"Trace{pid_str}:")
            print(ctx.traces[path_id], end="")

        if model.is_valid:
            print(red(f"Counterexample: {model}"))
            ctx.valid_counterexamples.append(model)

            # we have a valid counterexample, so we are eligible for early exit
            if args.early_exit:
                debug(f"Shutting down {ctx.info.name}'s solver executor")
                ctx.solving_ctx.executor.shutdown(wait=False)
        else:
            warn_str = f"Counterexample (potentially invalid): {model}"
            warn_code(COUNTEREXAMPLE_INVALID, warn_str)

            ctx.invalid_counterexamples.append(model)

        # print call sequence for invariant testing
        if sequence := ctx.call_sequences[path_id]:
            print(f"Sequence:\n{sequence}")

    #
    # consume the sevm.run() generator
    # (actually triggers path exploration)
    #

    path_id = 0  # default value in case we don't enter the loop body
    submitted_futures = []
    for path_id, ex in enumerate(exs):
        # check if early exit is triggered
        if ctx.solving_ctx.executor.is_shutdown():
            if args.debug:
                print("aborting path exploration, executor has been shutdown")
            break

        # cache exec in case we need to print it later
        if args.print_failed_states:
            ctx.exec_cache[path_id] = ex

        if args.verbose >= VERBOSITY_TRACE_PATHS:
            print(f"Path #{path_id}:")
            print(indent_text(hexify(ex.path)))

            print("\nTrace:")
            render_trace(ex.context)

        output = ex.context.output
        error_output = output.error
        panic_found = ex.is_panic_of(args.panic_error_codes)

        if panic_found or (fail_found := is_global_fail_set(ex.context)):
            potential += 1

            if args.verbose >= 1:
                print(f"Found potential path with {path_id=} ", end="")
                if panic_found:
                    panic_code = unbox_int(output.data[4:36].unwrap())
                    print(f"Panic(0x{panic_code:02x}) {error_output}")
                elif fail_found:
                    print(f"(fail flag set) {error_output}")

            # we don't know yet if this will lead to a counterexample
            # so we save the rendered trace here and potentially print it later
            # if a valid counterexample is found
            if args.verbose >= VERBOSITY_TRACE_COUNTEREXAMPLE:
                ctx.traces[path_id] = rendered_trace(ex.context)
            ctx.call_sequences[path_id] = rendered_call_sequence(ex.call_sequence)

            query: SMTQuery = ex.path.to_smt2(args)

            # beware: because this object crosses thread boundaries, we must be careful to
            # avoid any reference to z3 objects
            path_ctx = PathContext(
                args=args,
                path_id=path_id,
                query=query,
                solving_ctx=ctx.solving_ctx,
            )

            try:
                solve_future = ctx.thread_pool.submit(solve_end_to_end, path_ctx)
                solve_future.add_done_callback(solve_end_to_end_callback)
                submitted_futures.append(solve_future)
            except ShutdownError:
                if args.debug:
                    print("aborting path exploration, executor has been shutdown")
                break

        elif ex.context.is_stuck():
            debug(f"Potential error path (id: {path_id})")
            path_ctx = PathContext(
                args=args,
                path_id=path_id,
                query=ex.path.to_smt2(args),
                solving_ctx=ctx.solving_ctx,
            )
            solver_output = solve_low_level(path_ctx)
            if solver_output.result != unsat:
                stuck.append((path_id, ex, ex.context.get_stuck_reason()))
                if args.print_blocked_states:
                    ctx.traces[path_id] = (
                        f"{hexify(ex.path)}\n{rendered_trace(ex.context)}"
                    )

        elif not error_output:
            if args.print_success_states:
                print(f"# {path_id}")
                print(ex)
            normal += 1

        # print post-states
        if args.print_states:
            print(f"# {path_id}")
            print(ex)

        # 0 width is unlimited
        if args.width and path_id >= args.width:
            msg = "incomplete execution due to the specified limit"
            warn(f"{funsig}: {msg}: --width {args.width}")
            break

    num_execs = path_id + 1

    # the name is a bit misleading: this timer only starts after the exploration phase is complete
    # but it's possible that solvers have already been running for a while
    timer.create_subtimer("models")

    if potential > 0 and args.verbose >= 1:
        print(
            f"# of potential paths involving assertion violations: {potential} / {num_execs}"
            f" (--solver-threads {args.solver_threads})"
        )

    #
    # display assertion solving progress
    #

    if not args.no_status:
        while True:
            done = sum(fm.done() for fm in submitted_futures)
            total = potential
            if done == total:
                break
            elapsed = timedelta(seconds=int(timer.elapsed()))
            progress_status.update(f"[{elapsed}] solving queries: {done} / {total}")
            time.sleep(0.1)

    ctx.thread_pool.shutdown(wait=True)

    timer.stop()
    time_info = timer.report(include_subtimers=args.statistics)

    #
    # print test result
    #

    counter = Counter(str(m.result) for m in ctx.solver_outputs)
    if counter["sat"] > 0:
        passfail = red("[FAIL]")
        exitcode = Exitcode.COUNTEREXAMPLE.value
    elif counter["unknown"] > 0:
        passfail = yellow("[TIMEOUT]")
        exitcode = Exitcode.TIMEOUT.value
    elif len(stuck) > 0:
        passfail = red("[ERROR]")
        exitcode = Exitcode.STUCK.value
    elif normal == 0:
        passfail = red("[ERROR]")
        exitcode = Exitcode.REVERT_ALL.value
        warn_code(
            REVERT_ALL,
            f"{funsig}: all paths have been reverted; the setup state or inputs may have been too restrictive.",
        )
    else:
        passfail = green("[PASS]")
        exitcode = Exitcode.PASS.value

    timer.stop()
    time_info = timer.report(include_subtimers=args.statistics)

    # print test result
    print(
        f"{passfail} {funsig} (paths: {num_execs}, {time_info}, "
        f"bounds: [{', '.join([str(x) for x in dyn_params])}])"
    )

    for path_id, _, err in stuck:
        warn_code(INTERNAL_ERROR, f"Encountered {err}")
        if args.print_blocked_states:
            print(f"\nPath #{path_id}")
            print(ctx.traces[path_id], end="")

    logs = sevm.logs
    if logs.bounded_loops:
        warn_code(
            LOOP_BOUND,
            f"{funsig}: paths have not been fully explored due to the loop unrolling bound: {args.loop}",
        )
        debug("\n".join(jumpid_str(x) for x in logs.bounded_loops))

    # return test result
    num_cexes = len(ctx.valid_counterexamples) + len(ctx.invalid_counterexamples)
    if args.minimal_json_output:
        return TestResult(funsig, exitcode, num_cexes)
    else:
        return TestResult(
            funsig,
            exitcode,
            num_cexes,
            ctx.valid_counterexamples + ctx.invalid_counterexamples,
            (num_execs, normal, len(stuck)),
            (timer.elapsed(), timer["paths"].elapsed(), timer["models"].elapsed()),
            len(logs.bounded_loops),
        )


def extract_setup(ctx: ContractContext) -> FunctionInfo:
    methodIdentifiers = ctx.method_identifiers
    setup_sigs = sorted(
        [
            (k, v)
            for k, v in methodIdentifiers.items()
            if k == "setUp()" or k.startswith("setUpSymbolic(")
        ]
    )

    if not setup_sigs:
        return FunctionInfo()

    (setup_sig, setup_selector) = setup_sigs[-1]
    setup_name = setup_sig.split("(")[0]
    return FunctionInfo(ctx.name, setup_name, setup_sig, setup_selector)


def reset(solver):
    if threading.current_thread() != threading.main_thread():
        # can't access z3 objects from other threads
        warn("reset() called from a non-main thread")

    solver.reset()


def run_contract(ctx: ContractContext) -> list[TestResult]:
    BuildOut().set_build_out(ctx.build_out_map)

    args = ctx.args
    setup_info = extract_setup(ctx)

    try:
        setup_config = with_devdoc(args, setup_info.sig, ctx.contract_json)
        setup_solver = mk_solver(setup_config)
        setup_ctx = FunctionContext(
            args=setup_config,
            info=setup_info,
            solver=setup_solver,
            contract_ctx=ctx,
        )

        halmos.traces.config_context.set(setup_config)
        setup_ex = setup(setup_ctx)
        setup_ex.path_slice()
    except Exception as err:
        error(f"{setup_info.sig} failed: {type(err).__name__}: {err}")
        if args.debug:
            traceback.print_exc()

        # reset any remaining solver states from the default context
        reset(setup_solver)

        return []

    # initialize the frontier and visited states using the initial setup state
    ctx.frontier_states[0] = [setup_ex]
    ctx.visited.add(get_state_id(setup_ex))

    test_results = run_tests(ctx, setup_ex, ctx.funsigs)

    # reset any remaining solver states from the default context
    reset(setup_solver)

    return test_results


def run_tests(
    ctx: ContractContext,
    setup_ex: Exec,
    funsigs: list[str],
) -> list[TestResult]:
    """
    Executes each of the given test functions on the given input state.
    Used for both regular and invariant tests.

    Args:
        ctx: The context of the test contract.
        setup_ex: The setup state from which each test will be run.
        funsigs: A list of test function signatures to execute.

    Returns:
        A list of test results.
    """
    args = ctx.args

    test_results = []
    debug_config = args.debug_config

    for funsig in funsigs:
        selector = ctx.method_identifiers[funsig]
        fun_info = FunctionInfo(ctx.name, funsig.split("(")[0], funsig, selector)
        try:
            test_config = with_devdoc(args, funsig, ctx.contract_json)
            if debug_config:
                debug(f"{test_config.formatted_layers()}")

            max_call_depth = (
                test_config.invariant_depth if funsig.startswith("invariant_") else 0
            )

            test_ctx = FunctionContext(
                args=test_config,
                info=fun_info,
                solver=None,
                contract_ctx=ctx,
                setup_ex=setup_ex,
                max_call_depth=max_call_depth,
            )

            test_result = run_test(test_ctx)
        except Exception as err:
            print(f"{color_error('[ERROR]')} {funsig}")
            error(f"{type(err).__name__}: {err}")
            if args.debug:
                traceback.print_exc()
            test_results.append(TestResult(funsig, Exitcode.EXCEPTION.value))
            continue

        test_results.append(test_result)

    return test_results


def contract_regex(args):
    if contract := args.contract:
        return f"^{contract}$"
    else:
        return args.match_contract


def test_regex(args):
    match_test = args.match_test
    if match_test.startswith("^"):
        return match_test
    else:
        return f"^{args.function}.*{match_test}"


@dataclass(frozen=True)
class MainResult:
    exitcode: int
    # contract path -> list of test results
    test_results: dict[str, list[TestResult]] = None


def _main(_args=None) -> MainResult:
    timer = NamedTimer("total")
    timer.create_subtimer("build")

    # clear any remaining live display before starting a new instance
    rich.get_console().clear_live()
    progress_status.start()

    #
    # z3 global options
    #

    set_option(max_width=240)
    set_option(max_lines=10**8)
    # set_option(max_depth=1000)

    #
    # command line arguments
    #

    args = load_config(_args)

    if args.version:
        print(f"halmos {metadata.version('halmos')}")
        return MainResult(0)

    if args.disable_gc:
        gc.disable()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logger_unique.setLevel(logging.DEBUG)

    if args.trace_memory:
        import halmos.memtrace as memtrace

        memtrace.MemTracer.get().start()

    #
    # compile
    #

    build_cmd = [
        "forge",  # shutil.which('forge')
        "build",
        "--ast",
        "--root",
        args.root,
        "--extra-output",
        "storageLayout",
        "metadata",
    ]

    # run forge without capturing stdout/stderr
    debug(f"Running {' '.join(build_cmd)}")

    build_exitcode = subprocess.run(build_cmd).returncode

    if build_exitcode:
        error(f"Build failed: {build_cmd}")
        return MainResult(1)

    timer.create_subtimer("load")
    try:
        build_out = parse_build_out(args)
    except Exception as err:
        error(f"Build output parsing failed: {type(err).__name__}: {err}")
        if args.debug:
            traceback.print_exc()
        return MainResult(1)

    timer.create_subtimer("tests")

    total_passed = 0
    total_failed = 0
    total_found = 0
    test_results_map = {}

    #
    # exit and signal handlers to avoid dropping json output
    #

    def on_exit(exitcode: int) -> MainResult:
        ExecutorRegistry().shutdown_all()

        progress_status.stop()

        result = MainResult(exitcode, test_results_map)

        if args.json_output:
            debug(f"Writing output to {args.json_output}")
            with open(args.json_output, "w") as json_file:
                json.dump(asdict(result), json_file, indent=4)

        return result

    def on_signal(signum, frame):
        debug(f"Signal {signum} received")
        exitcode = 128 + signum
        on_exit(exitcode)
        sys.exit(exitcode)

    for signum in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(signum, on_signal)

    #
    # run
    #

    _contract_regex = contract_regex(args)
    _test_regex = test_regex(args)

    for build_out_map, filename, contract_name in build_output_iterator(build_out):
        if not re.search(_contract_regex, contract_name):
            continue

        (contract_json, contract_type, natspec) = build_out_map[filename][contract_name]
        if contract_type != "contract":
            continue

        methodIdentifiers = contract_json["methodIdentifiers"]
        funsigs = [f for f in methodIdentifiers if re.search(_test_regex, f)]
        num_found = len(funsigs)

        if num_found == 0:
            continue

        contract_timer = NamedTimer("time")

        abi = get_abi(contract_json)
        creation_hexcode = contract_json["bytecode"]["object"]
        deployed_hexcode = contract_json["deployedBytecode"]["object"]
        linkReferences = contract_json["bytecode"]["linkReferences"]
        libs = import_libs(build_out_map, creation_hexcode, linkReferences)

        contract_path = f"{contract_json['ast']['absolutePath']}:{contract_name}"
        print(f"\nRunning {num_found} tests for {contract_path}")

        # Set the test contract address in DeployAddressMapper
        DeployAddressMapper().add_deployed_contract(hexify(FOUNDRY_TEST), contract_name)

        # support for `/// @custom:halmos` annotations
        contract_args = with_natspec(args, contract_name, natspec)
        contract_ctx = ContractContext(
            args=contract_args,
            name=contract_name,
            funsigs=funsigs,
            creation_hexcode=creation_hexcode,
            deployed_hexcode=deployed_hexcode,
            abi=abi,
            method_identifiers=methodIdentifiers,
            contract_json=contract_json,
            libs=libs,
            build_out_map=build_out_map,
        )

        test_results = run_contract(contract_ctx)
        num_passed = sum(r.exitcode == PASS for r in test_results)
        num_failed = num_found - num_passed

        print(
            "Symbolic test result: "
            f"{num_passed} passed; "
            f"{num_failed} failed; "
            f"{contract_timer.report()}"
        )

        total_found += num_found
        total_passed += num_passed
        total_failed += num_failed

        if contract_path in test_results_map:
            raise ValueError("already exists", contract_path)

        test_results_map[contract_path] = test_results

    if args.statistics:
        print(f"\n[time] {timer.report()}")

    if args.profile_instructions:
        profiler = Profiler()
        top_instructions = profiler.get_top_instructions()
        separator = "-" * 26
        print(separator)
        print(f"{'Instruction':<12} {'Count':>12}")
        print(separator)
        for instruction, count in top_instructions:
            print(f"{instruction:<12} {count:>12,}")
        print(separator)
        print(f"{'Total':<12} {profiler.counters.total():>12,}")
        print(separator)

    if total_found == 0:
        error(
            "No tests with"
            + f" --match-contract '{contract_regex(args)}'"
            + f" --match-test '{test_regex(args)}'"
        )
        return MainResult(1)

    exitcode = 0 if total_failed == 0 else 1
    return on_exit(exitcode)


# entrypoint for the `halmos` script
def main() -> int:
    exitcode = _main().exitcode
    return exitcode


# entrypoint for `python -m halmos`
if __name__ == "__main__":
    sys.exit(main())
