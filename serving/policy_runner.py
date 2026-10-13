"""
Inference wrapper around the trained GCN-PPO routing policy.

Loads the policy once and exposes two operations the network service needs:

    route(t0)   - run the policy from a start time and return the chosen
                  end-to-end route with per-hop predicted contact time and
                  imminent-failure flags.
    detect(t0)  - list the currently-live links predicted to drop within a
                  short window (the proactive anomaly signal).

Physics come from sim/env_v2.py; the network from sim/model_v2.py.
"""

from __future__ import annotations

import os
from typing import Dict, List

import numpy as np
import torch

from sim.env_v2 import LeoEnvV2, NODE_FEAT_DIM, EDGE_FEAT_DIM
from sim.model_v2 import PPOAgentV2

DEFAULT_MODEL = os.environ.get("MODEL_PATH", "serving/model/PPO-GCN_seed0.pt")


class Router:
    def __init__(self, weight_path: str = DEFAULT_MODEL, device: str = "cpu"):
        ck = torch.load(weight_path, map_location=device)
        self.kind = ck["kind"]
        self.agent = PPOAgentV2(kind=self.kind, node_dim=NODE_FEAT_DIM,
                                edge_dim=EDGE_FEAT_DIM, device=device)
        self.agent.load(weight_path)
        self.env = LeoEnvV2()

    # -- end-to-end routing -------------------------------------------------

    def route(self, t0: int) -> Dict:
        env = self.env
        env.p.allow_backtrack = False
        env.reset(t0=int(t0))
        path = [int(env.start)]
        steps: List[Dict] = []
        total, success = 0.0, False
        for _ in range(env.p.max_steps):
            i = int(env.prev_a)
            x = env.node_features()
            adj = env.compute_graph()
            edge = env.edge_features(i)
            mask = env.action_mask()
            if float(mask.sum()) == 0:
                break
            a, _, _ = self.agent.act(x, adj, edge, mask, i, greedy=True)
            trem = env._pred_remaining_contact_time(i, int(a), int(env.t))
            trem = float(min(trem, env.p.TREM_CAP_S))
            _, r, done, info = env.step(int(a))
            total += float(r)
            path.append(int(a))
            steps.append({"from": i, "to": int(a),
                          "pred_contact_s": round(trem, 2),
                          "risky": bool(info.get("risky", False))})
            if done:
                success = bool(info["success"])
                break
        return {"t0": int(t0), "path": path, "hops": len(path) - 1,
                "reward": round(total, 4), "success": success, "steps": steps}

    # -- proactive anomaly detection ---------------------------------------

    def detect(self, t0: int, tau_s: float = 2.0) -> Dict:
        env = self.env
        R = float(env.p.link_range_km)
        cap = float(env.p.TREM_CAP_S)
        dt = float(env.p.time_step)
        D0 = env.pair_dist(int(t0))
        D1 = env.pair_dist(int(t0) + 1)
        dd = D1 - D0
        with np.errstate(divide="ignore", invalid="ignore"):
            trem = np.where(dd <= 0, cap,
                            np.maximum(0.0, (R - D0) / np.where(dd <= 0, 1.0, dd)) * dt)
        trem = np.minimum(trem, cap)
        N = int(env.N)
        iu, ju = np.triu_indices(N, k=1)
        active = D0[iu, ju] <= R
        tr = trem[iu, ju]
        flagged = active & (tr <= float(tau_s))
        links = [{"i": int(iu[k]), "j": int(ju[k]),
                  "pred_contact_s": round(float(tr[k]), 2)}
                 for k in np.where(flagged)[0]]
        links.sort(key=lambda d: d["pred_contact_s"])
        return {"t0": int(t0), "tau_s": float(tau_s),
                "n_live_links": int(active.sum()),
                "n_imminent": len(links), "links": links}
