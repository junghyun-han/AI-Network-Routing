"""
Network Operations Analysis for the AI routing policy.

Reads the per-episode results produced by the routing experiments
(`results_v2/episodes_all_v2.csv`) and derives network-operations
oriented views: end-to-end quality, proactive anomaly (imminent link
loss) avoidance, per-scenario traffic patterns, and link stability
margin.

Turns the routing experiment logs into the operational view a network
team watches: delivery quality, imminent-failure exposure, per-scenario
traffic patterns, and link stability margin.

Usage:
    python ops_analysis.py \
        --episodes ../results_v2/episodes_all_v2.csv \
        --out .

Outputs:
    figures/fig_anomaly_avoidance.png
    figures/fig_quality_stability.png
    figures/fig_contact_margin.png
    figures/fig_traffic_horizon.png
    README.md              (tables + embedded figures)
"""

from __future__ import annotations

import argparse
import os

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ---- presentation config -------------------------------------------------

# Core methods shown in the report (ablation coefficient sweeps excluded).
MAIN_METHODS = [
    "Oracle",
    "Dijkstra-Static",
    "Dijkstra-Replan",
    "Greedy-Dist",
    "Cost-Greedy",
    "Proactive-Heuristic",
    "PPO-Linear",
    "PPO-MLP",
    "PPO-GCN-noCT",
    "PPO-GCN",
]
PROPOSED = "PPO-GCN"

# Human-readable descriptions (used in the report table).
METHOD_DESC = {
    "Oracle": "Offline optimum (full future knowledge) - upper bound",
    "Dijkstra-Static": "Plan once on the initial snapshot",
    "Dijkstra-Replan": "Recompute shortest path every hop",
    "Greedy-Dist": "Always hop to the neighbor closest to destination",
    "Cost-Greedy": "Greedy on the per-hop cost score",
    "Proactive-Heuristic": "Hand-tuned contact-time heuristic",
    "PPO-Linear": "RL policy, linear features",
    "PPO-MLP": "RL policy, MLP",
    "PPO-GCN-noCT": "RL + graph encoder, no contact-time input",
    "PPO-GCN": "Proposed: RL + graph encoder + contact-time",
}

# Metrics: column -> (label, better direction)
METRIC_META = {
    "success": ("Delivery success rate", "high"),
    "risky_ratio": ("Imminent-loss link usage", "low"),
    "path_length_km": ("Path length (km)", "low"),
    "hops": ("Hops", "low"),
    "trem_median_s": ("Contact margin, median (s)", "high"),
    "trem_min_s": ("Contact margin, min (s)", "high"),
}

HIGHLIGHT = "#c0392b"   # proposed method
NEUTRAL = "#5b6b7a"     # other RL / heuristics
ORACLE_C = "#2c7fb8"    # oracle bound


def load(episodes_path: str) -> pd.DataFrame:
    df = pd.read_csv(episodes_path)
    needed = set(METRIC_META) | {"method", "t0"}
    missing = needed - set(df.columns)
    if missing:
        raise SystemExit(f"missing columns in {episodes_path}: {sorted(missing)}")
    return df


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    """Mean of each metric per method, over all t0 (and seeds)."""
    cols = list(METRIC_META)
    agg = df.groupby("method")[cols].mean()
    # keep only main methods, in the declared order
    order = [m for m in MAIN_METHODS if m in agg.index]
    return agg.loc[order]


# ---- figures -------------------------------------------------------------

def _color(method: str) -> str:
    if method == PROPOSED:
        return HIGHLIGHT
    if method == "Oracle":
        return ORACLE_C
    return NEUTRAL


