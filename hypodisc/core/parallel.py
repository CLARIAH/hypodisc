#! /usr/bin/env python

import concurrent.futures as cf
from multiprocessing import Manager, Lock
from multiprocessing.managers import ListProxy
from random import random
from typing import Counter, Literal, Optional

import numpy as np
from hypodisc.data.utils import write_query

from rdf.formats import NTriples
from rdf.namespaces import RDF, RDFS
from rdf.terms import IRIRef

from hypodisc.data.graph import KnowledgeGraph
from hypodisc.core.sequential import (explore, infer_type, new_graph_pattern,
                                      new_var_graph_pattern,
                                      new_mm_graph_pattern)
from hypodisc.core.structures import (GraphPattern,
                                      DataTypeVariable,
                                      MultiModalNumericVariable,
                                      MultiModalStringVariable,
                                      ObjectTypeVariable)
from hypodisc.core.utils import predict_hash
from hypodisc.multimodal.clustering import (compute_clusters,
                                            SUPPORTED_XSD_TYPES)
from hypodisc.multimodal.datatypes import (XSD_DATEFRAG, XSD_DATETIME,
                                           XSD_NUMERIC, XSD_STRING)


XSD_TEMPORAL = set.union(XSD_DATETIME, XSD_DATEFRAG)
IGNORE_PREDICATES = {RDF + 'type', RDFS + 'label'}

def init_lock(plock:Lock) -> None:
    """
    Initiate a global variable lock

    :param plock:
    """
    global lock
    lock = plock


def generate(root_patterns: dict[str, list],
             depths: range, min_support: int,
             p_explore: float, p_extend: float,
             max_length: int, max_width: int,
             out_writer: Optional[NTriples],
             out_prefix_map: Optional[dict[str, str]],
             out_ns: Optional[IRIRef],
             strategy: Literal["BFS", "DFS"]) -> int:
    """ Generate all patterns up to and including a maximum depth which
        satisfy a minimal support.

    :param depths:
    :type depths: range
    :param min_support:
    :type min_support: int
    :param p_explore:
    :type p_explore: float
    :param p_extend:
    :type p_extend: float
    :param max_length:
    :type max_length: int
    :param max_width:
    :type max_width: int
    """

    if strategy == "BFS":
        return generate_bf(root_patterns=root_patterns,
                           depths=depths,
                           min_support=min_support,
                           p_explore=p_explore,
                           p_extend=p_extend,
                           max_length=max_length,
                           max_width=max_width,
                           out_writer=out_writer,
                           out_prefix_map=out_prefix_map,
                           out_ns=out_ns)
    else:  # DFS
        return generate_df(root_patterns=root_patterns,
                           depths=depths,
                           min_support=min_support,
                           p_explore=p_explore,
                           p_extend=p_extend,
                           max_length=max_length,
                           max_width=max_width,
                           out_writer=out_writer,
                           out_prefix_map=out_prefix_map,
                           out_ns=out_ns)


def generate_df(root_patterns: dict[str, list],
                depths: range, min_support: int,
                p_explore: float, p_extend: float,
                max_length: int, max_width: int,
                out_writer: Optional[NTriples],
                out_prefix_map: Optional[dict[str, str]],
                out_ns: Optional[IRIRef]) -> int:
    """ Generate all patterns up to and including a maximum depth which
        satisfy a minimal support, using a depth first approach.

    :param depths:
    :type depths: range
    :param min_support:
    :type min_support: int
    :param p_explore:
    :type p_explore: float
    :param p_extend:
    :type p_extend: float
    :param max_length:
    :type max_length: int
    :param max_width:
    :type max_width: int
    :rtype: None
    """

    patterns = set()
    num_patterns = 0
    with Manager() as manager:
        for name in sorted(list(root_patterns.keys())):
            print(f"type {name}")

            visited = manager.list()
            for depth in range(0, depths.stop):
                print(" exploring depth {} / {}".format(depth+1, depths.stop),
                      end=" ")

                if depth == 0:
                    patterns = root_patterns[name]

                derivatives = set()
                if depth <= 0:
                    # add these as parents for next depth
                    for pattern in patterns:
                        if isinstance(pattern.assertion.rhs,
                                      ObjectTypeVariable):
                            derivatives.add(pattern)

                lock = Lock()
                with cf.ProcessPoolExecutor(initializer=init_lock,
                                            initargs=(lock,)) as executor:
                    fcandidates = [executor.submit(compute_candidates,
                                                   root_patterns, pattern,
                                                   depth, p_explore, p_extend,
                                                   visited)
                                   for pattern in patterns
                                   if len(pattern) < max_length
                                   and pattern.width() < max_width]

                    fextensions = list()
                    for fcandidate in cf.as_completed(fcandidates):
                        pattern, candidates = fcandidate.result()

                        # start as soon as candidates drop in
                        fextensions.append(executor.submit(explore,
                                                           pattern, candidates,
                                                           max_length,
                                                           max_width,
                                                           min_support))

                    for fextension in cf.as_completed(fextensions):
                        extensions = fextension.result()
                        derivatives |= extensions

                        if out_writer is not None\
                                and out_prefix_map is not None:
                            for pattern in extensions:
                                num_patterns = write_query(out_writer, pattern,
                                                           num_patterns,
                                                           out_ns,
                                                           out_prefix_map)

                print("(+{} discovered)".format(len(derivatives)))

                # omit exhausted classes from next iteration
                if len(derivatives) > 0:
                    patterns = {v for v in derivatives}
                else:
                    break

    return num_patterns


