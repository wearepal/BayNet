"""Graph object."""
from __future__ import annotations
from copy import deepcopy
import itertools
from itertools import combinations
from pathlib import Path
from string import ascii_uppercase
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type, Union

import igraph
import numpy as np
import pandas as pd
from pandas.api.types import is_categorical_dtype, is_integer_dtype, is_string_dtype
from typing_extensions import Literal

from baynet.utils import dag_io, visualisation

from .interventions import odds_ratio_aggregator
from .parameters import ConditionalProbabilityDistribution, ConditionalProbabilityTable


def _nodes_sorted(nodes: Union[List[int], List[str], List[object]]) -> List[str]:
    return sorted([str(node) for node in nodes])


def _nodes_with_parents(modelstring: str) -> List[str]:
    return modelstring.strip().strip("[]]").split("][")


def _nodes_from_modelstring(modelstring: str) -> List[str]:
    nodes = [node.split("|")[0] for node in _nodes_with_parents(modelstring)]
    return _nodes_sorted(nodes)


def _edges_from_modelstring(modelstring: str) -> List[Tuple[str, str]]:
    edges: List[Tuple[str, str]] = []
    for node_and_parents in _nodes_with_parents(modelstring):
        try:
            node, parents = node_and_parents.split("|")
        except ValueError:
            continue
        for parent in parents.split(":"):
            edges.append((parent, node))
    return edges


def _name_node(index: int) -> str:
    chars: List[str] = []
    if index == 0:
        return "A"
    while index > 0:
        index, mod = divmod(index, 26)
        chars.insert(0, ascii_uppercase[mod])
    return ''.join(chars)


def _graph_method_wrapper(dag: DAG, func: Callable) -> Callable:
    """Call an igraph.Graph function, (where applicable) return DAG instead of igraph.Graph."""

    def wrapped_method(*args: Tuple[Any], **kwargs: Dict[Any, Any]) -> Union[Callable, DAG]:
        res = func(*args, **kwargs)
        if isinstance(res, igraph.Graph):
            dag_copy = dag.copy()
            dag_copy.graph = res
            return dag_copy
        return res

    return wrapped_method


