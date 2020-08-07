# Copyright 2018 D-Wave Systems Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
#
# ================================================================================================

import collections.abc as abc
import itertools

import numpy as np
import dimod

from collections import defaultdict

from dwave.embedding.chain_breaks import majority_vote, broken_chains
from dwave.embedding.exceptions import MissingEdgeError, MissingChainError, InvalidNodeError, DisconnectedChainError
from dwave.embedding.utils import adjacency_to_edge_iter, intlabel_disjointsets


__all__ = ['embed_bqm',
           'embed_ising',
           'embed_qubo',
           'unembed_sampleset',
           ]

class EmbeddedStructure:
    """Processes an embedding and a target graph to collect target edges
    into those within individual chains, and those that connect chains.  This
    is used elsewhere to embed binary quadratic models into the target graph.

    Args:

        target_edges (iterable[edge]):
            An iterable of edges in the target graph.  Each edge should be an
            iterable of 2 hashable objects.

        embedding (dict):
            Mapping from source graph to target graph as a dict of form 
            {s: {t, ...}, ...}, where s is a source-model variable and t is a 
            target-model variable.

    """

    def __init__(self, target_edges, embedding):
        self.embedding = embedding = {u: tuple(c) for u, c in embedding.items()}

        target_label = {}
        self._chain_edges = chain_edges = {}
        self._interaction_edges = interaction_edges = defaultdict(list)

        disjoint_sets = {}
        # prepare the data structures and compute the labeling of target nodes
        for u, emb_u in embedding.items():
            chain_edges[u] = []
            if not emb_u:
                raise MissingChainError(u)
            disjoint_sets[u] = intlabel_disjointsets(len(emb_u))
            for i, q in enumerate(emb_u):
                target_label[q] = u, i

        # filter the target edges into / between chain components
        for p, q in target_edges:
            if p in target_label and q in target_label:
                u, i = target_label[p]
                v, j = target_label[q]
                if u == v:
                    chain_edges[u].append((i, j))
                    disjoint_sets[u].union(i, j)
                else:
                    interaction_edges[u, v].append(i)
                    interaction_edges[v, u].append(j)

        for u, emb_u in embedding.items():
            if len(emb_u) != disjoint_sets[u].size(0):
                raise DisconnectedChainError(u)


    def chain_edge_iter(self, u):
        """Iterate over edges contained in the chain for u

        Args:
            u (hashable):
                A key in self.embedding

        Yields:
            tuple: A 2-tuple, corresponding to an edge in the target graph
    
        """
        emb_u = self.embedding[u]
        for i, j in self._chain_edges[u]:
            yield emb_u[i], emb_u[j]


    def interaction_edge_iter(self, u, *args):
        """Iterate over edges between in the chains for u and v

        Args:
            u (hashable/tuple):
                A key in self.embedding

            v (hashable, optional):
                A key in self.embedding.  If this argument is omitted, we will
                interpret u, v := u

        Yields:
            tuple: A 2-tuple, corresponding to an edge in the target graph
    
        """
        if args:
            v, = args
        else:
            u, v = u
        emb_u = self.embedding[u]
        emb_v = self.embedding[v]
        int_u = self._interaction_edges[u, v]
        int_v = self._interaction_edges[v, u]
        for i, j in zip(int_u, int_v):
            yield emb_u[i], emb_v[j]