def generate_bf(root_patterns: dict[str, list],
                depths: range, min_support: int,
                p_explore: float, p_extend: float,
                max_length: int, max_width: int,
                out_writer: Optional[NTriples],
                out_prefix_map: Optional[dict[str, str]],
                out_ns: Optional[IRIRef]) -> int:
    """ Generate all patterns up to and including a maximum depth which
        satisfy a minimal support, using a breadth first approach.

    :param depths:
    :type depths: range
    :param min_support:
    :type min_support: int
    :param p_explore:
    :type p_explore: float
    :param p_extend:
    :type p_extend: float
    :param max_length:
    :type max_length: int
    :param max_width:
    :type max_width: int
    :rtype: None
    """

    num_patterns = 0
    with Manager() as manager:
        parents = dict()
        for depth in range(0, depths.stop):
            print("exploring depth {} / {}".format(depth+1, depths.stop))

            if depth == 0:
                parents = root_patterns

            derivatives = dict()
            visited = manager.list()
            for name in sorted(list(parents.keys())):
                print(f" type {name}", end=" ")

                derivatives[name] = set()
                patterns = parents[name]
                if depth > 0:
                    patterns = parents.pop(name)
                else:
                    # add these as parents for next depth
                    for pattern in patterns:
                        if isinstance(pattern.assertion.rhs,
                                      ObjectTypeVariable):
                            derivatives[name].add(pattern)

                lock = Lock()
                with cf.ProcessPoolExecutor(initializer=init_lock,
                                            initargs=(lock,)) as executor:
                    fcandidates = [executor.submit(compute_candidates,
                                                   root_patterns, pattern,
                                                   depth, p_explore, p_extend,
                                                   visited)
                                   for pattern in patterns
                                   if len(pattern) < max_length
                                   and pattern.width() < max_width]

                    fextensions = list()
                    for fcandidate in cf.as_completed(fcandidates):
                        pattern, candidates = fcandidate.result()

                        # start as soon as candidates drop in
                        fextensions.append(executor.submit(explore,
                                                           pattern, candidates,
                                                           max_length,
                                                           max_width,
                                                           min_support))

                    for fextension in cf.as_completed(fextensions):
                        extensions = fextension.result()
                        derivatives[name] |= extensions

                        if out_writer is not None\
                                and out_prefix_map is not None:
                            for pattern in extensions:
                                num_patterns = write_query(out_writer, pattern,
                                                           num_patterns,
                                                           out_ns,
                                                           out_prefix_map)

                print("(+{} discovered)".format(len(derivatives[name])))

            # omit exhausted classes from next iteration
            parents = {k: v for k, v in derivatives.items()
                       if len(v) > 0}

    return num_patterns


def compute_candidates(root_patterns: dict, pattern: GraphPattern, depth: int,
                       p_explore: float, p_extend: float,
                       visited: ListProxy)\
                               -> tuple[GraphPattern, set]:
    """ Compute and return all candidates for this pattern.

    :param root_patterns:
    :type root_patterns: dict
    :param pattern:
    :type pattern: GraphPattern
    :param depth:
    :type depth: int
    :param p_explore:
    :type p_explore: float
    :param p_extend:
    :type p_extend: float
    :param visited:
    :type visited: ListProxy
    :rtype: tuple[GraphPattern,set]
    """
    if depth <= 0:
        endpoints = {pattern.root}
    else:
        # Gather candidate endpoints at this depth.
        # Only consider unbound object type variables since we
        # cannot connect to literals or data type variables, and
        # bound entities already have a fixed context.
        endpoints = {a.rhs for a in pattern.distances[depth-1]
                     if isinstance(a.rhs, ObjectTypeVariable)}

    candidates = set()
    for endpoint in endpoints:
        if endpoint.value not in root_patterns.keys():
            # no extension available
            continue

        if p_explore < random():
            # skip this endpoint with probability p_explore
            continue

        # Gather all candidate extensions that can connect
        # to an object type variable of the relevant type.
        for base_pattern in root_patterns[endpoint.value]:
            if p_extend < random():
                # skip this extension with probability p_extend
                continue

            # get base assertion
            extension = base_pattern.assertion

            pattern_hash = predict_hash(pattern,
                                        endpoint,
                                        extension)

            skip = False
            with lock:
                if pattern_hash in visited\
                        or pattern.contains_at_depth(extension, depth):
                    skip = True
                if not skip:
                    visited.append(pattern_hash)

            if skip:
                continue

            candidates.add((endpoint, extension))

    return pattern, candidates