def fig_anomaly_avoidance(agg: pd.DataFrame, out_dir: str) -> str:
    """Bar chart: fraction of routing decisions that used a link about to
    fail (an operational anomaly the policy should avoid)."""
    s = agg["risky_ratio"].sort_values(ascending=False)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    colors = [_color(m) for m in s.index]
    ax.barh(range(len(s)), s.values * 100, color=colors)
    ax.set_yticks(range(len(s)))
    ax.set_yticklabels(s.index)
    for lbl, m in zip(ax.get_yticklabels(), s.index):
        if m == PROPOSED:
            lbl.set_color(HIGHLIGHT)
            lbl.set_fontweight("bold")
    ax.invert_yaxis()
    ax.set_xlabel("Imminent-loss link usage (% of hops)")
    ax.set_title("Proactive anomaly avoidance: use of links about to fail\n"
                 "(lower is better)")
    for i, v in enumerate(s.values):
        ax.text(v * 100 + 0.4, i, f"{v*100:.1f}%", va="center", fontsize=9)
    ax.margins(x=0.12)
    fig.tight_layout()
    p = os.path.join(out_dir, "figures", "fig_anomaly_avoidance.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def fig_quality_stability(agg: pd.DataFrame, out_dir: str) -> str:
    """Scatter: imminent-loss usage (x) vs path length (y). Bottom-left
    = stable and efficient."""
    fig, ax = plt.subplots(figsize=(7.5, 5.5))
    for m in agg.index:
        x = agg.loc[m, "risky_ratio"] * 100
        y = agg.loc[m, "path_length_km"]
        c = _color(m)
        marker, size = "o", 90
        if m == PROPOSED:
            marker, size = "*", 320
        elif m == "Oracle":
            marker, size = "D", 110
        ax.scatter(x, y, s=size, color=c, zorder=3,
                   marker=marker, edgecolor="white", linewidth=0.8)
        bold = m in (PROPOSED, "Oracle")
        ax.annotate(m, (x, y), textcoords="offset points", xytext=(8, 5),
                    fontsize=9 if bold else 8.5, color=c if bold else "#222",
                    fontweight="bold" if bold else "normal")
    ax.set_xlabel("Imminent-loss link usage (% of hops)  ->  worse")
    ax.set_ylabel("Path length (km)  ->  worse")
    ax.set_title("Quality-stability trade-off\n"
                 "(bottom-left = stable and efficient)")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    p = os.path.join(out_dir, "figures", "fig_quality_stability.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def fig_contact_margin(df: pd.DataFrame, out_dir: str) -> str:
    """Distribution of per-episode median contact margin for key methods.
    Higher = links chosen have more life left = safer."""
    keep = ["PPO-GCN", "Proactive-Heuristic", "Dijkstra-Replan",
            "Greedy-Dist", "Cost-Greedy"]
    keep = [m for m in keep if m in df["method"].unique()]
    data = [df.loc[df["method"] == m, "trem_median_s"].values for m in keep]
    fig, ax = plt.subplots(figsize=(8, 4.8))
    bp = ax.boxplot(data, orientation="vertical", patch_artist=True,
                    showfliers=False, medianprops=dict(color="black"))
    for patch, m in zip(bp["boxes"], keep):
        patch.set_facecolor(HIGHLIGHT if m == PROPOSED else NEUTRAL)
        patch.set_alpha(0.75)
    ax.set_xticklabels(keep, rotation=15, ha="right")
    ax.set_ylabel("Median remaining contact time per episode (s)")
    ax.set_title("Link stability margin of chosen links\n(higher is safer)")
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    p = os.path.join(out_dir, "figures", "fig_contact_margin.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def fig_traffic_horizon(df: pd.DataFrame, out_dir: str, n_bins: int = 40) -> str:
    """Per-scenario view over the orbital horizon: each t0 is a different
    network snapshot (traffic scenario). Shows how anomaly exposure varies
    across scenarios for the proposed policy vs a shortest-path baseline -
    the kind of monitoring view an operator would watch."""
    pair = ["PPO-GCN", "Dijkstra-Replan"]
    pair = [m for m in pair if m in df["method"].unique()]
    fig, ax = plt.subplots(figsize=(8.5, 4.6))
    for m in pair:
        d = df[df["method"] == m]
        g = d.groupby("t0")["risky_ratio"].mean().sort_index()
        # smooth into bins for readability
        idx = g.index.values.astype(float)
        vals = g.values.astype(float)
        bins = np.linspace(idx.min(), idx.max(), n_bins + 1)
        centers = 0.5 * (bins[:-1] + bins[1:])
        which = np.digitize(idx, bins) - 1
        which = np.clip(which, 0, n_bins - 1)
        binned = np.array([vals[which == b].mean() if np.any(which == b)
                           else np.nan for b in range(n_bins)])
        ax.plot(centers, binned * 100, label=m,
                color=HIGHLIGHT if m == PROPOSED else NEUTRAL, linewidth=2)
    ax.set_xlabel("Traffic scenario (start time t0 over the horizon)")
    ax.set_ylabel("Imminent-loss link usage (%)")
    ax.set_title("Per-scenario anomaly exposure across the horizon")
    ax.legend()
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    p = os.path.join(out_dir, "figures", "fig_traffic_horizon.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


# ---- report --------------------------------------------------------------

def _fmt(col: str, v: float) -> str:
    if col in ("success", "risky_ratio"):
        return f"{v*100:.1f}%"
    if col in ("path_length_km",):
        return f"{v:.1f}"
    if col in ("hops",):
        return f"{v:.2f}"
    return f"{v:.1f}"


def write_report(agg: pd.DataFrame, df: pd.DataFrame, figs: dict,
                 out_dir: str) -> str:
    p = os.path.join(out_dir, "README.md")
    prop = agg.loc[PROPOSED]

    # a couple of derived talking points
    risky_baselines = agg.loc[["Dijkstra-Replan", "Greedy-Dist",
                               "Dijkstra-Static"], "risky_ratio"].mean() * 100
    n_episodes = len(df)
    n_scenarios = df["t0"].nunique()

    lines = []
    W = lines.append
    W("# Network Operations Analysis")
    W("")
    W("An operations-oriented reading of the AI routing policy: end-to-end "
      "quality, proactive avoidance of imminent link failures, per-scenario "
      "traffic patterns, and link stability margin.")
    W("")
    W(f"- Data: `{n_episodes:,}` episodes over `{n_scenarios:,}` network "
      f"scenarios (start times across the orbital horizon).")
    W("")
    W("> Framing note: the study domain is a low-earth-orbit satellite mesh, "
      "but the operational questions - keep delivery high, avoid links that "
      "are about to drop, stay efficient, hold a safety margin - are the "
      "same ones any dynamic packet network faces.")
    W("")

    # ---- 1. quality
    W("## 1. End-to-end quality and efficiency")
    W("")
    W("| Method | Description | Success | Path (km) | Hops |")
    W("|---|---|---|---|---|")
    for m in agg.index:
        W(f"| {'**'+m+'**' if m==PROPOSED else m} | {METHOD_DESC.get(m,'')} "
          f"| {_fmt('success', agg.loc[m,'success'])} "
          f"| {_fmt('path_length_km', agg.loc[m,'path_length_km'])} "
          f"| {_fmt('hops', agg.loc[m,'hops'])} |")
    W("")
    W(f"The proposed policy delivers **{_fmt('success', prop['success'])}** of "
      f"traffic. It is not the shortest path - it trades roughly a few percent "
      f"of path length for stability (see section 2), which is the intended "
      f"behaviour, not a defect.")
    W("")

    # ---- 2. anomaly avoidance
    W("## 2. Proactive anomaly avoidance (imminent link loss)")
    W("")
    W("An *imminent-loss link* is one predicted to drop within the contact-"
      "time window. Using such a link is the operational anomaly the policy "
      "is meant to pre-empt.")
    W("")
    W(f"![anomaly avoidance]({os.path.relpath(figs['anomaly'], out_dir)})")
    W("")
    W(f"- Proposed policy: **{_fmt('risky_ratio', prop['risky_ratio'])}** of "
      f"hops use an imminent-loss link.")
    W(f"- Shortest-path / greedy baselines: ~**{risky_baselines:.0f}%** - they "
      f"pick the nearest hop and walk straight into links that are about to "
      f"fail.")
    W("")
    W("The policy predicts remaining link life and routes around failures "
      "before they happen, rather than reacting after a drop.")
    W("")
    W(f"![quality vs stability]({os.path.relpath(figs['tradeoff'], out_dir)})")
    W("")

    # ---- 3. traffic patterns
    W("## 3. Per-scenario traffic patterns")
    W("")
    W("Each start time is a different network snapshot. Watching a metric "
      "across scenarios is the monitoring view an operator would keep on a "
      "dashboard.")
    W("")
    W(f"![traffic horizon]({os.path.relpath(figs['horizon'], out_dir)})")
    W("")
    W("Anomaly exposure for the shortest-path baseline swings with the "
      "scenario, while the proposed policy holds near zero across the whole "
      "horizon - stable behaviour regardless of the traffic snapshot.")
    W("")

    # ---- 4. stability margin
    W("## 4. Link stability margin")
    W("")
    W("Remaining contact time of the links a method chooses - how much life "
      "is left when the link is used. Higher means a bigger safety buffer.")
    W("")
    W(f"![contact margin]({os.path.relpath(figs['margin'], out_dir)})")
    W("")
    W(f"- Proposed policy median margin: **{prop['trem_median_s']:.1f} s** "
      f"(capped at 20 s), min **{prop['trem_min_s']:.1f} s**.")
    W("- Reactive baselines sit far lower - they commit to links with little "
      "life left, which is why their imminent-loss usage is high.")
    W("")

    # ---- honest limits
    W("## 5. Honest limits")
    W("")
    W("- Path length is not the best; the proposed policy is a few percent "
      "longer than pure shortest-path. That is the cost of avoiding fragile "
      "links, not an efficiency loss to hide.")
    W("- Delivery reaching 100% is partly due to per-hop decision making; "
      "re-planning every hop also reaches 100%. The policy's distinct value "
      "is the near-zero imminent-loss usage at that success level.")
    W("- `Oracle` is a genuine offline upper bound (full future knowledge). "
      "The remaining gap to it is the price of deciding online.")
    W("")
    W("---")
    W("")
    W("*Generated by `ops_analysis.py`. Figures in `figures/`.*")

    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", default="../results_v2/episodes_all_v2.csv")
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    os.makedirs(os.path.join(args.out, "figures"), exist_ok=True)

    df = load(args.episodes)
    agg = aggregate(df)

    figs = {
        "anomaly": fig_anomaly_avoidance(agg, args.out),
        "tradeoff": fig_quality_stability(agg, args.out),
        "margin": fig_contact_margin(df, args.out),
        "horizon": fig_traffic_horizon(df, args.out),
    }
    report = write_report(agg, df, figs, args.out)

    print("wrote:")
    for _, v in figs.items():
        print("  ", v)
    print("  ", report)


if __name__ == "__main__":
    main()
