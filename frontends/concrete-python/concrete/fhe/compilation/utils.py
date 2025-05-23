"""
Declaration of various functions and constants related to compilation.
"""

import json
import os
import re
from collections.abc import Iterable
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable, Generic, Optional, TypeVar, Union

import networkx as nx
import numpy as np

from ..dtypes import Float, Integer, SignedInteger, UnsignedInteger
from ..representation import Graph, Node, Operation
from ..tracing import ScalarAnnotation
from ..values import ValueDescription
from .specs import ClientSpecs

if TYPE_CHECKING:
    from .artifacts import FunctionDebugArtifacts  # pragma: no cover

# ruff: noqa: ERA001

T = TypeVar("T")


class Lazy(Generic[T]):
    """
    A lazyly initialized value.

    Allows to prevent executing a costly initialization if the value is not used afterward.
    """

    def __init__(self, init: Callable[[], T]) -> None:
        self._initialized: bool = False
        self._init: Callable[[], T] = init
        self._val: Optional[T] = None

    def init(self) -> None:
        """
        Force initialization of the value.
        """
        if not self._initialized:
            self._val = self._init()
            self._initialized = True

    @property
    def val(self) -> T:
        """
        Initializes the value if needed, and returns it.
        """
        self.init()
        return self._val  # type: ignore

    @property
    def initialized(self) -> bool:
        """
        Returns whether the value has been initialized or not.
        """
        return self._initialized


def inputset(
    *inputs: Union[ScalarAnnotation, ValueDescription, Callable[[int], Any]],
    size: int = 100,
) -> list[tuple[Any, ...]]:
    """
    Generate a random inputset.

    Args:
        *inputs (Union[ScalarAnnotation, ValueDescription, Callable[[int], Any]]):
            specification of each input

        size (int, default = 100):
            size of the inputset

    Returns:
        List[Tuple[Any, ...]]:
            generated inputset
    """

    result: list[tuple[Any, ...]] = []
    for i in range(size):
        sample: list[Any] = []
        for specification in inputs:
            is_value = isinstance(specification, ValueDescription)
            is_scalar_annotation = isinstance(specification, type) and issubclass(
                specification, ScalarAnnotation
            )

            if is_scalar_annotation or is_value:
                dtype = specification.dtype  # type: ignore
                shape = () if is_scalar_annotation else specification.shape  # type: ignore

                if isinstance(dtype, Integer):
                    sample.append(np.random.randint(dtype.min(), dtype.max() + 1, size=shape))
                else:
                    sample.append(np.random.rand(*shape))
            else:
                assert not isinstance(specification, (ScalarAnnotation, ValueDescription))
                sample.append(specification(i))

        result.append(tuple(sample))
    return result