def init_root_patterns(rng: np.random.Generator, kg: KnowledgeGraph,
                       min_support: float, mode: Literal["A", "AT", "T"],
                       textual_support: bool, numerical_support: bool,
                       temporal_support: bool) -> dict[str, list]:
    """ Creating all patterns of types which satisfy minimal support.

    :param rng:
    :type rng: np.random.Generator
    :param kg:
    :type kg: KnowledgeGraph
    :param min_support:
    :type min_support: float
    :param mode:
    :type mode: Literal["A", "AT", "T"]
    :param multimodal:
    :type multimodal: bool
    :returns:
    :rtype: dict[str,list]
    """
    print("mapping root patterns")
    root_patterns = dict()

    rdf_type = RDF + "type"
    rdf_type_idx = kg.r2i[rdf_type]

    A_type = kg.A[rdf_type_idx]

    class_idx_list = sorted(list(set(A_type.col)))
    class_freq = A_type.sum(axis=0)[class_idx_list]
    for class_i, class_idx in enumerate(class_idx_list):
        # if the number of type instances do not exceed the minimal support
        # then any pattern of this type will not either
        support = class_freq[class_i]
        if support < min_support:
            continue

        class_name = kg.i2n[class_idx]
        root_patterns[class_name] = set()

        print(f" type {class_name}...", end='')

        # find all members of this class
        class_members_idx = A_type.row[A_type.col == class_idx]

        root_var = ObjectTypeVariable(class_name)
        with cf.ProcessPoolExecutor() as executor:
            futures = [executor.submit(compute_root_patterns, rng, kg,
                                       min_support, mode, textual_support,
                                       numerical_support, temporal_support,
                                       p_idx, rdf_type_idx, root_var,
                                       class_members_idx)
                       for p_idx in range(kg.num_relations)
                       if p_idx != rdf_type_idx
                       and len(set(kg.A[p_idx].row) &
                               set(class_members_idx)) >= min_support]

            for future in cf.as_completed(futures):
                root_patterns[class_name] |= future.result()

        tree_size = len(root_patterns[class_name])
        print(" (+{} discovered)".format(tree_size))

        if tree_size <= 0:
            del root_patterns[class_name]

    return root_patterns


