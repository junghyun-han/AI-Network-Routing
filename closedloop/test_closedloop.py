"""
Closed-loop logic tests that need no Docker, no cluster and no torch
(run in CI):

  - the controller drains exactly once, only when the mapped link is
    flagged, only in proactive mode (fake /detect server + fake lab);
  - the probe analysis computes loss and outage correctly on a synthetic
    ping trace.

    python closedloop/test_closedloop.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from controller import Controller, load_scenario  # noqa: E402
from experiment import analyse  # noqa: E402

SC = load_scenario()
PAIR = (SC["sat_link"]["i"], SC["sat_link"]["j"])
FLAG_T = {SC["first_flag_t"], SC["first_flag_t"] + 1}


class FakeDetect(BaseHTTPRequestHandler):
    def do_GET(self):
        t = int(self.path.split("/detect/")[1].split("?")[0])
        links = [{"i": 1, "j": 2, "pred_contact_s": 0.5}]       # other links
        if t in FLAG_T:
            links.append({"i": PAIR[0], "j": PAIR[1], "pred_contact_s": 1.0})
        body = json.dumps({"t0": t, "tau_s": 2.0, "n_live_links": 300,
                           "n_imminent": len(links), "links": links}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class FakeLab:
    def __init__(self):
        self.calls = []

    def set_cost(self, router, subnet, cost):
        self.calls.append((router, subnet, cost))
        return 100.0

    @staticmethod
    def parallel(fn, items):
        return [fn(x) for x in items]


def test_controller():
    srv = HTTPServer(("127.0.0.1", 0), FakeDetect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    api = "http://127.0.0.1:%d" % srv.server_port
    rp = SC["replay"]
    for mode, expect in (("proactive", 1), ("reactive", 0)):
        lab = FakeLab()
        c = Controller(api, SC, lab, mode=mode)
        for t in range(rp["t_start"], rp["t_end"] + 1):
            ev = c.step(t)
            assert ("action" in ev) == (mode == "proactive" and t == SC["first_flag_t"]), (mode, t, ev)
        drains = [x for x in lab.calls if x[2] == 1000]
        assert len(drains) == 2 * expect, (mode, lab.calls)      # both ends
        assert {x[0] for x in drains} == (set(SC["lab_link"]["ends"]) if expect else set())
        assert all(x[1] == SC["lab_link"]["subnet"] for x in drains)
    srv.shutdown()
    print("controller OK: drains once on the flag (proactive), never (reactive)")


class _Probe:
    pass


def test_analyse():
    p = _Probe()
    p.sent = 900
    lost = set(range(300, 340))                     # 40 consecutive probes
    p.replies = {s: (1000.0 + 0.1 * s + 0.0001, 64 if s < 300 else 63, 0.1)
                 for s in range(1, 901) if s not in lost}
    p.noans = {s: 1000.0 + 0.1 * (s + 1) for s in lost}
    p.errors = {}
    fail_ts = 1000.0 + 0.1 * 300 - 0.05
    r = analyse(p, fail_ts, drain_ts=None)
    assert abs(r["outage_s"] - 4.0) < 1e-3, r["outage_s"]
    assert abs(r["outage_start_rel_s"] + 0.05) < 1e-3, r["outage_start_rel_s"]
    assert r["lost_in_window"] == 40
    assert r["probes_in_window"] == 600, r["probes_in_window"]
    assert abs(r["loss_pct_sent"] - 100 * 40 / 600) < 1e-3
    assert abs(r["loss_pct"] - 100 * 4.0 / 60) < 1e-3      # time-based
    assert r["ttl_last_reply_before_failure"] == 64
    # a stalled sender (no probes for 2 s mid-outage) must not shrink the outage
    q = _Probe()
    q.sent, q.errors = 880, {}
    q.replies = {s: (1000.0 + 0.1 * s + (2.0 if s >= 320 else 0), 64, 0.0)
                 for s in range(1, 881) if not 300 <= s < 320}
    q.noans = {}
    r3 = analyse(q, fail_ts, drain_ts=None)
    assert abs(r3["outage_s"] - 4.0) < 1e-3, r3["outage_s"]
    assert r3["lost_in_window"] == 20
    assert abs(r3["loss_pct"] - 100 * 4.0 / 60) < 1e-3     # not 20 probes' worth
    r2 = analyse(p, fail_ts, drain_ts=fail_ts - 1.5)
    assert r2["drain_lead_s"] == 1.5
    none = _Probe()
    none.sent, none.noans, none.errors = 900, {}, {}
    none.replies = {s: (1000.0 + 0.1 * s, 64, 0.1) for s in range(1, 901)}
    assert analyse(none, fail_ts, None)["outage_s"] == 0.0
    assert analyse(none, fail_ts, None)["loss_pct"] == 0.0
    print("analysis OK: outage 4.0 s, loss %.2f%% on a synthetic trace; "
          "sender stall handled" % r["loss_pct"])


if __name__ == "__main__":
    test_controller()
    test_analyse()
