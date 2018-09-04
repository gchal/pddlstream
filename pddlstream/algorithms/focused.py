from __future__ import print_function

import time

from pddlstream.algorithms.algorithm import parse_problem, SolutionStore, has_costs, compile_fluent_streams
from pddlstream.algorithms.incremental import layered_process_stream_queue
from pddlstream.algorithms.instantiation import Instantiator
from pddlstream.algorithms.postprocess import locally_optimize
from pddlstream.algorithms.refine_shared import iterative_solve_stream_plan, optimistic_process_streams
from pddlstream.algorithms.reorder import separate_plan, reorder_combined_plan, reorder_stream_plan
from pddlstream.algorithms.scheduling.relaxed import relaxed_stream_plan
from pddlstream.algorithms.scheduling.simultaneous import simultaneous_stream_plan
from pddlstream.algorithms.skeleton import SkeletonQueue
# from pddlstream.algorithms.scheduling.sequential import sequential_stream_plan
# from pddlstream.algorithms.scheduling.incremental import incremental_stream_plan, exhaustive_stream_plan
from pddlstream.algorithms.visualization import clear_visualizations, create_visualizations
from pddlstream.language.conversion import revert_solution
from pddlstream.language.execution import get_action_info
from pddlstream.language.function import Function, Predicate
from pddlstream.language.statistics import load_stream_statistics, \
    write_stream_statistics
from pddlstream.language.stream import Stream
from pddlstream.language.synthesizer import get_synthetic_stream_plan
from pddlstream.utils import INF, elapsed_time, get_length


# TODO: compute total stream plan p_success and overhead
# TODO: ensure search and sampling have equal time
# TODO: select whether to search or sample based on expected success rates
# TODO: estimate the success rate for a stream_plan given past outcomes
# TODO: make a subroutine that does commit

def partition_externals(externals):
    functions = list(filter(lambda s: type(s) is Function, externals))
    predicates = list(filter(lambda s: type(s) is Predicate, externals)) # and s.is_negative()
    negated_streams = list(filter(lambda s: (type(s) is Stream) and s.is_negated(), externals)) # and s.is_negative()
    negative = predicates + negated_streams
    streams = list(filter(lambda s: s not in (functions + negative), externals))
    return streams, functions, negative

##################################################

def solve_focused(problem, stream_info={}, action_info={}, synthesizers=[],
                  max_time=INF, max_cost=INF, unit_costs=None,
                  effort_weight=None, eager_layers=1,
                  visualize=False, verbose=True, postprocess=False, **search_kwargs):
    """
    Solves a PDDLStream problem by first hypothesizing stream outputs and then determining whether they exist
    :param problem: a PDDLStream problem
    :param action_info: a dictionary from stream name to ActionInfo for planning and execution
    :param stream_info: a dictionary from stream name to StreamInfo altering how individual streams are handled
    :param max_time: the maximum amount of time to apply streams
    :param max_cost: a strict upper bound on plan cost
    :param effort_weight: a multiplier for stream effort compared to action costs
    :param eager_layers: the number of eager stream application layers per iteration
    :param visualize: if True, it draws the constraint network and stream plan as a graphviz file
    :param verbose: if True, this prints the result of each stream application
    :param search_kwargs: keyword args for the search subroutine
    :return: a tuple (plan, cost, evaluations) where plan is a sequence of actions
        (or None), cost is the cost of the plan, and evaluations is init but expanded
        using stream applications
    """
    # TODO: return to just using the highest level samplers at the start
    search_sampling_ratio = 1
    solve_stream_plan_fn = relaxed_stream_plan if effort_weight is None else simultaneous_stream_plan
    #solve_stream_plan_fn = incremental_stream_plan # sequential_stream_plan | incremental_stream_plan | exhaustive_stream_plan
    # TODO: warning check if using simultaneous_stream_plan or sequential_stream_plan with non-eager functions
    num_iterations = 0
    search_time = sample_time = 0
    store = SolutionStore(max_time, max_cost, verbose) # TODO: include other info here?
    evaluations, goal_expression, domain, externals = parse_problem(problem, stream_info)
    compile_fluent_streams(domain, externals)
    if unit_costs is None:
        unit_costs = not has_costs(domain)
    full_action_info = get_action_info(action_info)
    load_stream_statistics(externals + synthesizers)
    if visualize:
        clear_visualizations()
    eager_externals = list(filter(lambda e: e.info.eager, externals))
    streams, functions, negative = partition_externals(externals)
    queue = SkeletonQueue(store, evaluations, domain)
    # TODO: decide max_sampling_time based on total search_time or likelihood estimates
    # TODO: switch to searching if believe chance of search better than sampling
    while not store.is_terminated():
        num_iterations += 1
        print('\nIteration: {} | Queue: {} | Evaluations: {} | Cost: {} '
              '| Search Time: {:.3f} | Sample Time: {:.3f} | Total Time: {:.3f}'.format(
            num_iterations, len(queue), len(evaluations), store.best_cost,
            search_time, sample_time, store.elapsed_time()))

        start_time = time.time()
        layered_process_stream_queue(Instantiator(evaluations, eager_externals), evaluations, store, eager_layers)
        solve_stream_plan = lambda sr: solve_stream_plan_fn(evaluations, goal_expression, domain, sr, negative,
                                                            max_cost=store.best_cost,
                                                            #max_cost=min(store.best_cost, max_cost),
                                                            unit_costs=unit_costs, **search_kwargs)
        #combined_plan, cost = solve_stream_plan(optimistic_process_streams(evaluations, streams + functions))
        combined_plan, cost = iterative_solve_stream_plan(evaluations, streams, functions, solve_stream_plan)
        if action_info:
            combined_plan = reorder_combined_plan(evaluations, combined_plan, full_action_info, domain)
            print('Combined plan: {}'.format(combined_plan))
        stream_plan, action_plan = separate_plan(combined_plan, full_action_info)
        stream_plan = reorder_stream_plan(stream_plan) # TODO: is this strictly redundant?
        stream_plan = get_synthetic_stream_plan(stream_plan, synthesizers)
        print('Stream plan ({}): {}\nAction plan ({}, {}): {}'.format(get_length(stream_plan), stream_plan,
                                                                      get_length(action_plan), cost, action_plan))
        search_time += elapsed_time(start_time)

        start_time = time.time()
        if stream_plan is None:
            if not queue:
                break
            queue.process_until_success()
            #queue.fairly_process()
        else:
            if visualize:
                create_visualizations(evaluations, stream_plan, num_iterations)
            queue.new_skeleton(stream_plan, action_plan, cost)
            queue.greedily_process()
        sample_time += elapsed_time(start_time)

        start_time = time.time()
        queue.timed_process(search_sampling_ratio*search_time - sample_time)
        sample_time += elapsed_time(start_time)

    if postprocess and (not unit_costs):
        locally_optimize(evaluations, store, goal_expression, domain, functions, negative, synthesizers)
    write_stream_statistics(externals + synthesizers, verbose)
    return revert_solution(store.best_plan, store.best_cost, evaluations)