def embed_bqm(source_bqm, embedding=None, target_adjacency=None,
              chain_strength=1.0, smear_vartype=None):
    """Embed a binary quadratic model onto a target graph.

    Args:
        source_bqm (:obj:`.BinaryQuadraticModel`):
            Binary quadratic model to embed.

        embedding (dict/:class:`.EmbeddedStructure`):
            Mapping from source graph to target graph as a dict of form
            {s: {t, ...}, ...}, where s is a source-model variable and t is a
            target-model variable.  Alternately, an EmbeddedStructure object
            produced by, for example, 
            EmbeddedStructure(target_adjacency.edges(), embedding). If embedding
            is a dict, target_adjacency must be provided.

        target_adjacency (dict/:obj:`networkx.Graph`, optional):
            Adjacency of the target graph as a dict of form {t: Nt, ...}, where
            t is a variable in the target graph and Nt is its set of neighbours.
            This should be omitted if and only if embedding is an 
            EmbeddedStructure object.

        chain_strength (float, optional):
            Magnitude of the quadratic bias (in SPIN-space) applied between
            variables to create chains, with the energy penalty of chain breaks
            set to 2 * `chain_strength`.

        smear_vartype (:class:`.Vartype`, optional, default=None):
            Determines whether the linear bias of embedded variables is smeared
            (the specified value is evenly divided as biases of a chain in the
            target graph) in SPIN or BINARY space. Defaults to the
            :class:`.Vartype` of `source_bqm`.

    Returns:
        :obj:`.BinaryQuadraticModel`: Target binary quadratic model.

    Examples:
        This example embeds a triangular binary quadratic model representing
        a :math:`K_3` clique into a square target graph by mapping variable `c`
        in the source to nodes `2` and `3` in the target.

        >>> import networkx as nx
        ...
        >>> target = nx.cycle_graph(4)
        >>> # Binary quadratic model for a triangular source graph
        >>> h = {'a': 0, 'b': 0, 'c': 0}
        >>> J = {('a', 'b'): 1, ('b', 'c'): 1, ('a', 'c'): 1}
        >>> bqm = dimod.BinaryQuadraticModel.from_ising(h, J)
        >>> # Variable c is a chain
        >>> embedding = {'a': {0}, 'b': {1}, 'c': {2, 3}}
        >>> # Embed and show the chain strength
        >>> target_bqm = dwave.embedding.embed_bqm(bqm, embedding, target)
        >>> target_bqm.quadratic[(2, 3)]
        -1.0
        >>> print(target_bqm.quadratic)  # doctest: +SKIP
        {(0, 1): 1.0, (0, 3): 1.0, (1, 2): 1.0, (2, 3): -1.0}


    See also:
        :func:`.embed_ising`, :func:`.embed_qubo`

    """
    if isinstance(embedding, EmbeddedStructure):
        if target_adjacency is not None:
            raise ValueError("target_adjacency should not be provided if "
                             "embedding is an EmbeddedStructure")
        embedded_structure = embedding
        embedding = embedded_structure.embedding
    elif target_adjacency is None:
        raise ValueError("either embedding should be an EmbeddedStructure, or "
                         "target_adjacency must be provided")
    else:
        target_edges = adjacency_to_edge_iter(target_adjacency)
        embedded_structure = EmbeddedStructure(target_edges, embedding)

    if smear_vartype is dimod.SPIN and source_bqm.vartype is dimod.BINARY:
        return embed_bqm(source_bqm.spin, embedding=embedded_structure,
                         chain_strength=chain_strength, smear_vartype=None
                         ).binary
    elif smear_vartype is dimod.BINARY and source_bqm.vartype is dimod.SPIN:
        return embed_bqm(source_bqm.binary, embedding=embedded_structure,
                         chain_strength=chain_strength, smear_vartype=None).spin

    # create a new empty binary quadratic model with the same class as
    # source_bqm if it's shapeable, otherwise use AdjVectorBQM
    if source_bqm.shapeable():
        target_bqm = source_bqm.base.empty(source_bqm.vartype)
    else:
        target_bqm = dimod.AdjVectorBQM.empty(source_bqm.vartype)

    # add the offset
    target_bqm.add_offset(source_bqm.offset)

    # start with the linear biases, spreading the source bias equally over the
    # target variables in the chain
    for v, bias in source_bqm.linear.items():
        chain = embedding.get(v)
        if chain is None:
            raise MissingChainError(v)

        #we check that this is nonzero in EmbeddedStructure
        b = bias / len(chain)

        target_bqm.add_variables_from({u: b for u in chain})

    # next up the quadratic biases, spread the quadratic biases evenly over the
    # available interactions
    for (u, v), bias in source_bqm.quadratic.items():
        interactions = list(embedded_structure.interaction_edge_iter(u, v))

        if not interactions:
            raise MissingEdgeError(u, v)

        b = bias / len(interactions)

        target_bqm.add_interactions_from((u, v, b) for u, v in interactions)

    for v, chain in embedding.items():
        if len(chain) == 1:
            # in the case where the chain has length 1, there are no chain
            # quadratic biases, but we none-the-less want the chain variables to
            # appear in the target_bqm
            q, = chain
            target_bqm.add_variable(q, 0.0)
        elif target_bqm.vartype is dimod.SPIN:
            for p, q in embedded_structure.chain_edge_iter(v):
                target_bqm.add_interaction(p, q, -chain_strength)
                target_bqm.add_offset(chain_strength)
        else:
            # this is in spin, but we need to respect the vartype
            for p, q in embedded_structure.chain_edge_iter(v):
                target_bqm.add_interaction(p, q, -4*chain_strength)
                target_bqm.add_variable(p, 2*chain_strength)
                target_bqm.add_variable(q, 2*chain_strength)

    return target_bqm