def validate_input_args(
    client_specs: ClientSpecs,
    *args: Optional[Union[int, np.ndarray, list]],
    function_name: str,
) -> list[Optional[Union[int, np.ndarray]]]:
    """Validate input arguments.

    Args:
        client_specs (ClientSpecs):
            client specification
        *args (Optional[Union[int, np.ndarray, List]]):
            argument(s) for evaluation
        function_name (str): name of the function to verify

    Returns:
        List[Optional[Union[int, np.ndarray]]]: ordered validated args
    """

    functions_parameters = json.loads(client_specs.program_info.serialize())["circuits"]
    for function_parameters in functions_parameters:
        if function_parameters["name"] == function_name:
            client_parameters_json = function_parameters
            break
    else:
        message = f"Function `{function_name}` is not in the module"
        raise ValueError(message)

    assert "inputs" in client_parameters_json
    input_specs = client_parameters_json["inputs"]
    if len(args) != len(input_specs):
        message = f"Expected {len(input_specs)} inputs but got {len(args)}"
        raise ValueError(message)

    sanitized_args: dict[int, Optional[Union[int, np.ndarray]]] = {}
    for index, (arg, spec) in enumerate(zip(args, input_specs)):
        if arg is None:
            sanitized_args[index] = None
            continue

        if isinstance(arg, list):
            arg = np.array(arg)

        is_valid = isinstance(arg, (int, np.integer)) or (
            isinstance(arg, np.ndarray) and np.issubdtype(arg.dtype, np.integer)
        )

        if "lweCiphertext" in spec["typeInfo"].keys():
            type_info = spec["typeInfo"]["lweCiphertext"]
            is_encrypted = True
            shape = tuple(type_info["abstractShape"]["dimensions"])
            assert "integer" in type_info["encoding"].keys()
            width = type_info["encoding"]["integer"]["width"]
            is_signed = type_info["encoding"]["integer"]["isSigned"]
        elif "plaintext" in spec["typeInfo"].keys():
            type_info = spec["typeInfo"]["plaintext"]
            is_encrypted = False
            width = type_info["integerPrecision"]
            is_signed = type_info["isSigned"]
            shape = tuple(type_info["shape"]["dimensions"])
        else:
            message = f"Expected a valid type in {spec['typeInfo'].keys()}"
            raise ValueError(message)

        expected_dtype = SignedInteger(width) if is_signed else UnsignedInteger(width)
        expected_value = ValueDescription(expected_dtype, shape, is_encrypted)
        if is_valid:
            expected_min = expected_dtype.min()
            expected_max = expected_dtype.max()

            if not is_encrypted:
                # clear integers are signless
                # (e.g., 8-bit clear integer can be in range -128, 255)
                expected_min = -(expected_max // 2) - 1

            actual_min = arg if isinstance(arg, int) else arg.min()
            actual_max = arg if isinstance(arg, int) else arg.max()
            actual_shape = () if isinstance(arg, int) else arg.shape

            is_valid = (
                actual_min >= expected_min
                and actual_max <= expected_max
                and actual_shape == expected_value.shape
            )

            if is_valid:
                sanitized_args[index] = arg

        if not is_valid:
            try:
                actual_value = str(ValueDescription.of(arg, is_encrypted=is_encrypted))
            except ValueError:
                actual_value = type(arg).__name__
            message = f"Expected argument {index} to be {expected_value} but it's {actual_value}"
            raise ValueError(message)

    ordered_sanitized_args = [sanitized_args[i] for i in range(len(sanitized_args))]
    return ordered_sanitized_args


def fuse(graph: Graph, artifacts: Optional["FunctionDebugArtifacts"] = None):
    """
    Fuse appropriate subgraphs in a graph to a single Operation.Generic node.

    Args:
        graph (Graph):
            graph to search and update

        artifacts (Optional[DebugArtifacts], default = None):
            compilation artifacts to store information about the fusing process

    Raises:
        RuntimeError:
            if there is a subgraph which needs to be fused cannot be fused
    """

    nx_graph = graph.graph
    processed_terminal_nodes: set[Node] = set()

    fusing_floats = True
    while True:
        subgraph_to_fuse = (
            find_float_subgraph_with_unique_terminal_node(
                graph,
                processed_terminal_nodes,
            )
            if fusing_floats
            else find_tlu_subgraph_with_multiple_variable_inputs_that_has_a_single_common_ancestor(
                graph,
                processed_terminal_nodes,
            )
        )

        if subgraph_to_fuse is None:
            if fusing_floats:
                fusing_floats = False
                processed_terminal_nodes.clear()
                continue
            break

        all_nodes, start_nodes, terminal_node = subgraph_to_fuse
        processed_terminal_nodes.add(terminal_node)

        conversion_result = convert_subgraph_to_subgraph_node(
            graph,
            all_nodes,
            start_nodes,
            terminal_node,
        )
        if conversion_result is None:
            continue

        fused_node, node_before_subgraph = conversion_result
        nx_graph.add_node(fused_node)

        if terminal_node in graph.output_nodes.values():
            output_node_to_idx: dict[Node, list[int]] = {
                out_node: [] for out_node in graph.output_nodes.values()
            }
            for output_idx, output_node in graph.output_nodes.items():
                output_node_to_idx[output_node].append(output_idx)

            for output_idx in output_node_to_idx.get(terminal_node, []):
                graph.output_nodes[output_idx] = fused_node

        terminal_node_succ = list(nx_graph.successors(terminal_node))
        for succ in terminal_node_succ:
            succ_edge_data = deepcopy(nx_graph.get_edge_data(terminal_node, succ))
            for edge_key, edge_data in succ_edge_data.items():
                nx_graph.remove_edge(terminal_node, succ, key=edge_key)
                new_edge_data = deepcopy(edge_data)
                nx_graph.add_edge(fused_node, succ, key=edge_key, **new_edge_data)

        nx_graph.add_edge(node_before_subgraph, fused_node, input_idx=0)

        graph.prune_useless_nodes()
        if artifacts is not None:
            artifacts.add_graph("after-fusing", graph)


def find_float_subgraph_with_unique_terminal_node(
    graph: Graph,
    processed_terminal_nodes: set[Node],
) -> Optional[tuple[dict[Node, None], dict[Node, None], Node]]:
    """
    Find a subgraph with float computations that end with an integer output.

    Args:
        graph (Graph):
            graph to search

        processed_terminal_nodes (Set[Node]):
            set of terminal nodes which have already been searched for float subgraphs

    Returns:
        Optional[Tuple[Dict[Node, None], Dict[Node, None], Node]]:
            None if there are no such subgraphs,
            tuple containing all nodes in the subgraph, start nodes of the subgraph,
            and terminal node of the subgraph otherwise
    """

    nx_graph = graph.graph
    terminal_nodes = (
        node
        for node in nx_graph.nodes()
        if (
            node not in processed_terminal_nodes
            and any(isinstance(input.dtype, Float) for input in node.inputs)
            and isinstance(node.output.dtype, Integer)
        )
    )
    try:
        terminal_node = next(terminal_nodes)
    except StopIteration:
        return None

    all_nodes: dict[Node, None] = {}

    start_single_int_output_nodes_search_from = terminal_node
    while True:
        all_nodes, start_nodes = find_closest_integer_output_nodes(
            graph,
            [start_single_int_output_nodes_search_from],
            all_nodes,
        )

        variable_start_nodes = [
            start_node for start_node in start_nodes if start_node.operation != Operation.Constant
        ]
        if len(variable_start_nodes) == 1:
            break

        # find a common ancestor as we need a single variable input node
        # lca == lowest common ancestor
        lca = find_single_lca(graph, variable_start_nodes)

        # if subgraph cannot be fused because there is no way to find a common ancestor, break
        if lca is None:
            break

        # add the nodes from the `start_nodes` to `lca`, to `all_nodes`
        all_nodes = add_nodes_from_to(graph, start_nodes, {lca: None}, all_nodes)

        # if `lca` is a valid starting node for fusing break
        if isinstance(lca.output.dtype, Integer):
            # `lca` is the new start node
            start_nodes = {lca: None}
            break

        # otherwise, push a little further
        # (e.g., if there is a node just before, which has an integer output)
        start_single_int_output_nodes_search_from = lca

    return all_nodes, start_nodes, terminal_node


def find_tlu_subgraph_with_multiple_variable_inputs_that_has_a_single_common_ancestor(
    graph: Graph,
    processed_terminal_nodes: set[Node],
) -> Optional[tuple[dict[Node, None], dict[Node, None], Node]]:
    """
    Find a subgraph with a tlu computation that has multiple variable inputs \
    where all variable inputs share a common ancestor.

    Args:
        graph (Graph):
            graph to search

        processed_terminal_nodes (Set[Node]):
            set of terminal nodes which have already been searched for tlu subgraphs

    Returns:
        Optional[Tuple[Dict[Node, None], Dict[Node, None], Node]]:
            None if there are no such subgraphs,
            tuple containing all nodes in the subgraph, start nodes of the subgraph,
            and terminal node of the subgraph otherwise
    """

    nx_graph = graph.graph
    terminal_nodes = (
        node
        for node in nx_graph.nodes()
        if (
            node not in processed_terminal_nodes
            and node.converted_to_table_lookup
            and all(isinstance(input.dtype, Integer) for input in node.inputs)
            and isinstance(node.output.dtype, Integer)
            and len(
                [
                    pred
                    for pred in nx_graph.predecessors(node)
                    if pred.operation != Operation.Constant
                ]
            )
            > 1
        )
    )
    try:
        terminal_node = next(terminal_nodes)
    except StopIteration:
        return None

    all_nodes: dict[Node, None] = {}

    while True:
        variable_start_nodes = list(nx_graph.predecessors(terminal_node))

        # find a common ancestor as we need a single variable input node
        # lca == lowest common ancestor
        lca = find_single_lca(graph, variable_start_nodes)

        # if subgraph cannot be fused because there is no way to find a common ancestor, break
        if lca is None:
            start_nodes = {node: None for node in variable_start_nodes}
            all_nodes = {node: None for node in variable_start_nodes + [terminal_node]}
            break

        # add the nodes from the `start_nodes` to `lca`, to `all_nodes`
        all_nodes = add_nodes_from_to(
            graph,
            list(nx_graph.predecessors(terminal_node)),
            {lca: None},
            all_nodes,
        )
        all_nodes[terminal_node] = None

        # if `lca` is a valid starting node for fusing break
        if isinstance(lca.output.dtype, Integer):
            # `lca` is the new start node
            start_nodes = {lca: None}
            break

    return all_nodes, start_nodes, terminal_node


def find_single_lca(graph: Graph, nodes: list[Node]) -> Optional[Node]:
    """
    Find the single lowest common ancestor of a list of nodes.

    Args:
        graph (Graph):
            graph to search for single lca

        nodes (List[Node]):
            nodes to find the single lca of

    Returns
        Optional[Node]:
            single lca if it exists, None otherwise
    """

    nx_graph = graph.graph

    # find all ancestors of `nodes`
    # nodes themselves need to be in this set because the single lca can be within `nodes`
    all_ancestors = [set(list(nx.ancestors(nx_graph, node)) + [node]) for node in nodes]

    # find common ancestors among `nodes`
    # if the single lca exists, it's in this set
    common_ancestors = {
        node
        for node in nx_graph.nodes()
        if node.operation != Operation.Constant
        and all(node in ancestors for ancestors in all_ancestors)
    }

    # iterate over every node in the graph reversed topological order
    # this is to ensure result, if found, is the single "lowest" common ancestor
    for candidate in reversed(list(nx.topological_sort(nx_graph))):
        # check if node is a common ancestor of all `nodes`
        if candidate not in common_ancestors:
            # if not, it cannot be the single lca
            continue

        # check if node is a single common ancestor of `nodes`
        if is_single_common_ancestor(graph, candidate, nodes):
            # if so, it's the single lca of `nodes`
            # so return it
            return candidate

    # if none of the nodes in `common_ancestors` is the single lca
    # there is no single lca of this set of nodes, so return None
    return None


def is_single_common_ancestor(
    graph: Graph,
    candidate: Node,
    nodes: list[Node],
) -> bool:
    """
    Determine if a node is the single common ancestor of a list of nodes.

    Note that this function doesn't care about `lowest` property of `lca`.

    Args:
        graph (Graph):
            graph to perform the check

        candidate (Node):
            node to determine single common ancestor status

        nodes (List[Node]):
            nodes to determine single common ancestor status against

    Returns
        bool:
            True if `candidate` is a single common ancestor of `nodes`, False otherwise
    """

    nx_graph = graph.graph

    # create a subgraph with `candidate` node
    subgraph = nx.DiGraph()
    subgraph.add_node(candidate)

    # iterate over `nodes` to add them to the subgraph
    # along with every path from `candidate` to them
    for node in nodes:
        subgraph.add_node(node)
        for path in nx.all_simple_paths(nx_graph, source=candidate, target=node):
            nx.add_path(subgraph, path)

    # iterate over the nodes of the subgraph
    for node in subgraph.nodes():
        # the condition below doesn't apply to `candidate`
        # as its predecessors are not in the subgraph
        if node == candidate:
            continue

        # find number of predecessors in the subgraph and in the original graph
        # except constant nodes in the original graph as
        #   - they are not in the subgraph
        #   - they don't affect fusability status
        predecessor_count_in_subgraph = len(list(subgraph.predecessors(node)))
        predecessor_count_in_nx_graph = len(
            [pred for pred in nx_graph.predecessors(node) if pred.operation != Operation.Constant]
        )

        # see if number of predecessors are different
        if predecessor_count_in_subgraph != predecessor_count_in_nx_graph:
            # if so, `candidate` cannot be a single common ancestor
            # reasoning for is explained below
            return False

    # if every node in the subgraph has the same number of predecessors
    # as in the original graph `candidate` is in fact a single common ancestor
    return True

    # Here is why this function works.
    #
    # Legend:
    #   - /|\- = Edge
    #   - (...) = Intermediate Node
    #   - {...} = Candidate Node
    #   - [...] = Node of which single common ancestor is searched
    #   - {[...]} = Both Candidate Node and Node of which single common ancestor is searched
    #
    # Consider the following graph:
    #
    # (3)       (x)     (2)
    #    \     /   \   /
    #     [{*}]    (/)
    #          \   /
    #           [+]
    #
    # - Operation: (x * 3) + (x / 2)
    # - Candidate: {*}
    # - Nodes: [*] and [+]
    #
    # So we want to know if multiplication node is a single common ancestor of
    # multiplication and addition nodes. The result is no in this case for our purposes.
    #
    # Once you apply the subgraph creation above, you'll get the following graph:
    #
    # (*)
    #  |
    # (+)
    #
    # In this subgraph, addition node only have a single predecessor,
    # which means there is path leading to the addition node and that path doesn't include
    # the multiplication node, so we conclude multiplication node is not a single common ancestor
    #
    # Now, consider the following graph:
    #
    # (3)     {x}     (2)
    #    \   /   \   /
    #     [*]     (/)
    #        \   /
    #         [+]
    #
    # - Operation: (x * 3) + (x / 2)
    # - Candidate: {x}
    # - Nodes: [*] and [+]
    #
    # So we want to know if the input node 'x' is the single common ancestor of
    # multiplication and addition nodes. The result is yes in this case.
    #
    # Once you apply the subgraph creation above, you'll get the following graph:
    #
    #     {x}
    #    /   \
    # [*]     (/)
    #    \   /
    #     [+]
    #
    # In this subgraph, every node except the candidate node
    # will keep all of their non-constant predecessors,
    # which means all of their non-constant predecessors originated
    # from the `candidate`, so it's a single common ancestor.
    #
    # When you think about it, this implementation makes a lot of sense for our purposes
    # It basically determines if `nodes` "solely" depend on the `candidate`,
    # which is the condition for fusing.


def find_closest_integer_output_nodes(
    graph: Graph,
    start_nodes: list[Node],
    all_nodes: dict[Node, None],
) -> tuple[dict[Node, None], dict[Node, None]]:
    """
    Find the closest upstream integer output nodes to a set of start nodes in a graph.

    Args:
        graph (Graph):
            graph to search

        start_nodes (List[Node]):
            nodes from which to start the search

        all_nodes (Dict[Node, None]):
            set of nodes to be extended with visited nodes during the search

    Returns:
        Tuple[Dict[Node, None], Dict[Node, None]]:
            tuple containing extended `all_nodes` and integer output nodes closest to `start_nodes`
    """

    nx_graph = graph.graph

    closest_integer_output_nodes: dict[Node, None] = {}
    visited_nodes: set[Node] = set()

    current_nodes = {start_node: None for start_node in start_nodes}
    while current_nodes:
        next_nodes: dict[Node, None] = {}
        for node in current_nodes:
            if node not in visited_nodes:
                visited_nodes.add(node)

                all_nodes.update({node: None})
                for pred in nx_graph.predecessors(node):
                    if isinstance(pred.output.dtype, Integer):
                        closest_integer_output_nodes.update({pred: None})
                        all_nodes.update({pred: None})
                    else:
                        next_nodes.update({pred: None})
        current_nodes = next_nodes

    return all_nodes, closest_integer_output_nodes


def add_nodes_from_to(
    graph: Graph,
    from_nodes: Iterable[Node],
    to_nodes: dict[Node, None],
    all_nodes: dict[Node, None],
) -> dict[Node, None]:
    """
    Add nodes from `from_nodes` to `to_nodes`, to `all_nodes`.

    Args:
        graph (Graph):
            graph to traverse

        from_nodes (Iterable[Node]):
            nodes from which extending `all_nodes` start

        to_nodes (Dict[Node, None]):
            nodes to which extending `all_nodes` stop

        all_nodes (Dict[Node, None]):
            nodes to be extended

    Returns:
        Dict[Node, None]:
            extended `all_nodes`
    """

    nx_graph = graph.graph

    all_nodes.update(to_nodes)
    visited_nodes: set[Node] = set()

    current_nodes = {from_node: None for from_node in from_nodes}
    while current_nodes:
        next_nodes: dict[Node, None] = {}
        for node in current_nodes:
            if node not in visited_nodes:
                visited_nodes.add(node)

                all_nodes.update({node: None})
                if node not in to_nodes:
                    predecessors = nx_graph.predecessors(node)
                    next_nodes.update({pred: None for pred in predecessors if pred not in to_nodes})
        current_nodes = next_nodes

    return all_nodes


def convert_subgraph_to_subgraph_node(
    graph: Graph,
    all_nodes: dict[Node, None],
    start_nodes: dict[Node, None],
    terminal_node: Node,
) -> Optional[tuple[Node, Node]]:
    """
    Convert a subgraph to Operation.Generic node.

    Args:
        graph (Graph):
            original graph

        all_nodes (Dict[Node, None]):
            all nodes in the subgraph

        start_nodes (Dict[Node, None]):
            start nodes of the subgraph

        terminal_node (Node):
            terminal node of the subgraph

    Raises:
        RuntimeError:
            if subgraph is not fusable

    Returns:
        Optional[Tuple[Node, Node]]:
            None if the subgraph cannot be fused,
            subgraph node and its predecessor otherwise
    """

    nx_graph = graph.graph

    if terminal_node.operation == Operation.Generic and terminal_node.properties["attributes"].get(
        "is_multivariate"
    ):
        return None

    variable_input_nodes = [node for node in start_nodes if node.operation != Operation.Constant]
    if len(variable_input_nodes) != 1:
        base_highlighted_nodes = {
            node: ["within this subgraph", node.location] for node in all_nodes
        }
        for variable_input_node in variable_input_nodes:
            base_highlighted_nodes[variable_input_node] = [
                "this is one of the input nodes",
                variable_input_node.location,
            ]

        if terminal_node.properties["name"] == "where":
            return None

        raise RuntimeError(
            "A subgraph within the function you are trying to compile cannot be fused "
            "because it has multiple input nodes\n\n"
            + graph.format(highlighted_nodes=base_highlighted_nodes, show_bounds=False)
        )

    variable_input_node = variable_input_nodes[0]
    check_subgraph_fusibility(graph, all_nodes, variable_input_node)

    nx_subgraph = nx.MultiDiGraph(nx_graph)
    nodes_to_remove = [node for node in nx_subgraph.nodes() if node not in all_nodes]
    nx_subgraph.remove_nodes_from(nodes_to_remove)

    subgraph_variable_input_node = Node.input("input", deepcopy(variable_input_node.output))
    nx_subgraph.add_node(subgraph_variable_input_node)

    subgraph_variable_input_node.location = variable_input_node.location
    subgraph_variable_input_node.tag = variable_input_node.tag
    subgraph_variable_input_node.created_at = variable_input_node.created_at

    variable_input_node_successors = {
        node: None for node in all_nodes if node in nx_graph.succ[variable_input_node]
    }
    for successor in variable_input_node_successors:
        edges = deepcopy(nx_subgraph.get_edge_data(variable_input_node, successor))
        for edge_key, edge_data in edges.items():
            nx_subgraph.remove_edge(variable_input_node, successor, key=edge_key)
            new_edge_data = deepcopy(edge_data)
            nx_subgraph.add_edge(
                subgraph_variable_input_node,
                successor,
                key=edge_key,
                **new_edge_data,
            )

    original_location = terminal_node.location
    original_tag = terminal_node.tag
    original_created_at = terminal_node.created_at

    subgraph = Graph(nx_subgraph, {0: subgraph_variable_input_node}, {0: terminal_node}, graph.name)
    subgraph_node = Node.generic(
        "subgraph",
        deepcopy(subgraph_variable_input_node.inputs),
        terminal_node.output,
        lambda x, subgraph, terminal_node: subgraph.evaluate(x)[terminal_node],
        kwargs={
            "subgraph": subgraph,
            "terminal_node": terminal_node,
        },
    )

    subgraph_node.location = original_location
    subgraph_node.tag = original_tag
    subgraph_node.created_at = original_created_at

    return subgraph_node, variable_input_node


def check_subgraph_fusibility(
    graph: Graph,
    all_nodes: dict[Node, None],
    variable_input_node: Node,
):
    """
    Determine if a subgraph can be fused.

    e.g.,

    shuffling or reshaping a tensor make fusing impossible as there should be a one-to-one mapping
    between each cell of the input and each cell of the output for table lookups

    Args:
        graph (Graph):
            original graph

        all_nodes (Dict[Node, None]):
            all nodes in the subgraph

        variable_input_node (Node):
            variable input node to the subgraph

    Raises:
        RuntimeError:
            if subgraph is not fusable
    """

    base_highlighted_nodes = {node: ["within this subgraph", node.location] for node in all_nodes}
    base_highlighted_nodes[variable_input_node] = [
        "with this input node",
        variable_input_node.location,
    ]

    non_constant_nodes = (node for node in all_nodes if node.operation != Operation.Constant)
    for node in non_constant_nodes:
        if node == variable_input_node:
            continue

        if not node.is_fusable:
            base_highlighted_nodes[node] = ["this node is not fusable", node.location]
            raise RuntimeError(
                "A subgraph within the function you are trying to compile cannot be fused "
                "because of a node, which is marked explicitly as non-fusable\n\n"
                + graph.format(highlighted_nodes=base_highlighted_nodes, show_bounds=False)
            )

        if node.output.shape != variable_input_node.output.shape:
            base_highlighted_nodes[node] = [
                "this node has a different shape than the input node",
                node.location,
            ]
            raise RuntimeError(
                "A subgraph within the function you are trying to compile cannot be fused "
                "because of a node, which is has a different shape than the input node\n\n"
                + graph.format(highlighted_nodes=base_highlighted_nodes, show_bounds=False)
            )

    return True


def friendly_type_format(type_: type) -> str:
    """Convert a type to a string. Remove package name and class/type keywords."""
    result = str(type_)
    result = re.sub(r"<\w+ '(\w+)'>", r"\1", result)
    result = re.sub(r"(\w+\.)+", "", result)
    if result.startswith("Union"):
        # py3.8: Optional are Union
        try:
            arg0, arg1 = type_.__args__  # type: ignore
        except (AttributeError, ValueError):
            pass
        else:
            if arg1 == None.__class__:  # pragma: no cover
                return f"Optional[{friendly_type_format(arg0)}]"

    return result


def get_terminal_size() -> int:
    """
    Get the terminal size.
    """

    try:  # pragma: no cover
        # this branch cannot be covered
        # because `os.get_terminal_size()`
        # raises an exception during tests
        columns, _ = os.get_terminal_size()
        if columns == 0:  # noqa: SIM108
            columns = 80
    except OSError:  # pragma: no cover
        columns = 80

    return columns
