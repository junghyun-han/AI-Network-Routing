"""
Measure the closed loop: proactive (detect -> drain -> failure) versus
reactive (failure only, OSPF recovers by itself), on the live lab.

One trial:
  1. Clean baseline: link restored, default cost, OSPF Full on r1-r3 and both
     loopbacks routed over it; then wait 6 s (> OSPF min LSA interval 5 s).
  2. Start a probe: ping every 0.1 s from r1 (source loopback 10.255.1.1) to
     r3's loopback 10.255.3.3. The r1-r3 link is the primary path.
  3. Replay the scenario on a 1:1 clock (closedloop/scenario.json). The
     controller polls the K8s-served detector every simulated second and, in
     proactive mode, drains r1-r3. Independently, a "world" thread breaks the
     lab link at the scenario's real break time (ground truth from the
     simulator) - the controller never sees ground truth.
  4. Analyse probe loss in a fixed window [failure - 10 s, failure + 50 s].
  5. Restore the link.

Conditions (each x {proactive, reactive} x N trials, modes interleaved):
  silent-default   netem 100% loss on both ends, FRR default timers (10/40 s)
  silent-fast      same, hello 1 s / dead 4 s set at runtime on r1-r3 only
  carrier-default  ip link set down on both ends, default timers

Usage (lab + kind service up, see closedloop/README.md):
    python closedloop/experiment.py --trials 5
Writes closedloop/results/{trials.csv, summary.csv, summary.json,
probes.csv.gz, events.csv.gz, ping_raw.txt.gz, fig_closedloop.png}.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import re
import subprocess
import threading
import time
from typing import Dict, List, Optional

import numpy as np
from prometheus_client import Counter, Gauge, start_http_server

from controller import (Controller, load_scenario, run_replay, sleep_until,
                        HERE)
from lab import Lab

CONDITIONS = {
    "silent-default": {"failure": "silent", "timers": None},
    "silent-fast": {"failure": "silent", "timers": (1, 4)},
    "carrier-default": {"failure": "carrier", "timers": None},
}
MODES = ["proactive", "reactive"]
PROBE = {"router": "r1", "src": "10.255.1.1", "dst": "10.255.3.3"}
PEER_LOOPBACK = {"r1": "10.255.3.3", "r3": "10.255.1.1"}
INTERVAL_S = 0.1
WIN_PRE_S, WIN_POST_S = 10.0, 50.0
SETTLE_S = 6.0

# -- Prometheus (the controller's metrics live in controller.py) -------------
FAILED = Gauge("closedloop_link_failed",
               "1 while the emulated failure is applied to the lab link", ["link"])
PROBE_UP = Gauge("closedloop_probe_up", "1 if the last probe got a reply")
PROBE_REPLIES = Counter("closedloop_probe_replies_total", "probe replies")
PROBE_LOST = Counter("closedloop_probe_lost_total", "probes with no reply")
T_LABELS = ["mode", "condition"]
T_LOSS = Gauge("closedloop_trial_loss_ratio",
               "probe loss in the window, last trial", T_LABELS)
T_OUT = Gauge("closedloop_trial_outage_seconds",
              "longest probe outage, last trial", T_LABELS)
T_LEAD = Gauge("closedloop_trial_drain_lead_seconds",
               "drain applied this long before the failure, last trial", T_LABELS)
T_COUNT = Counter("closedloop_trials_total", "completed trials", T_LABELS)

REPLY_RE = re.compile(r"^\[(\d+\.\d+)\] \d+ bytes from \S+ icmp_seq=(\d+) "
                      r"ttl=(\d+) time=([\d.]+) ms")
NOANS_RE = re.compile(r"^\[(\d+\.\d+)\] no answer yet for icmp_seq=(\d+)")
ERROR_RE = re.compile(r"^\[(\d+\.\d+)\] From \S+ icmp_seq=(\d+) (.+)")
SENT_RE = re.compile(r"(\d+) packets transmitted")


class Probe(threading.Thread):
    """ping inside r1, parsed line by line (live metrics + post analysis).

    Stopped with SIGINT by coreutils `timeout`, not with ping -w: with a
    deadline, iputils ping exits on its first error, and a silent failure
    does produce errors (ARP for the dead neighbour fails after ~35 s ->
    "Destination Host Unreachable"), which would cut the trace short."""

    def __init__(self, lab: Lab, duration_s: int):
        super().__init__(daemon=True)
        self.cmd = ["docker", "exec", lab.ctr(PROBE["router"]),
                    "timeout", "-s", "INT", str(int(duration_s)),
                    "ping", "-n", "-D", "-O", "-i", str(INTERVAL_S),
                    "-I", PROBE["src"], PROBE["dst"]]
        self.replies: Dict[int, tuple] = {}     # seq -> (recv ts, ttl, rtt ms)
        self.noans: Dict[int, float] = {}       # seq -> first "no answer yet"
        self.errors: Dict[int, str] = {}        # seq -> ICMP/local error text
        self.sent = 0
        self.raw: List[str] = []

    def run(self):
        p = subprocess.Popen(self.cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
        counted = set()
        for line in p.stdout:
            self.raw.append(line.rstrip("\n"))
            m = REPLY_RE.match(line)
            if m:
                self.replies.setdefault(int(m.group(2)), (
                    float(m.group(1)), int(m.group(3)), float(m.group(4))))
                PROBE_UP.set(1)
                PROBE_REPLIES.inc()
                continue
            m = NOANS_RE.match(line) or ERROR_RE.match(line)
            if m:
                seq = int(m.group(2))
                if line.find("no answer yet") >= 0:
                    self.noans.setdefault(seq, float(m.group(1)))
                else:
                    self.errors.setdefault(seq, m.group(3))
                if seq not in counted:
                    counted.add(seq)
                    PROBE_UP.set(0)
                    PROBE_LOST.inc()
                continue
            m = SENT_RE.search(line)
            if m:
                self.sent = int(m.group(1))
        p.wait()


def analyse(probe: Probe, fail_ts: float, drain_ts: Optional[float]) -> Dict:
    """Loss and outage in the window [fail - WIN_PRE_S, fail + WIN_POST_S].

    Send time of every probe, from the router clock: a reply gives
    recv - rtt; a "no answer yet for N" line is printed as probe N+1 is sent.
    Other probes are interpolated. (ping briefly stalls while ARP for a dead
    neighbour is unresolved, so a fixed 0.1 s grid would be wrong.)
    Outage = longest time between two consecutive answered probes that have
    lost probes between them (0 if nothing was lost).
    Loss = share of the window without answers (sum of those gaps / window),
    i.e. the loss a constant 10 pkt/s stream would see. ping's own count
    (loss_pct_sent) understates it during a silent failure: ping's small
    socket buffer fills with packets held for the unresolved neighbour, so
    it sends only ~5 probes per 3 s ARP cycle."""
    sent = probe.sent or max([*probe.replies, *probe.noans, *probe.errors, 0])
    known = {s: ts - rtt / 1000.0 for s, (ts, _, rtt) in probe.replies.items()}
    for s, ts in probe.noans.items():
        known.setdefault(s + 1, ts)
    xs = np.array(sorted(known))
    allseq = np.arange(1, sent + 1)
    t_send = np.interp(allseq, xs, [known[x] for x in xs])
    ok = np.isin(allseq, list(probe.replies))
    w0, w1 = fail_ts - WIN_PRE_S, fail_ts + WIN_POST_S
    win = (t_send >= w0) & (t_send < w1)
    lost = win & ~ok
    interval = float(np.median(np.diff(t_send[ok]))) if ok.sum() > 1 else INTERVAL_S

    # gaps between consecutive answered probes (window edges act as answers)
    ans = [(-1, w0)] + [(int(q), float(t)) for q, t in zip(allseq[win & ok],
                                                          t_send[win & ok])]
    ans.append((sent + 1, w1))
    best, best_at, total = 0.0, None, 0.0
    for (qa, ta), (qb, tb) in zip(ans, ans[1:]):
        n_lost = int(lost[(allseq > qa) & (allseq < qb)].sum())
        if n_lost == 0:
            continue
        gap = max(0.0, tb - ta - interval) if qa >= 0 else tb - ta
        total += gap
        if gap > best:
            best, best_at = gap, ta - fail_ts
    before = [q for q, (ts, _, _) in probe.replies.items() if ts < fail_ts]
    ttl_before = probe.replies[max(before)][1] if before else None
    return {
        "probe_interval_s": round(interval, 4),
        "probes_in_window": int(win.sum()),
        "lost_in_window": int(lost.sum()),
        "loss_pct": round(100.0 * total / (w1 - w0), 3),
        "loss_pct_sent": round(100.0 * lost.sum() / max(1, win.sum()), 3),
        "outage_s": round(best, 3),
        "outage_start_rel_s": None if best_at is None else round(best_at, 3),
        "unreachable_s_total": round(total, 3),
        "probe_errors": len(probe.errors),
        "ttl_last_reply_before_failure": ttl_before,
        "drain_lead_s": None if drain_ts is None else round(fail_ts - drain_ts, 3),
        "_series": [(int(q), round(float(t - fail_ts), 3), bool(o),
                     probe.replies[int(q)][1] if o else None)
                    for q, t, o in zip(allseq[win], t_send[win], ok[win])],
    }


class Experiment:
    def __init__(self, api: str, lab: Lab):
        self.sc = load_scenario()
        self.lab = lab
        self.ctrl = Controller(api, self.sc, lab)
        self.link = self.sc["lab_link"]
        self.sub = self.link["subnet"]
        self.ends = self.link["ends"]

    # -- lab state ----------------------------------------------------------

    def restore(self):
        self.lab.parallel(lambda r: self.lab.restore(r, self.sub), self.ends)
        self.ctrl.reset()
        FAILED.labels(self.link["name"]).set(0)

    def healthy(self) -> bool:
        return all(self.lab.neighbor_full(r, self.sub)
                   and self.lab.route_dev(r, PEER_LOOPBACK[r])
                   == self.lab.iface_on(r, self.sub) for r in self.ends)

    def wait_healthy(self, timeout_s: float = 180.0) -> float:
        t0 = time.monotonic()
        while not self.healthy():
            if time.monotonic() - t0 > timeout_s:
                raise RuntimeError("lab did not reconverge on the r1-r3 link")
            time.sleep(1.0)
        return time.monotonic() - t0

    def set_timers(self, timers):
        hello, dead = timers if timers else (None, None)
        self.lab.parallel(lambda r: self.lab.set_timers(r, self.sub, hello, dead),
                          self.ends)

    # -- one trial ----------------------------------------------------------

    def trial(self, cond: str, mode: str, idx: int) -> Dict:
        kind = CONDITIONS[cond]["failure"]
        rp = self.sc["replay"]
        self.restore()
        self.wait_healthy()
        time.sleep(SETTLE_S)
        self.ctrl.mode = mode

        probe = Probe(self.lab, duration_s=rp["t_end"] - rp["t_start"] + 2)
        probe.start()
        time.sleep(0.3)                       # first probes out before t_start
        w0 = time.monotonic()
        fail = {}

        def world():
            sleep_until(w0 + (self.sc["break_t"] - rp["t_start"]))
            res = self.lab.parallel(
                lambda r: self.lab.fail(r, self.sub, kind, PEER_LOOPBACK[r]),
                self.ends)
            FAILED.labels(self.link["name"]).set(1)
            fail["ts"] = max(x["ts"] for x in res)
            fail["dev_before"] = {r: x["dev_before"] for r, x in zip(self.ends, res)}
            fail["link_iface"] = {r: x["iface"] for r, x in zip(self.ends, res)}

        th = threading.Thread(target=world, daemon=True)
        th.start()
        run_replay(self.ctrl, self.sc, w0)
        th.join()
        probe.join()

        res = analyse(probe, fail["ts"], self.ctrl.drained_at)
        moved = {r: fail["dev_before"][r] != fail["link_iface"][r]
                 for r in self.ends}
        lat = [e["latency_s"] for e in self.ctrl.log if "latency_s" in e]
        flags = [e["t"] for e in self.ctrl.log if e.get("flagged")]
        row = {"condition": cond, "failure": kind,
               "timers": "%d/%d" % CONDITIONS[cond]["timers"]
               if CONDITIONS[cond]["timers"] else "10/40 (default)",
               "mode": mode, "trial": idx,
               "first_flag_t": flags[0] if flags else None,
               "traffic_off_link_at_failure_r1": moved["r1"],
               "traffic_off_link_at_failure_r3": moved["r3"],
               "detect_calls": len(self.ctrl.log),
               "detect_errors": sum(1 for e in self.ctrl.log if "error" in e),
               "detect_latency_ms_mean": round(1000 * float(np.mean(lat)), 1) if lat else None,
               **{k: v for k, v in res.items() if not k.startswith("_")}}
        events = [{"condition": cond, "trial": idx, **e} for e in self.ctrl.log]
        series = [dict(condition=cond, mode=mode, trial=idx, seq=s, t_rel_s=t,
                       ok=int(o), ttl=ttl) for s, t, o, ttl in res["_series"]]

        T_LOSS.labels(mode, cond).set(res["loss_pct"] / 100.0)
        T_OUT.labels(mode, cond).set(res["outage_s"])
        if res["drain_lead_s"] is not None:
            T_LEAD.labels(mode, cond).set(res["drain_lead_s"])
        T_COUNT.labels(mode, cond).inc()
        raw = ["# %s %s trial %d  failure_ts=%.6f  drain_lead_s=%s" % (
            cond, mode, idx, fail["ts"], res["drain_lead_s"])] + probe.raw
        self.restore()
        return {"row": row, "events": events, "series": series, "raw": raw}


def summarise(rows: List[Dict]) -> List[Dict]:
    out = []
    for cond in CONDITIONS:
        for mode in MODES:
            rs = [r for r in rows if r["condition"] == cond and r["mode"] == mode]
            if not rs:
                continue
            loss = np.array([r["loss_pct"] for r in rs])
            loss_sent = np.array([r["loss_pct_sent"] for r in rs])
            outg = np.array([r["outage_s"] for r in rs])
            lead = [r["drain_lead_s"] for r in rs if r["drain_lead_s"] is not None]
            out.append({
                "condition": cond, "failure": rs[0]["failure"],
                "timers": rs[0]["timers"], "mode": mode, "trials": len(rs),
                "loss_pct_mean": round(float(loss.mean()), 2),
                "loss_pct_std": round(float(loss.std(ddof=1)) if len(rs) > 1 else 0.0, 2),
                "loss_pct_sent_mean": round(float(loss_sent.mean()), 2),
                "outage_s_mean": round(float(outg.mean()), 2),
                "outage_s_std": round(float(outg.std(ddof=1)) if len(rs) > 1 else 0.0, 2),
                "outage_s_min": round(float(outg.min()), 2),
                "outage_s_max": round(float(outg.max()), 2),
                "lost_probes_mean": round(float(np.mean([r["lost_in_window"] for r in rs])), 1),
                "drain_lead_s_mean": round(float(np.mean(lead)), 3) if lead else None,
                "off_link_before_failure": "%d/%d" % (
                    sum(r["traffic_off_link_at_failure_r1"]
                        and r["traffic_off_link_at_failure_r3"] for r in rs), len(rs)),
            })
    return out


def write_csv(path: str, rows: List[Dict], gz: bool = False):
    keys = list(dict.fromkeys(k for r in rows for k in r))
    op = gzip.open if gz else open
    with op(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", default="http://127.0.0.1:30080")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    ap.add_argument("--metrics-port", type=int, default=9101)
    ap.add_argument("--hold", type=int, default=0,
                    help="keep the metrics endpoint up this many seconds at the end")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    start_http_server(a.metrics_port)

    lab = Lab()
    ex = Experiment(a.api, lab)
    env = {"started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "api": a.api,
           "frr": lab.sh("r1", "vtysh -c 'show version' | head -1").strip(),
           "sat_link": ex.sc["sat_link"], "lab_link": ex.link,
           "first_flag_t": ex.sc["first_flag_t"], "break_t": ex.sc["break_t"],
           "probe": PROBE, "probe_interval_s": INTERVAL_S,
           "window_s": [-WIN_PRE_S, WIN_POST_S], "drain_cost": ex.ctrl.drain_cost,
           "ospf_timers_default": lab.ospf_timers("r1", ex.sub),
           "conditions": {}}
    rows, events, series, raw = [], [], [], []
    for cond in a.conditions.split(","):
        ex.restore()
        ex.set_timers(CONDITIONS[cond]["timers"])
        ex.wait_healthy()
        env["conditions"][cond] = {
            "failure": CONDITIONS[cond]["failure"],
            "ospf_timers_on_link": {r: lab.ospf_timers(r, ex.sub) for r in ex.ends}}
        for i in range(1, a.trials + 1):
            for mode in MODES:                # interleaved P, R, P, R, ...
                r = ex.trial(cond, mode, i)
                rows.append(r["row"])
                events += r["events"]
                series += r["series"]
                raw += r["raw"]
                x = r["row"]
                print(f"{cond:16s} {mode:9s} #{i}  loss {x['loss_pct']:6.2f}%  "
                      f"outage {x['outage_s']:6.2f}s (start {x['outage_start_rel_s']})  "
                      f"sent {x['probes_in_window']} (loss {x['loss_pct_sent']}%)  "
                      f"lead {x['drain_lead_s']}  "
                      f"off-link@fail r1={x['traffic_off_link_at_failure_r1']} "
                      f"r3={x['traffic_off_link_at_failure_r3']}", flush=True)
        ex.restore()
        ex.set_timers(None)
        ex.wait_healthy()

    env["finished"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    summ = summarise(rows)
    write_csv(os.path.join(a.out, "trials.csv"), rows)
    write_csv(os.path.join(a.out, "summary.csv"), summ)
    write_csv(os.path.join(a.out, "probes.csv.gz"), series, gz=True)
    write_csv(os.path.join(a.out, "events.csv.gz"), events, gz=True)
    with gzip.open(os.path.join(a.out, "ping_raw.txt.gz"), "wt") as f:
        f.write("\n".join(raw) + "\n")
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump({"environment": env, "summary": summ}, f, indent=1)
    for s in summ:
        print(s)
    try:
        from plot import plot
        plot(a.out)
    except Exception as e:                      # figure is optional
        print("plot skipped:", e)
    if a.hold:
        time.sleep(a.hold)


if __name__ == "__main__":
    main()