def embed_ising(source_h, source_J, embedding, target_adjacency, chain_strength=1.0):
    """Embed an Ising problem onto a target graph.

    Args:
        source_h (dict[variable, bias]/list[bias]):
            Linear biases of the Ising problem. If a list, the list's indices are used as
            variable labels.

        source_J (dict[(variable, variable), bias]):
            Quadratic biases of the Ising problem.

        embedding (dict):
            Mapping from source graph to target graph as a dict of form {s: {t, ...}, ...},
            where s is a source-model variable and t is a target-model variable.

        target_adjacency (dict/:obj:`networkx.Graph`):
            Adjacency of the target graph as a dict of form {t: Nt, ...},
            where t is a target-graph variable and Nt is its set of neighbours.

        chain_strength (float, optional):
            Magnitude of the quadratic bias (in SPIN-space) applied between
            variables to form a chain, with the energy penalty of chain breaks
            set to 2 * `chain_strength`.

    Returns:
        tuple: A 2-tuple:

            dict[variable, bias]: Linear biases of the target Ising problem.

            dict[(variable, variable), bias]: Quadratic biases of the target Ising problem.

    Examples:
        This example embeds a triangular Ising problem representing
        a :math:`K_3` clique into a square target graph by mapping variable `c`
        in the source to nodes `2` and `3` in the target.

        >>> import networkx as nx
        ...
        >>> target = nx.cycle_graph(4)
        >>> # Ising problem biases
        >>> h = {'a': 0, 'b': 0, 'c': 0}
        >>> J = {('a', 'b'): 1, ('b', 'c'): 1, ('a', 'c'): 1}
        >>> # Variable c is a chain
        >>> embedding = {'a': {0}, 'b': {1}, 'c': {2, 3}}
        >>> # Embed and show the resulting biases
        >>> th, tJ = dwave.embedding.embed_ising(h, J, embedding, target)
        >>> th  # doctest: +SKIP
        {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0}
        >>> tJ  # doctest: +SKIP
        {(0, 1): 1.0, (0, 3): 1.0, (1, 2): 1.0, (2, 3): -1.0}


    See also:
        :func:`.embed_bqm`, :func:`.embed_qubo`

    """
    source_bqm = dimod.BinaryQuadraticModel.from_ising(source_h, source_J)
    target_bqm = embed_bqm(source_bqm, embedding, target_adjacency, chain_strength=chain_strength)
    target_h, target_J, __ = target_bqm.to_ising()
    return target_h, target_J


def embed_qubo(source_Q, embedding, target_adjacency, chain_strength=1.0):
    """Embed a QUBO onto a target graph.

    Args:
        source_Q (dict[(variable, variable), bias]):
            Coefficients of a quadratic unconstrained binary optimization (QUBO) model.

        embedding (dict):
            Mapping from source graph to target graph as a dict of form {s: {t, ...}, ...},
            where s is a source-model variable and t is a target-model variable.

        target_adjacency (dict/:obj:`networkx.Graph`):
            Adjacency of the target graph as a dict of form {t: Nt, ...},
            where t is a target-graph variable and Nt is its set of neighbours.

        chain_strength (float, optional):
            Magnitude of the quadratic bias (in SPIN-space) applied between
            variables to form a chain, with the energy penalty of chain breaks
            set to 2 * `chain_strength`.

    Returns:
        dict[(variable, variable), bias]: Quadratic biases of the target QUBO.

    Examples:
        This example embeds a triangular QUBO representing a :math:`K_3` clique
        into a square target graph by mapping variable `c` in the source to nodes
        `2` and `3` in the target.

        >>> import networkx as nx
        ...
        >>> target = nx.cycle_graph(4)
        >>> # QUBO
        >>> Q = {('a', 'b'): 1, ('b', 'c'): 1, ('a', 'c'): 1}
        >>> # Variable c is a chain
        >>> embedding = {'a': {0}, 'b': {1}, 'c': {2, 3}}
        >>> # Embed and show the resulting biases
        >>> tQ = dwave.embedding.embed_qubo(Q, embedding, target)
        >>> tQ  # doctest: +SKIP
        {(0, 1): 1.0,
         (0, 3): 1.0,
         (1, 2): 1.0,
         (2, 3): -4.0,
         (0, 0): 0.0,
         (1, 1): 0.0,
         (2, 2): 2.0,
         (3, 3): 2.0}

    See also:
        :func:`.embed_bqm`, :func:`.embed_ising`

    """
    source_bqm = dimod.BinaryQuadraticModel.from_qubo(source_Q)
    target_bqm = embed_bqm(source_bqm, embedding, target_adjacency, chain_strength=chain_strength)
    target_Q, __ = target_bqm.to_qubo()
    return target_Q


