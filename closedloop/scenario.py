"""
Pick the satellite link whose lifetime the closed loop replays on the lab.

The choice is made by a fixed rule from the simulator, not by hand:

  1. Enumerate every link break in the simulation: a satellite pair that is in
     range at t-1 and out of range at t (t in [2, horizon-2]).
  2. Classify it. *gradual*: the distance step at the break matches the step
     before it (smooth relative motion carries the pair past the 150 km
     range - a link that expires). *discontinuous*: the distance jumps (a
     satellite wraps around at the edge of the bounded simulation area).
     Only gradual breaks are physically meaningful expiries, so only they are
     replayed; both counts are reported.
  3. For each break, find the *lead time*: seconds between the first time the
     serving detector (the same rule as serving/policy_runner.detect, default
     tau_s = 2.0) flags the link inside its last live interval and the break.
  4. Take the median lead over gradual breaks (the typical case, not the best
     case) and keep gradual breaks with exactly that lead, flagged only in the
     final lead seconds (no earlier flicker), and live for the whole
     PRE_ROLL_S seconds before the first flag, so the replay starts with a
     normal, healthy period.
  5. The scenario is the earliest such break (ties: lowest pair index).

Usage:
    python closedloop/scenario.py            # (re)write closedloop/scenario.json
    python closedloop/scenario.py --check    # recompute, assert it matches
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from sim.env_v2 import LeoEnvV2  # noqa: E402

TAU_S = 2.0          # serving /detect default
PRE_ROLL_S = 10      # replay starts this many seconds before the first flag
POST_ROLL_S = 50     # and runs this long after the break
SMOOTH_KM = 1.0      # |step at break - previous step| below this = gradual
LAB_LINK = {"name": "r1-r3", "subnet": "10.0.13.0/24",
            "ends": ["r1", "r3"]}


def flags_at(env: LeoEnvV2, t: int, iu, ju, tau: float) -> np.ndarray:
    """Exactly serving/policy_runner.Router.detect, vectorized over pairs."""
    R = float(env.p.link_range_km)
    cap = float(env.p.TREM_CAP_S)
    dt = float(env.p.time_step)
    D0 = env.pair_dist(t)
    D1 = env.pair_dist(t + 1)
    dd = D1 - D0
    with np.errstate(divide="ignore", invalid="ignore"):
        trem = np.where(dd <= 0, cap,
                        np.maximum(0.0, (R - D0) / np.where(dd <= 0, 1.0, dd)) * dt)
    trem = np.minimum(trem, cap)
    return (D0[iu, ju] <= R) & (trem[iu, ju] <= tau)


def build() -> dict:
    env = LeoEnvV2()
    R = float(env.p.link_range_km)
    H = int(env.p.horizon)
    iu, ju = np.triu_indices(int(env.N), k=1)
    D = np.stack([env.pair_dist(t)[iu, ju] for t in range(H)])      # [H, P]
    live = D <= R
    F = np.stack([flags_at(env, t, iu, ju, TAU_S) for t in range(H - 1)])

    events = []
    for tb in range(2, H - 1):
        for p in np.where(live[tb - 1] & ~live[tb])[0]:
            s = tb - 1
            while s - 1 >= 0 and live[s - 1, p]:
                s -= 1
            fl = np.where(F[s:tb, p])[0]
            first = int(s + fl[0]) if len(fl) else None
            lead = tb - first if first is not None else 0
            step = float(D[tb, p] - D[tb - 1, p])
            prev = float(D[tb - 1, p] - D[tb - 2, p])
            events.append({"pair": (int(iu[p]), int(ju[p])), "p": int(p),
                           "gradual": abs(step - prev) < SMOOTH_KM,
                           "live_from": int(s), "first_flag": first,
                           "break": int(tb), "lead": int(lead),
                           "n_flagged": int(len(fl))})

    def stats(evs):
        leads = np.array([e["lead"] for e in evs])
        vals, cnt = np.unique(leads, return_counts=True)
        return {"n_breaks": int(len(evs)),
                "flagged_before_break_pct":
                    round(100.0 * float((leads >= 1).mean()), 2),
                "lead_s_histogram": {str(int(v)): int(c)
                                     for v, c in zip(vals, cnt)},
                "median_lead_s": int(np.median(leads))}

    grad = [e for e in events if e["gradual"]]
    disc = [e for e in events if not e["gradual"]]
    med = int(np.median([e["lead"] for e in grad]))

    cands = [e for e in grad
             if e["lead"] == med and e["n_flagged"] == med
             and 0 < e["live_from"] <= e["first_flag"] - PRE_ROLL_S]
    cands.sort(key=lambda e: (e["break"], e["pair"]))
    pick = cands[0]
    p = pick["p"]
    t_start = pick["first_flag"] - PRE_ROLL_S
    t_end = pick["break"] + POST_ROLL_S
    trace = [{"t": t, "dist_km": round(float(D[t, p]), 2),
              "live": bool(live[t, p]),
              "flagged": bool(F[t, p]) if t < H - 1 else False}
             for t in range(t_start, min(t_end, H - 1) + 1)]

    return {
        "source": "sim/env_v2.py (68 satellites, 150 km link range, 1 s step)",
        "detector": "serving /detect/{t0}, tau_s=%.1f" % TAU_S,
        "tau_s": TAU_S,
        "all_breaks": stats(events),
        "gradual_breaks": stats(grad),
        "discontinuous_breaks": stats(disc),
        "rule": ("earliest gradual (range-expiry) break with lead == median "
                 "gradual lead, flagged only in the final lead seconds, "
                 "live for the whole %d s pre-roll" % PRE_ROLL_S),
        "n_candidates": len(cands),
        "sat_link": {"i": pick["pair"][0], "j": pick["pair"][1]},
        "live_from_t": pick["live_from"],
        "first_flag_t": pick["first_flag"],
        "break_t": pick["break"],
        "lead_s": pick["lead"],
        "replay": {"t_start": int(t_start), "t_end": int(t_end),
                   "pre_roll_s": PRE_ROLL_S, "post_roll_s": POST_ROLL_S,
                   "speed": 1.0,
                   "note": "1 simulated second = 1 wall-clock second (no compression)"},
        "lab_link": LAB_LINK,
        "trace": trace,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "scenario.json"))
    ap.add_argument("--check", action="store_true",
                    help="recompute and compare with the existing file")
    a = ap.parse_args()
    sc = build()
    if a.check:
        with open(a.out) as f:
            old = json.load(f)
        for k in ["sat_link", "first_flag_t", "break_t", "lead_s", "replay",
                  "lab_link"]:                          # the scenario: exact
            assert old[k] == sc[k], (k, old[k], sc[k])
        for k in ["all_breaks", "gradual_breaks", "discontinuous_breaks"]:
            a, b = old[k]["n_breaks"], sc[k]["n_breaks"]   # stats: float32
            assert abs(a - b) <= 0.005 * a, (k, a, b)      # tolerance
        print("scenario OK: sat link %d-%d flagged at t=%d, breaks at t=%d "
              "(lead %d s); %d gradual breaks, %.1f%% flagged ahead"
              % (sc["sat_link"]["i"], sc["sat_link"]["j"], sc["first_flag_t"],
                 sc["break_t"], sc["lead_s"], sc["gradual_breaks"]["n_breaks"],
                 sc["gradual_breaks"]["flagged_before_break_pct"]))
        return
    with open(a.out, "w") as f:
        json.dump(sc, f, indent=1)
    print(json.dumps({k: v for k, v in sc.items() if k != "trace"}, indent=1))


if __name__ == "__main__":
    main()