def compute_root_patterns(rng: np.random.Generator, kg: KnowledgeGraph,
                          min_support: float, mode: Literal["A", "AT", "T"],
                          textual_support: bool, numerical_support: bool,
                          temporal_support: bool, p_idx: int,
                          rdf_type_idx: int, root_var: ObjectTypeVariable,
                          class_members_idx: np.ndarray) -> set[GraphPattern]:
    """ Compute all root patterns with this predicate.

    :param rng:
    :type rng: np.random.Generator
    :param kg:
    :type kg: KnowledgeGraph
    :param min_support:
    :type min_support: float
    :param mode:
    :type mode: Literal["A", "AT", "T"]
    :param multimodal:
    :type multimodal: bool
    :param p_idx:
    :type p_idx: int
    :param rdf_type_idx:
    :type rdf_type_idx: int
    :param root_var:
    :type root_var: ObjectTypeVariable
    :param class_members_idx:
    :type class_members_idx: np.ndarray
    :rtype: set[GraphPattern]
    """
    # don't generate what we won't need
    generate_Abox = False
    generate_Tbox = False
    if "A" in mode:
        generate_Abox = True
    if "T" in mode:
        generate_Tbox = True

    multimodal = textual_support | numerical_support | temporal_support

    p = kg.i2r[p_idx]

    root_patterns = set()

    # mask for class members that have this relation outgoing
    p_mask = np.isin(kg.A[p_idx].row, class_members_idx)

    # list of global indices for class members with this property
    # plus the matching tail nodes
    s_idx_list = kg.A[p_idx].row[p_mask]
    o_idx_list = kg.A[p_idx].col[p_mask]

    # infer (data) type from single tail node (assume rest is same)
    o_type, is_literal = infer_type(kg, rdf_type_idx, o_idx_list[-1])

    # add graph_patterns with variables as objects
    # assume that all objects linked by the same relation are of
    # the same type. This may not always be true but it is usually
    # the case in well-engineered graphs. See this as an
    # optimization by approximation.
    pattern = None
    o_idx = o_idx_list[0]
    inv_assertion_map = {o_idx: {s_idx_list[i]
                                 for i, idx in enumerate(o_idx_list)
                                 if idx == o_idx} for o_idx in o_idx_list}
    if generate_Tbox and o_idx in kg.ni2ai.keys():
        # object is literal
        o_type = kg.i2a[kg.ni2ai[o_idx]]
        var_o = DataTypeVariable(o_type)

        pattern = new_var_graph_pattern(root_var, var_o,
                                        set(s_idx_list), p,
                                        inv_assertion_map)
    else:
        # object is entity (or bnode or literal without type)
        idx = np.where(kg.A[rdf_type_idx].row == o_idx)
        o_type_idx = kg.A[rdf_type_idx].col[idx]
        if len(o_type_idx) > 0:
            o_type = kg.i2n[o_type_idx[0]]
            var_o = ObjectTypeVariable(o_type)

            pattern = new_var_graph_pattern(root_var, var_o,
                                            set(s_idx_list), p,
                                            inv_assertion_map)

    if pattern is not None and pattern.support >= min_support:
        root_patterns.add(pattern)

    # create graph_patterns for all predicate-object pairs
    # treat both entities and literals as node
    if generate_Abox:
        if multimodal and is_literal:
            o_freqs = Counter([kg.i2n[i].value for i in o_idx_list])
            o_value_idx_map = {v: {i for i, o_idx in enumerate(o_idx_list)
                                   if kg.i2n[o_idx].value == v}
                               for v in o_freqs.keys()}
        else:  # is IRI
            o_freqs = Counter(o_idx_list)
            o_value_idx_map = {kg.i2n[k]:
                               {i for i, o_idx in enumerate(o_idx_list)
                                if k == o_idx} for k in o_freqs.keys()}

        for o_value, o_freq in o_freqs.items():
            if o_freq < min_support:
                continue

            if not is_literal:
                o_value = kg.i2n[o_value]

            domain = {s_idx_list[i] for i in o_value_idx_map[o_value]}
            inv_assertion_map = {o_idx_list[i]: domain
                                 for i in o_value_idx_map[o_value]}

            # create new graph_pattern
            pattern = new_graph_pattern(root_var, p, o_value,
                                        o_type, domain, inv_assertion_map)

            if pattern is not None and pattern.support >= min_support:
                root_patterns.add(pattern)

    if multimodal:
        # assume that all objects linked by the same relation are of
        # the same type. This may not always be true but it is usually
        # the case in well-engineered graphs. See this as an
        # optimization by approximation.
        o_idx = o_idx_list[0]
        if o_idx in kg.ni2ai.keys():
            # object is literal
            o_type = kg.i2a[kg.ni2ai[o_idx]]
            if o_type not in SUPPORTED_XSD_TYPES:
                return root_patterns

            if not textual_support and o_type in XSD_STRING:
                return root_patterns
            if not numerical_support and o_type in XSD_NUMERIC:
                return root_patterns
            if not temporal_support and o_type in XSD_TEMPORAL:
                return root_patterns

            o_values = [kg.i2n[i].value for i in o_idx_list]
            if len(o_values) >= min_support:
                # if the full set does not exceed the threshold then nor
                # will subsets thereof
                os_idx_map = dict(zip(o_idx_list, s_idx_list))
                clusters = compute_clusters(rng, o_type,
                                            o_values,
                                            o_idx_list)
                for members, cluster in clusters:
                    if o_type in set.union(XSD_NUMERIC,
                                           XSD_DATETIME,
                                           XSD_DATEFRAG):
                        var_o = MultiModalNumericVariable(o_type, cluster)
                    else:  # treat as strings
                        var_o = MultiModalStringVariable(o_type, cluster)

                    domain = {os_idx_map[i] for i in members}
                    inv_assertion_map = {o_idx:
                                         domain for o_idx in members}
                    pattern = new_mm_graph_pattern(root_var, var_o,
                                                   domain, p,
                                                   inv_assertion_map)

                    if pattern is not None\
                            and pattern.support >= min_support:
                        root_patterns.add(pattern)

    return root_patterns
