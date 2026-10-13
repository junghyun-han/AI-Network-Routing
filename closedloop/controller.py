"""
Closed-loop controller: detect -> decide -> act, on a live lab.

Every simulated second the controller asks the routing-policy service
(serving/, deployed on the kind Kubernetes cluster) which live links are
about to drop:  GET /detect/{t0}?tau_s=2.0.  If the satellite link mapped
onto the lab link r1-r3 is flagged, it *drains* that link before it fails:
raises the OSPF cost of the link on both ends (vtysh), so OSPF moves traffic
to r1-r2-r3 while r1-r3 still works. Every step is exported as Prometheus
metrics (port 9101 by default).

modes
    proactive   act on the detector (the closed loop)
    reactive    same polling, no action (baseline: OSPF recovers on its own)

The drain is latched: once applied it stays until reset() (the experiment
restores the link between trials).

Standalone (one replay of the scenario, no failure injected; shows the loop
flag the link and drain it, then undrains):
    python closedloop/controller.py --api http://127.0.0.1:30080
The measured experiment (failure injection + packet-loss probe) is
closedloop/experiment.py.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from typing import Dict, List, Optional

from prometheus_client import Counter, Gauge, Histogram, start_http_server

try:                                   # run as a script or as a module
    from lab import Lab
except ImportError:                    # pragma: no cover
    from closedloop.lab import Lab

HERE = os.path.dirname(os.path.abspath(__file__))

# -- Prometheus metrics -------------------------------------------------------
SIM_T = Gauge("closedloop_sim_time_seconds", "replay clock (simulated second)")
DETECT_REQS = Counter("closedloop_detect_requests_total",
                      "detect calls to the policy service", ["result"])
DETECT_LAT = Histogram("closedloop_detect_latency_seconds",
                       "detect call latency (controller -> K8s Service)",
                       buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5))
IMMINENT = Gauge("closedloop_detect_imminent_links",
                 "links flagged imminent-loss in the last detect call")
FLAGGED = Gauge("closedloop_link_flagged",
                "1 if the mapped link was flagged in the last detect call",
                ["link"])
FLAG_EVENTS = Counter("closedloop_link_flag_events_total",
                      "detect calls that flagged the mapped link", ["link"])
PRED = Gauge("closedloop_link_pred_contact_seconds",
             "predicted remaining contact time of the mapped link "
             "(NaN when not flagged)", ["link"])
DRAINED = Gauge("closedloop_link_drained",
                "1 while the controller holds the link drained", ["link"])
DRAINS = Counter("closedloop_drain_actions_total",
                 "proactive drains applied by the controller", ["link"])
MAPPING = Gauge("closedloop_mapping_info",
                "lab link <- replayed satellite link", ["link", "sat_link"])


def load_scenario(path: Optional[str] = None) -> Dict:
    with open(path or os.path.join(HERE, "scenario.json")) as f:
        return json.load(f)


def sleep_until(deadline: float) -> None:
    d = deadline - time.monotonic()
    if d > 0:
        time.sleep(d)


class Controller:
    def __init__(self, api: str, scenario: Dict, lab: Lab,
                 mode: str = "proactive", drain_cost: int = 1000,
                 timeout_s: float = 2.0):
        assert mode in ("proactive", "reactive"), mode
        self.api = api.rstrip("/")
        self.pair = (int(scenario["sat_link"]["i"]), int(scenario["sat_link"]["j"]))
        self.tau_s = float(scenario["tau_s"])
        self.link = scenario["lab_link"]
        self.name = self.link["name"]
        self.lab = lab
        self.mode = mode
        self.drain_cost = int(drain_cost)
        self.timeout_s = timeout_s
        self.drained_at: Optional[float] = None      # router clock
        self.log: List[Dict] = []
        MAPPING.labels(self.name, "%d-%d" % self.pair).set(1)

    # -- observe / detect ---------------------------------------------------

    def detect(self, t: int) -> Optional[Dict]:
        url = f"{self.api}/detect/{int(t)}?tau_s={self.tau_s}"
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as r:
                d = json.load(r)
        except Exception as e:                      # fail safe: no action
            DETECT_REQS.labels("error").inc()
            return {"error": repr(e)}
        lat = time.perf_counter() - t0
        DETECT_REQS.labels("ok").inc()
        DETECT_LAT.observe(lat)
        hit = next((l for l in d["links"]
                    if (int(l["i"]), int(l["j"])) == self.pair), None)
        IMMINENT.set(d["n_imminent"])
        FLAGGED.labels(self.name).set(1 if hit else 0)
        if hit:
            FLAG_EVENTS.labels(self.name).inc()
        PRED.labels(self.name).set(hit["pred_contact_s"] if hit else float("nan"))
        return {"n_imminent": int(d["n_imminent"]), "flagged": hit is not None,
                "pred_contact_s": hit["pred_contact_s"] if hit else None,
                "latency_s": round(lat, 4)}

    # -- decide / act -------------------------------------------------------

    def step(self, t: int) -> Dict:
        SIM_T.set(t)
        ev = {"t": int(t), "mode": self.mode, **self.detect(t)}
        if (ev.get("flagged") and self.mode == "proactive"
                and self.drained_at is None):
            self.drain()
            ev["action"] = "drain"
        self.log.append(ev)
        return ev

    def drain(self) -> float:
        """Raise the OSPF cost on both ends at once; returns the router clock
        when the later of the two finished."""
        sub = self.link["subnet"]
        ts = self.lab.parallel(
            lambda r: self.lab.set_cost(r, sub, self.drain_cost), self.link["ends"])
        self.drained_at = max(ts)
        DRAINS.labels(self.name).inc()
        DRAINED.labels(self.name).set(1)
        return self.drained_at

    def reset(self) -> None:
        sub = self.link["subnet"]
        self.lab.parallel(lambda r: self.lab.set_cost(r, sub, None),
                          self.link["ends"])
        self.drained_at = None
        self.log = []
        DRAINED.labels(self.name).set(0)
        FLAGGED.labels(self.name).set(0)


def run_replay(ctrl: Controller, scenario: Dict, w0: Optional[float] = None,
               speed: float = 1.0) -> float:
    """Drive the controller on the replay clock: sim second t is polled at
    wall time w0 + (t - t_start) / speed."""
    rp = scenario["replay"]
    w0 = time.monotonic() if w0 is None else w0
    for t in range(rp["t_start"], rp["t_end"] + 1):
        sleep_until(w0 + (t - rp["t_start"]) / speed)
        ctrl.step(t)
    return w0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", default="http://127.0.0.1:30080",
                    help="policy service (kind NodePort by default)")
    ap.add_argument("--mode", default="proactive",
                    choices=["proactive", "reactive"])
    ap.add_argument("--metrics-port", type=int, default=9101)
    ap.add_argument("--keep", action="store_true",
                    help="leave the drain in place at the end")
    a = ap.parse_args()

    start_http_server(a.metrics_port)
    sc = load_scenario()
    ctrl = Controller(a.api, sc, Lab(), mode=a.mode)
    ctrl.reset()
    print(f"replaying sat link {sc['sat_link']['i']}-{sc['sat_link']['j']} "
          f"on lab link {sc['lab_link']['name']}  (t={sc['replay']['t_start']}"
          f"..{sc['replay']['t_end']}, break at t={sc['break_t']})")
    rp = sc["replay"]
    w0 = time.monotonic()
    for t in range(rp["t_start"], min(rp["t_end"], sc["break_t"] + 2) + 1):
        sleep_until(w0 + (t - rp["t_start"]))
        ev = ctrl.step(t)
        dev = ctrl.lab.route_dev("r1", "10.255.3.3")
        print(f"t={t:4d}  flagged={ev.get('flagged')!s:5}  "
              f"pred={ev.get('pred_contact_s')}  action={ev.get('action', '-'):5}"
              f"  r1->r3 loopback via {dev}")
    if not a.keep:
        ctrl.reset()


if __name__ == "__main__":
    main()
