"""
Figure for the closed-loop experiment: every trial as one lane, time
relative to the link failure. Thick segments = outage (from the last
answered probe to the next one, with lost probes in between); the thin line
shows which path the replies took (TTL 64 = direct r1-r3, TTL 63 = via r2).

    python closedloop/plot.py [results_dir]
"""

from __future__ import annotations

import csv
import gzip
import os
import sys
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
MODE_COLOR = {"proactive": "#2a78d6", "reactive": "#eb6834"}
TITLES = {
    "silent-default": "Silent failure (packets vanish), OSPF default timers hello 10 s / dead 40 s",
    "silent-fast": "Silent failure, tuned OSPF timers hello 1 s / dead 4 s",
    "carrier-default": "Carrier-down failure (interface down), OSPF default timers",
}


def _read(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as f:
        return list(csv.DictReader(f))


def _runs(ts, flags):
    """[(start, end)] of consecutive True flags."""
    out, start, prev = [], None, None
    for t, f in zip(ts, flags):
        if f and start is None:
            start = t
        if not f and start is not None:
            out.append((start, prev))
            start = None
        prev = t
    if start is not None:
        out.append((start, prev))
    return out


def _outages(ts, ok):
    """[(last answer, next answer)] around every run of lost probes - the
    same definition as the outage metric (ping sends few probes while the
    dead neighbour's ARP is unresolved, so the lost probes alone would
    under-draw the gap)."""
    out, last_ok, lost = [], None, False
    for t, o in zip(ts, ok):
        if o:
            if lost:
                out.append((last_ok if last_ok is not None else ts[0], t))
            last_ok, lost = t, False
        else:
            lost = True
    if lost:
        out.append((last_ok if last_ok is not None else ts[0], ts[-1] + 0.1))
    return out


def plot(results_dir: str) -> str:
    probes = _read(os.path.join(results_dir, "probes.csv.gz"))
    trials = _read(os.path.join(results_dir, "trials.csv"))
    summ = {(r["condition"], r["mode"]): r
            for r in _read(os.path.join(results_dir, "summary.csv"))}
    lanes = defaultdict(list)
    for p in probes:
        lanes[(p["condition"], p["mode"], int(p["trial"]))].append(p)
    conds = [c for c in TITLES if any(k[0] == c for k in lanes)]
    lead = {(t["condition"], t["mode"], int(t["trial"])): t["drain_lead_s"]
            for t in trials}

    plt.rcParams.update({"font.family": "sans-serif", "font.size": 9,
                         "axes.edgecolor": GRID, "axes.labelcolor": INK2,
                         "xtick.color": MUTED, "ytick.color": INK2})
    legend_in = 0.8                           # space reserved for the legend
    fig_h = 2.9 * len(conds) + legend_in
    fig, axes = plt.subplots(len(conds), 1, figsize=(10, fig_h),
                             sharex=True, squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    for ax, cond in zip(axes[:, 0], conds):
        ax.set_facecolor(SURFACE)
        keys = sorted([k for k in lanes if k[0] == cond],
                      key=lambda k: (k[1] != "proactive", k[2]))
        yt, yl = [], []
        for y, key in enumerate(reversed(keys)):
            _, mode, trial = key
            rows = sorted(lanes[key], key=lambda r: float(r["t_rel_s"]))
            ts = [float(r["t_rel_s"]) for r in rows]
            ok = [r["ok"] == "1" for r in rows]
            via_r2 = [r["ok"] == "1" and r["ttl"] == "63" for r in rows]
            direct = [r["ok"] == "1" and r["ttl"] == "64" for r in rows]
            for a, b in _runs(ts, direct):
                ax.plot([a, b + 0.1], [y, y], color=MUTED, lw=1, solid_capstyle="butt")
            for a, b in _runs(ts, via_r2):
                ax.plot([a, b + 0.1], [y, y], color=INK, lw=2, solid_capstyle="butt")
            for a, b in _outages(ts, ok):
                ax.plot([a, b], [y, y], color=MODE_COLOR[mode], lw=7,
                        solid_capstyle="butt")
            ld = lead.get(key)
            if ld not in (None, ""):
                ax.plot([-float(ld)], [y], marker="v", ms=6, color=MODE_COLOR[mode],
                        mec=SURFACE, mew=1.5)
            yt.append(y)
            yl.append(f"{'P' if mode == 'proactive' else 'R'}{trial}")
        ax.set_yticks(yt)
        ax.set_yticklabels(yl)
        ax.set_ylim(-0.8, len(keys) - 0.2)
        ax.axvline(0, color=INK2, lw=1)
        ax.text(0.3, len(keys) - 0.45, "link fails", color=INK2, fontsize=8, va="top")
        ax.grid(axis="x", color=GRID, lw=1)
        ax.set_axisbelow(True)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.tick_params(axis="y", length=0)
        p, r = summ.get((cond, "proactive")), summ.get((cond, "reactive"))
        sub = ""
        if p and r:
            sub = (f"\nproactive: outage {float(p['outage_s_mean']):.2f} s, loss "
                   f"{float(p['loss_pct_mean']):.2f}%   |   reactive: outage "
                   f"{float(r['outage_s_mean']):.2f} s, loss {float(r['loss_pct_mean']):.2f}%"
                   f"   (mean of {r['trials']} trials, 60 s window)")
        ax.set_title(TITLES[cond] + sub, loc="left", color=INK, fontsize=9.5)
    axes[-1, 0].set_xlabel("time relative to the link failure (s)")
    axes[-1, 0].set_xlim(-10, 50)
    handles = [
        Line2D([], [], color=MODE_COLOR["proactive"], lw=7,
               label="outage, proactive (P: AI detect -> OSPF drain): none"),
        Line2D([], [], color=MODE_COLOR["reactive"], lw=7,
               label="outage, reactive (R: OSPF on its own)"),
        Line2D([], [], color=MUTED, lw=1, label="replies via direct r1-r3 link (TTL 64)"),
        Line2D([], [], color=INK, lw=2, label="replies via r2 (TTL 63)"),
        Line2D([], [], color=MODE_COLOR["proactive"], marker="v", ls="none", ms=6,
               label="controller drain applied"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False,
               fontsize=8.5, bbox_to_anchor=(0.5, 0.0))
    fig.tight_layout(rect=(0, legend_in / fig_h, 1, 1))
    out = os.path.join(results_dir, "fig_closedloop.png")
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return out


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    print(plot(sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "results")))
