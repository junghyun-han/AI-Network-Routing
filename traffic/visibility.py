"""
End-to-end traffic visibility.

Treats the network as an operator would on a dashboard: is the source
reachable from the destination right now, how many hops does end-to-end
traffic take, which links carry the most of it (bottlenecks), and how many
independent paths exist (resilience). All computed on the time-varying
topology with standard graph algorithms - no model, no training.

Usage:
    python visibility.py --stride 1 --out .

Outputs:
    figures/fig_latency.png
    figures/fig_bottleneck.png
    figures/fig_diversity.png
    README.md
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sim.env_v2 import ETRIEnvV2                       # noqa: E402
from sim.topology import SRC, DST, build_graph, with_terminals, e2e_path  # noqa: E402

HL = "#c0392b"
BL = "#2c7fb8"
GY = "#5b6b7a"


def collect(env: ETRIEnvV2, stride: int):
    times, hops, dists, mincut = [], [], [], []
    edge_load = Counter()
    connected = 0
    total = 0
    for t in range(0, env.p.horizon, stride):
        total += 1
        res = e2e_path(env, t, weight="dist")
        if res is None:
            times.append(t)
            hops.append(np.nan)
            dists.append(np.nan)
            mincut.append(0)
            continue
        connected += 1
        sats, dist, h = res
        times.append(t)
        hops.append(h)
        dists.append(dist)
        for k in range(len(sats) - 1):
            a, b = sorted((sats[k], sats[k + 1]))
            edge_load[(a, b)] += 1
        # resilience: edge-disjoint SRC->DST paths
        H = with_terminals(env, t, build_graph(env, t, weight="hop"))
        try:
            mincut.append(int(nx.edge_connectivity(H, SRC, DST)))
        except Exception:
            mincut.append(0)
    return (np.array(times), np.array(hops, dtype=float),
            np.array(dists, dtype=float), np.array(mincut),
            edge_load, connected, total)


def fig_latency(times, hops, out_dir):
    fig, ax = plt.subplots(figsize=(8.6, 4.4))
    ax.plot(times, hops, color=BL, lw=0.8)
    finite = hops[np.isfinite(hops)]
    if len(finite):
        ax.axhline(finite.mean(), color=HL, ls="--", lw=1.2,
                   label=f"mean {finite.mean():.2f} hops")
    ax.set_xlabel("time (s over the horizon)")
    ax.set_ylabel("end-to-end hops (shortest route)")
    ax.set_title("End-to-end latency over the horizon (operator view)")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    p = os.path.join(out_dir, "figures", "fig_latency.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def fig_bottleneck(edge_load, connected, out_dir, top=15):
    common = edge_load.most_common(top)
    labels = [f"{a}-{b}" for (a, b), _ in common]
    vals = [100.0 * c / max(1, connected) for _, c in common]
    fig, ax = plt.subplots(figsize=(8.2, 5.2))
    ax.barh(range(len(vals)), vals, color=GY)
    ax.set_yticks(range(len(vals)))
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel("share of connected time this link carries E2E traffic (%)")
    ax.set_title(f"Top {top} bottleneck links (most-used on shortest routes)")
    for i, v in enumerate(vals):
        ax.text(v + 0.3, i, f"{v:.1f}%", va="center", fontsize=8)
    ax.margins(x=0.12)
    fig.tight_layout()
    p = os.path.join(out_dir, "figures", "fig_bottleneck.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def fig_diversity(mincut, out_dir):
    m = mincut[mincut > 0]
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    if len(m):
        bins = np.arange(0.5, m.max() + 1.5, 1)
        ax.hist(m, bins=bins, color=BL, alpha=0.8, rwidth=0.85)
    ax.set_xlabel("edge-disjoint end-to-end paths (higher = more resilient)")
    ax.set_ylabel("number of time steps")
    ax.set_title("Path diversity across the horizon")
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    p = os.path.join(out_dir, "figures", "fig_diversity.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def write_report(stats, figs, out_dir):
    lines = []
    a = lines.append
    a("# End-to-End Traffic Visibility")
    a("")
    a("The network seen the way an operations center would see it: live "
      "reachability, end-to-end latency, which links are bottlenecks, and how "
      "much path redundancy there is - all on the time-varying topology.")
    a("")
    a(f"- Sampled `{stats['total']:,}` time steps; source and destination are "
      f"connected `{stats['connected']/stats['total']*100:.1f}%` of the time.")
    a(f"- End-to-end latency when connected: mean "
      f"**{stats['mean_hops']:.2f} hops** "
      f"(median {stats['median_hops']:.0f}, max {stats['max_hops']:.0f}), "
      f"mean path **{stats['mean_dist']:.0f} km**.")
    a("")
    a("## Latency over time")
    a("")
    a(f"![latency]({os.path.relpath(figs['lat'], out_dir)})")
    a("")
    a("## Bottleneck links")
    a("")
    a("Aggregating which links lie on the shortest end-to-end route reveals "
      "the ones carrying the most traffic - the first place to watch for "
      "congestion or to add capacity.")
    a("")
    a(f"- Busiest link carries end-to-end traffic **{stats['top_share']:.1f}%** "
      f"of the connected time.")
    a("")
    a(f"![bottleneck]({os.path.relpath(figs['bot'], out_dir)})")
    a("")
    a("## Resilience (path diversity)")
    a("")
    a("The number of edge-disjoint end-to-end paths is how many independent "
      "link failures the network can absorb before the source is cut off.")
    a("")
    a(f"- Median **{stats['median_cut']:.0f}** independent paths; a single "
      f"point of failure (only one path) exists "
      f"**{stats['spof_pct']:.1f}%** of the connected time.")
    a("")
    a(f"![diversity]({os.path.relpath(figs['div'], out_dir)})")
    a("")
    a("## Honest limits")
    a("")
    a("- Traffic is modeled as a single source-destination demand; real load "
      "would weight links by per-flow volume, which this method takes directly "
      "once flow data is available.")
    a("- Shortest path uses geometric distance as the latency proxy; swapping "
      "in measured link delay is a one-line change.")
    a("")
    a("---")
    a("")
    a("*Generated by `visibility.py`. Figures in `figures/`.*")
    p = os.path.join(out_dir, "README.md")
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--out", default=".")
    args = ap.parse_args()
    os.makedirs(os.path.join(args.out, "figures"), exist_ok=True)

    env = ETRIEnvV2()
    (times, hops, dists, mincut, edge_load,
     connected, total) = collect(env, args.stride)

    finite = hops[np.isfinite(hops)]
    dfin = dists[np.isfinite(dists)]
    m = mincut[mincut > 0]
    top_share = (100.0 * edge_load.most_common(1)[0][1] / max(1, connected)
                 if edge_load else 0.0)
    stats = dict(
        total=total, connected=connected,
        mean_hops=float(finite.mean()), median_hops=float(np.median(finite)),
        max_hops=float(finite.max()), mean_dist=float(dfin.mean()),
        median_cut=float(np.median(m)) if len(m) else 0.0,
        spof_pct=float((m == 1).mean() * 100) if len(m) else 0.0,
        top_share=top_share,
    )

    figs = {
        "lat": fig_latency(times, hops, args.out),
        "bot": fig_bottleneck(edge_load, connected, args.out),
        "div": fig_diversity(mincut, args.out),
    }
    report = write_report(stats, figs, args.out)

    print(f"connected {connected}/{total} "
          f"({connected/total*100:.1f}%)  mean {stats['mean_hops']:.2f} hops  "
          f"median cut {stats['median_cut']:.0f}  spof {stats['spof_pct']:.1f}%")
    print("wrote:", report)


if __name__ == "__main__":
    main()