def unembed_sampleset(target_sampleset, embedding, source_bqm,
                      chain_break_method=None, chain_break_fraction=False,
                      return_embedding=False):
    """Unembed a samples set.

    Given samples from a target binary quadratic model (BQM), construct a sample
    set for a source BQM by unembedding.

    Args:
        target_sampleset (:obj:`dimod.SampleSet`):
            Sample set from the target BQM.

        embedding (dict):
            Mapping from source graph to target graph as a dict of form
            {s: {t, ...}, ...}, where s is a source variable and t is a target
            variable.

        source_bqm (:obj:`dimod.BinaryQuadraticModel`):
            Source BQM.

        chain_break_method (function/list, optional):
            Method or methods used to resolve chain breaks. If multiple methods
            are given, the results are concatenated and a new field called
            "chain_break_method" specifying the index of the method is appended
            to the sample set.
            Defaults to :func:`~dwave.embedding.chain_breaks.majority_vote`.
            See :mod:`dwave.embedding.chain_breaks`.

        chain_break_fraction (bool, optional, default=False):
            Add a `chain_break_fraction` field to the unembedded :obj:`dimod.SampleSet`
            with the fraction of chains broken before unembedding.

        return_embedding (bool, optional, default=False):
            If True, the embedding is added to :attr:`dimod.SampleSet.info`
            of the returned sample set. Note that if an `embedding` key
            already exists in the sample set then it is overwritten.

    Returns:
        :obj:`.SampleSet`: Sample set in the source BQM.

    Examples:
       This example unembeds from a square target graph samples of a triangular
       source BQM.

        >>> # Triangular binary quadratic model and an embedding
        >>> J = {('a', 'b'): -1, ('b', 'c'): -1, ('a', 'c'): -1}
        >>> bqm = dimod.BinaryQuadraticModel.from_ising({}, J)
        >>> embedding = {'a': [0, 1], 'b': [2], 'c': [3]}
        >>> # Samples from the embedded binary quadratic model
        >>> samples = [{0: -1, 1: -1, 2: -1, 3: -1},  # [0, 1] is unbroken
        ...            {0: -1, 1: +1, 2: +1, 3: +1}]  # [0, 1] is broken
        >>> energies = [-3, 1]
        >>> embedded = dimod.SampleSet.from_samples(samples, dimod.SPIN, energies)
        >>> # Unembed
        >>> samples = dwave.embedding.unembed_sampleset(embedded, embedding, bqm)
        >>> samples.record.sample   # doctest: +SKIP
        array([[-1, -1, -1],
               [ 1,  1,  1]], dtype=int8)

    """

    if chain_break_method is None:
        chain_break_method = majority_vote
    elif isinstance(chain_break_method, abc.Sequence):
        # we want to apply multiple CBM and then combine
        samplesets = [unembed_sampleset(target_sampleset, embedding,
                                        source_bqm, chain_break_method=cbm,
                                        chain_break_fraction=chain_break_fraction)
                      for cbm in chain_break_method]
        sampleset = dimod.sampleset.concatenate(samplesets)

        # Add a new data field tracking which came from
        # todo: add this functionality to dimod
        cbm_idxs = np.empty(len(sampleset), dtype=np.int)

        start = 0
        for i, ss in enumerate(samplesets):
            cbm_idxs[start:start+len(ss)] = i
            start += len(ss)

        new = np.lib.recfunctions.append_fields(sampleset.record,
                                                'chain_break_method', cbm_idxs,
                                                asrecarray=True, usemask=False)

        return type(sampleset)(new, sampleset.variables, sampleset.info,
                               sampleset.vartype)

    variables = list(source_bqm.variables)  # need this ordered
    try:
        chains = [embedding[v] for v in variables]
    except KeyError:
        raise ValueError("given bqm does not match the embedding")

    record = target_sampleset.record

    unembedded, idxs = chain_break_method(target_sampleset, chains)

    reserved = {'sample', 'energy'}
    vectors = {name: record[name][idxs]
               for name in record.dtype.names if name not in reserved}

    if chain_break_fraction:
        broken = broken_chains(target_sampleset, chains)
        if broken.size:
            vectors['chain_break_fraction'] = broken.mean(axis=1)[idxs]
        else:
            vectors['chain_break_fraction'] = 0

    info = target_sampleset.info.copy()

    if return_embedding:
        embedding_context = dict(embedding=embedding,
                                 chain_break_method=chain_break_method.__name__)
        info.update(embedding_context=embedding_context)

    return dimod.SampleSet.from_samples_bqm((unembedded, variables),
                                            source_bqm,
                                            info=info,
                                            **vectors)

