"""
Time-varying topology helpers shared by the traffic-visibility and
impact-analysis experiments.

Builds, for any time t, the satellite mesh as a graph (an edge where two
satellites are within communication range) plus virtual source/destination
nodes attached to the satellites in range of each ground station. This lets
us ask end-to-end questions - reachability, latency, bottlenecks, and the
effect of a link failure - with standard graph algorithms.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import networkx as nx
import numpy as np

from sim.env_v2 import LeoEnvV2

SRC = "SRC"
DST = "DST"


def in_range_sats(env: LeoEnvV2, t: int, ground_idx: int) -> np.ndarray:
    pos = env.positions(t)
    g = np.array(env.grounds[ground_idx], dtype=np.float32)
    d = np.linalg.norm(pos - g[None, :], axis=1)
    return np.where(d < float(env.p.link_range_km))[0]


def build_graph(env: LeoEnvV2, t: int, weight: str = "dist") -> nx.Graph:
    """Satellite mesh at time t. Edge weight is inter-satellite distance
    (km) if weight='dist', else 1 (hop count)."""
    R = float(env.p.link_range_km)
    D = env.pair_dist(t)
    N = int(env.N)
    G = nx.Graph()
    G.add_nodes_from(range(N))
    iu, ju = np.triu_indices(N, k=1)
    live = D[iu, ju] <= R
    for i, j in zip(iu[live], ju[live]):
        w = float(D[i, j]) if weight == "dist" else 1.0
        G.add_edge(int(i), int(j), weight=w)
    return G


def with_terminals(env: LeoEnvV2, t: int, G: nx.Graph) -> nx.Graph:
    """Attach virtual SRC/DST nodes to the satellites in range of the two
    ground stations (zero-weight access links)."""
    H = G.copy()
    H.add_node(SRC)
    H.add_node(DST)
    for s in in_range_sats(env, t, 0):
        H.add_edge(SRC, int(s), weight=0.0)
    for s in in_range_sats(env, t, 1):
        H.add_edge(int(s), DST, weight=0.0)
    return H


def e2e_path(env: LeoEnvV2, t: int, weight: str = "dist"
             ) -> Optional[Tuple[List[int], float, int]]:
    """Shortest end-to-end route at time t.
    Returns (satellite path, total distance km, ISL hop count) or None if
    source and destination are disconnected."""
    G = build_graph(env, t, weight=weight)
    H = with_terminals(env, t, G)
    if not (H.has_node(SRC) and H.has_node(DST)) or not nx.has_path(H, SRC, DST):
        return None
    nodes = nx.shortest_path(H, SRC, DST, weight="weight")
    sats = [n for n in nodes if n not in (SRC, DST)]
    # geometric length over the satellite hops
    D = env.pair_dist(t)
    dist = sum(float(D[sats[k], sats[k + 1]]) for k in range(len(sats) - 1))
    hops = max(0, len(sats) - 1)
    return sats, dist, hops