class DAG:
    """Directed Acyclic Graph object, built around igraph.Graph, adapted for bayesian networks."""

    # pylint: disable=unsubscriptable-object, not-an-iterable, arguments-differ
    def __init__(self, graph_or_buf: Optional[Union[bytes, igraph.Graph]] = None) -> None:
        """Create a DAG object."""
        self.graph = igraph.Graph(directed=True, vertex_attrs={"CPD": None})
        if isinstance(graph_or_buf, igraph.Graph):
            self.graph = graph_or_buf
            assert self.is_dag()
            self.name_nodes()
        elif isinstance(graph_or_buf, bytes):
            dag_io.buf_to_dag(graph_or_buf, dag=self)

    def __getattribute__(self, name: str) -> Any:
        """Overwrite object.__getattribute__ to fall back on igraph.Graph where necessary."""
        try:
            return super().__getattribute__(name)
        except AttributeError as errormsg:
            try:
                attr = self.graph.__getattribute__(name)
                if callable(attr) and not isinstance(attr, (igraph.VertexSeq, igraph.EdgeSeq)):
                    # Anywhere a function *might* return a Graph, return a DAG instead
                    return _graph_method_wrapper(self, attr)
                return attr
            except AttributeError:
                raise errormsg

    def __reduce__(self) -> Tuple:
        """Return representation for Pickle."""
        return self.__class__, (self.save(),)

    @classmethod
    def from_modelstring(cls, modelstring: str) -> "DAG":
        """Instantiate a Graph object from a modelstring."""
        dag = cls()
        dag.add_vertices(_nodes_from_modelstring(modelstring))
        dag.add_edges(_edges_from_modelstring(modelstring))
        return dag

    @classmethod
    def from_edges(cls, edges: Set[Tuple[str, str]]) -> "DAG":
        """Instantiate a Graph object from a set of edges."""
        dag = cls()
        dag.add_vertices(_nodes_sorted({node for edge in edges for node in edge}))
        dag.add_edges(edges)
        return dag

    @classmethod
    def from_amat(
        cls, amat: Union[np.ndarray, List[List[int]]], colnames: Optional[List[str]] = None
    ) -> "DAG":
        """Instantiate a Graph object from an adjacency matrix."""
        if isinstance(amat, np.ndarray):
            amat = amat.tolist()
        if colnames is None:
            colnames = [_name_node(i) for i in range(len(amat))]
        dag = cls()
        dag.add_vertices(colnames)
        dag.add_edges(
            [
                (str(colnames[parent_idx]), str(colnames[target_idx]))
                for parent_idx, row in enumerate(amat)
                for target_idx, val in enumerate(row)
                if val
            ]
        )
        return dag

    @classmethod
    def from_other(cls, other_graph: Any) -> "DAG":
        """Attempt to create a Graph from an existing graph object (nx.DiGraph etc.)."""
        dag = cls()
        dag.add_vertices(_nodes_sorted(other_graph.nodes))
        dag.add_edges(other_graph.edges)
        return dag

    @staticmethod
    def generate(structure_type: str, n_nodes: int, **kwargs: Dict[Any, Any]) -> "DAG":
        """Call a structure generation function by name."""
        from . import structure_generation  # pylint: disable=import-outside-toplevel

        structure_func = getattr(structure_generation, structure_type.lower().replace(" ", "_"))
        return structure_func(n_nodes, **kwargs)

    @staticmethod
    def from_bif(bif: Union[Path, str]) -> "DAG":
        """Create a Graph from a BIF file, from Path or name of standard network from libarary."""
        return dag_io.dag_from_bif(bif)

    @property
    def dtype(self) -> Optional[str]:
        """Return data type of parameterised network."""
        if all(isinstance(vertex["CPD"], ConditionalProbabilityTable) for vertex in self.vs):
            return "discrete"
        elif all(
            isinstance(vertex["CPD"], ConditionalProbabilityDistribution) for vertex in self.vs
        ):
            return "continuous"
        elif all(
            isinstance(
                vertex["CPD"], (ConditionalProbabilityTable, ConditionalProbabilityDistribution)
            )
            for vertex in self.vs
        ):
            return "mixed"
        return None

    @property
    def nodes(self) -> Set[str]:
        """Return a set of the names of all nodes in the network."""
        return {self.get_node_name(v.index) for v in self.vs}

    @property
    def edges(self) -> Set[Tuple[str, str]]:
        """Return all edges in the Graph."""
        if self.is_directed():
            return self.directed_edges
        return self.skeleton_edges

    @property
    def skeleton_edges(self) -> Set[Tuple[str, str]]:
        """Return all edges in the skeleton of the Graph."""
        return self.reversed_edges | self.directed_edges

    @property
    def directed_edges(self) -> Set[Tuple[str, str]]:
        """Return forward edges in the Graph."""
        return {(self.get_node_name(e.source), self.get_node_name(e.target)) for e in self.es}

    @property
    def reversed_edges(self) -> Set[Tuple[str, str]]:
        """Return reversed edges in the Graph."""
        return {(self.get_node_name(e.target), self.get_node_name(e.source)) for e in self.es}

    def get_node_name(self, node: int) -> str:
        """Convert node index to node name."""
        return self.vs[node]["name"]

    def get_node_index(self, node: str) -> int:
        """Convert node name to node index."""
        return self.vs["name"].index(node)

    def get_node(self, name_or_index: Union[str, int]) -> igraph.Vertex:
        """Get Vertex object by node name."""
        if isinstance(name_or_index, str):
            name_or_index = self.get_node_index(name_or_index)
        return self.vs[name_or_index]

    def add_edge(self, source: str, target: str) -> None:
        """Add a single edge, using node names (as strings).

        Overrides: igraph.Graph.add_edge
        """
        if (source, target) in self.edges:
            raise ValueError(f"Edge {source}->{target} already exists in Graph")
        self.graph.add_edge(source, target)
        assert self.is_dag()

    def add_edges(self, edges: Union[Set[Tuple[str, str]], List[Tuple[str, str]]]) -> None:
        """Add multiple edges from a list of tuples, each containing (from, to) as strings."""
        for source, target in edges:
            if (source, target) in self.edges:
                raise ValueError(f"Edge {source}->{target} already exists in Graph")
        if len(edges) != len(set(edges)):
            raise ValueError("Edges list contains duplicates")
        self.graph.add_edges(edges)
        assert self.is_dag()

    def get_numpy_adjacency(self, skeleton: bool = False) -> np.ndarray:
        """Obtain adjacency matrix as a numpy (boolean) array."""
        if skeleton:
            amat = self.get_numpy_adjacency()
            return amat | amat.T
        return np.array(list(self.get_adjacency()), dtype=bool)

    def get_modelstring(self) -> str:
        """Obtain modelstring representation of stored graph."""
        modelstring = ""
        for node in _nodes_sorted(list(self.nodes)):
            parents = _nodes_sorted(
                [v["name"] for v in self.get_ancestors(node, only_parents=True)]
            )
            modelstring += f"[{node}"
            modelstring += f"|{':'.join(parents)}" if parents else ""
            modelstring += "]"
        return modelstring

    def get_ancestors(
        self, node: Union[str, int, igraph.Vertex], only_parents: bool = False
    ) -> igraph.VertexSeq:
        """Return an igraph.VertexSeq of ancestors for given node (string or node index)."""
        if isinstance(node, str):
            # Convert name to index
            node = self.get_node_index(node)
        elif isinstance(node, igraph.Vertex):
            node = node.index
        order = 1 if only_parents else len(self.vs)
        ancestors = list(self.neighborhood(vertices=node, order=order, mode="IN"))
        ancestors.remove(node)
        if len(ancestors) <= 1:
            return igraph.VertexSeq(self.graph, ancestors)
        return self.vs[sorted(ancestors)]

    def get_descendants(
        self, node: Union[str, int, igraph.Vertex], only_children: bool = False
    ) -> igraph.VertexSeq:
        """Return an igraph.VertexSeq of descendants for given node (string or node index)."""
        if isinstance(node, str):
            # Convert name to index
            node = self.get_node_index(node)
        elif isinstance(node, igraph.Vertex):
            node = node.index
        order = 1 if only_children else len(self.vs)
        ancestors = list(self.neighborhood(vertices=node, order=order, mode="OUT"))
        ancestors.remove(node)
        if len(ancestors) <= 1:
            return igraph.VertexSeq(self.graph, ancestors)
        return self.vs[sorted(ancestors)]

    def are_neighbours(
        self, node_a: Union[igraph.Vertex, str, int], node_b: Union[igraph.Vertex, str, int]
    ) -> bool:
        """Check if two nodes are neighbours in the Graph."""
        if not isinstance(node_a, igraph.Vertex):
            node_a = self.get_node(node_a)
        if not isinstance(node_b, igraph.Vertex):
            node_b = self.get_node(node_b)
        return node_a.index in self.neighborhood(vertices=node_b)

    def get_v_structures(self, include_shielded: bool = False) -> Set[Tuple[str, str, str]]:
        """Return a list of the Graph's v-structures in tuple form; (a,b,c) = a->b<-c."""
        v_structures: List[Tuple[str, str, str]] = []
        for node in self.nodes:
            all_parents = self.get_ancestors(node, only_parents=True)
            all_pairs = combinations(all_parents, 2)
            all_pairs = [sorted(pair, key=lambda x: x['name']) for pair in all_pairs]
            if include_shielded:
                node_v_structures = [(a["name"], node, b["name"]) for a, b in all_pairs]
            else:
                node_v_structures = [
                    (a["name"], node, b["name"])
                    for a, b in all_pairs
                    if not self.are_neighbours(a, b)
                ]
            v_structures += node_v_structures
        return set(v_structures)

    def generate_continuous_parameters(
        self,
        possible_weights: Optional[List[float]] = None,
        mean: Optional[float] = None,
        std: Optional[float] = None,
        seed: Optional[int] = None,
    ) -> DAG:
        """Populate continuous conditional distributions for each node."""
        for vertex in self.vs:
            vertex["CPD"] = ConditionalProbabilityDistribution(vertex, mean=mean, std=std)
            vertex["CPD"].sample_parameters(weights=possible_weights, seed=seed)
        return self

    def generate_levels(
        self,
        min_levels: Optional[int] = None,
        max_levels: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> DAG:
        """Set number of levels in each node, for generating discrete data."""
        if seed is not None:
            np.random.seed(seed)
        if min_levels is None:
            min_levels = 2
        if max_levels is None:
            max_levels = 3
        assert max_levels >= min_levels >= 2
        for vertex in self.vs:
            n_levels = np.random.randint(min_levels, max_levels + 1)
            vertex["levels"] = list(map(str, range(n_levels)))
        return self

    def generate_discrete_parameters(
        self,
        alpha: Optional[float] = None,
        min_levels: Optional[int] = None,
        max_levels: Optional[int] = None,
        seed: Optional[int] = None,
        normalise_alpha: bool = True,
    ) -> DAG:
        """Populate discrete conditional parameter tables for each node.

        Samples parameters from Dirichlet(alpha) (default alpha=20).
        Samples levels uniformly between min_levels and max_levels (default 2,3 respectively).
        """
        try:
            self.vs["levels"]
        except KeyError:
            self.generate_levels(min_levels, max_levels, seed)
        if seed is not None:
            np.random.seed(seed)
        for vertex in self.vs:
            vertex["CPD"] = ConditionalProbabilityTable(vertex)
            vertex["CPD"].sample_parameters(alpha=alpha, normalise_alpha=normalise_alpha)
        return self

    def estimate_parameters(
        self,
        data: pd.DataFrame,
        method: str = "mle",
        infer_levels: bool = False,
        method_args: Optional[Dict[str, Union[int, float]]] = None,
    ) -> None:
        """Estimate conditional probabilities based on supplied data."""
        data = data.copy()
        if infer_levels:
            if all(is_categorical_dtype(data[col]) for col in data.columns):
                self.vs['levels'] = [list(dtype.categories) for dtype in data.dtypes]
            else:
                for vertex in self.vs:
                    if not (
                        is_integer_dtype(data[vertex['name']])
                        or is_string_dtype(data[vertex['name']])
                    ):
                        raise ValueError(
                            f"Unrecognised DataFrame dtype: {data[vertex['name']].dtype}"
                        )
                    vertex_categories = sorted(data[vertex['name']].unique().astype(str))
                    column = pd.Categorical(
                        data[vertex['name']].astype(str), categories=vertex_categories
                    )
                    vertex['levels'] = vertex_categories
                    data[vertex['name']] = column
        else:
            try:
                if not all(isinstance(dtype, pd.CategoricalDtype) for dtype in data.dtypes):
                    for vertex in self.vs:
                        if is_integer_dtype(data[vertex['name']]):
                            cat_dtype = pd.CategoricalDtype(vertex['levels'], ordered=True)
                            data[vertex['name']] = pd.Categorical.from_codes(
                                codes=data[vertex['name']], dtype=cat_dtype
                            )
                        elif is_string_dtype(data[vertex['name']]):
                            data[vertex['name']] = pd.Categorical(
                                data[vertex['name']], categories=vertex['levels']
                            )
            except KeyError:
                raise ValueError(
                    "`estimate_parameters()` requires levels be defined or `infer_levels=True`"
                )

        for vertex in self.vs:
            vertex['CPD'] = ConditionalProbabilityTable.estimate(
                vertex, data=data, method=method, method_args=method_args
            )

    def sample(self, n_samples: int, seed: Optional[int] = None) -> pd.DataFrame:
        """Sample n_samples rows of data from the graph."""
        if seed is not None:
            np.random.seed(seed)
        sorted_nodes = self.topological_sorting(mode="out")
        dtype: Type
        if all(isinstance(vertex['CPD'], ConditionalProbabilityTable) for vertex in self.vs):
            dtype = int
        elif all(
            isinstance(vertex['CPD'], ConditionalProbabilityDistribution) for vertex in self.vs
        ):
            dtype = float
        else:
            raise RuntimeError("DAG requires parameters before sampling is possible.")
        data = pd.DataFrame(
            np.zeros((n_samples, len(self.nodes))).astype(dtype),
            columns=self.vs["name"],
        )
        for node_idx in sorted_nodes:
            data.iloc[:, node_idx] = self.vs[node_idx]["CPD"].sample(data)
        data = pd.DataFrame(data, columns=[vertex["name"] for vertex in self.vs])
        return data

    def save(self, buf_path: Optional[Path] = None) -> bytes:
        """Save DAG as protobuf, or string if no path is specified."""
        dag_proto = dag_io.dag_to_buf(self)
        if buf_path is not None:
            with buf_path.open("wb") as stream:
                stream.write(dag_proto)
        return dag_proto

    @classmethod
    def load(cls, buf: Union[Path, bytes]) -> "DAG":
        """Load DAG from yaml file or string."""
        if isinstance(buf, Path):
            with buf.open("rb") as stream:
                buf_str = stream.read()
        else:
            buf_str = buf
        return dag_io.buf_to_dag(buf_str)

    def remove_node(self, node: str) -> None:
        """Remove a node (inplace), marginalising it out of any children's CPTs."""
        assert node in self.nodes
        assert isinstance(self.get_node(node)["CPD"], ConditionalProbabilityTable)
        for vertex in self.get_descendants(node, only_children=True):
            assert isinstance(vertex["CPD"], ConditionalProbabilityTable)
            vertex["CPD"].marginalise(node)
        self.delete_vertices([node])

    def remove_nodes(self, nodes: Union[List[str], igraph.VertexSeq]) -> None:
        """Remove multiple nodes (inplace), marginalising it out of any children's CPTs."""
        if isinstance(nodes, igraph.VertexSeq):
            nodes = [node["name"] for node in nodes]
        for node in nodes:
            self.remove_node(node)

    def mutilate(self, node: str, evidence_level: str) -> "DAG":
        """Return a copy with node's value fixed at evidence_level, and parents killed off."""
        assert node in self.nodes
        mutilated_dag = self.copy()
        mutilated_dag.remove_nodes(mutilated_dag.get_ancestors(node, only_parents=True))
        mutilated_dag.get_node(node)["CPD"].intervene(evidence_level)
        return mutilated_dag

    def copy(self) -> "DAG":
        """Return a copy."""
        self_copy = DAG()
        self_copy.graph = self.graph.copy()
        try:
            for vertex in self_copy.graph.vs:
                vertex["CPD"] = deepcopy(vertex["CPD"])
        except KeyError:
            pass
        return self_copy

    def plot(self, path: Path = Path().resolve() / 'DAG.png') -> None:
        """Save a plot of the DAG to specified file path."""
        dag = self.copy()
        dag.vs['label'] = dag.vs['name']
        dag.vs['fontsize'] = 30
        dag.vs['fontname'] = "Helvetica"
        dag.es['color'] = "black"
        dag.es['penwidth'] = 2
        dag.es['style'] = "solid"
        visualisation.draw_graph(dag, save_path=path)

    def compare(self, other_graph: DAG) -> visualisation.GraphComparison:
        """Produce comparison to another DAG for plotting."""
        return visualisation.GraphComparison(self, other_graph, list(self.vs['name']))

    def name_nodes(self) -> None:
        """Assign names to unnamed nodes.

        For use after classmethods from igraph.Graph which don't name nodes.
        """
        for vertex in self.vs:
            if vertex.attributes().get('name', None) is None:
                vertex['name'] = _name_node(vertex.index)

    def get_equivalence_class(self, shielded: bool = True, data: pd.DataFrame = None) -> Set[DAG]:
        """Get the Markov equivalence class of the DAG object.

        If data is provided, a set of learnt BNs, rather than DAGs, will be provided.
        """
        get_pairs = lambda x: [(x[0], x[1]), (x[2], x[1])]
        v_pairs = {
            vi for v in self.get_v_structures(include_shielded=shielded) for vi in get_pairs(v)
        }
        non_v_edges = self.edges - v_pairs
        perms = list(itertools.product([0, 1], repeat=len(non_v_edges)))
        flip = lambda edge, f: edge if not f else (edge[1], edge[0])
        dags = set()
        for perm in perms:
            edges = {flip(edge, pi) for edge, pi in zip(non_v_edges, perm)}
            dag = DAG.from_edges(v_pairs | edges)
            if data is not None:
                dag.estimate_parameters(data=data, infer_levels=True)
            dags.add(dag)
        return dags

    def adjusted_odds_ratio(
        self,
        *,
        config: Optional[Union[dict, Path]] = None,
        target: Optional[str] = None,
        target_reference: Optional[Union[str, int]] = None,
        cpdag: bool = False,
        data: pd.DataFrame = None,
        aggregation: Literal['mean', 'median'] = "median",
        bounds: Optional[Literal['minmax', 'quartiles']] = "minmax",
    ) -> Union[Dict[tuple, Dict[str, float]], Dict[tuple, float]]:
        """Calculate the adjusted odds ratio for an intervention.

        :param config: A config file specifying what interventions to perform.
        :param target: The outcome variable where we want to measure effected change.
        :param target_reference: The reference level of the outcome variable
        :param cpdag: A boolean specifying whether to calculate odds ratios for
        all bayesian networks in the equivalence class.
        :param data: A pd.DataFrame object which must be given if cpdag is True
        :param aggregation: The aggregation function to use to combine the many
        values output when cpdag is True. Options: mean / median.
        :param bounds: The bound function to use when combining multiple values.
        Options are: minmax / quartiles.
        """
        return odds_ratio_aggregator(
            self,
            config=config,
            target=target,
            target_reference=target_reference,
            cpdag=cpdag,
            data=data,
            aggregation=aggregation,
            bounds=bounds,
        )
