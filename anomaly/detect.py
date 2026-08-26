"""
Proactive link-failure anomaly detection.

Reframes the routing model's remaining-contact-time predictor as an
anomaly detector: "which currently-live links are about to drop within
tau seconds?" and measures how well it detects real failures.

Ground truth comes from the simulator itself: for every live link at
time t, we look forward and find the actual time until the link leaves
communication range. Two detectors are compared:

    * contact-time  (proposed) : the paper's one-slot linear extrapolation
                                  of remaining contact time - uses the
                                  direction of relative motion.
    * distance-only (baseline) : how close the link already is to the range
                                  edge - a static snapshot, no motion.

Spotting a link about to drop before it drops is the core of proactive
fault management; here that signal is measured against ground truth.

Usage:
    python detect.py --out .

Outputs:
    figures/fig_roc.png
    figures/fig_pr.png
    figures/fig_calibration.png
    README.md
"""

from __future__ import annotations

import argparse
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from sklearn.metrics import (average_precision_score, f1_score,
                             precision_recall_curve, precision_score,
                             recall_score, roc_auc_score, roc_curve)

# import the simulator physics
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sim.env_v2 import ETRIEnvV2  # noqa: E402

LOOKAHEAD_W = 30          # seconds to look ahead for a real failure
TAUS = [2, 5]             # "imminent" thresholds (s) to detect
CENSORED = LOOKAHEAD_W + 1  # actual-TTF for links that survive the window

PROPOSED = "Contact-time (proposed)"
BASELINE = "Distance-only (baseline)"

HL = "#c0392b"
BL = "#2c7fb8"
GY = "#5b6b7a"


def predicted_trem(env: ETRIEnvV2, t: int, R: float, cap: float) -> np.ndarray:
    """Vectorized copy of env._pred_remaining_contact_time over all pairs.
    Returns [N, N]; approaching links -> cap (won't fail soon)."""
    dt = float(env.p.time_step)
    D0 = env.pair_dist(t)
    D1 = env.pair_dist(t + 1)
    dd = D1 - D0
    with np.errstate(divide="ignore", invalid="ignore"):
        trem = np.where(dd <= 0, cap,
                        np.maximum(0.0, (R - D0) / np.where(dd <= 0, 1.0, dd)) * dt)
    trem = np.minimum(trem, cap)
    trem = np.where(D0 >= R, 0.0, trem)
    return trem


def actual_ttf(env: ETRIEnvV2, t: int, active: np.ndarray, R: float) -> np.ndarray:
    """Actual time-to-failure (s) for each active pair at time t, by looking
    forward in the real simulation. CENSORED if it survives the window."""
    ttf = np.full(active.shape, CENSORED, dtype=np.int32)
    for k in range(1, LOOKAHEAD_W + 1):
        Dk = env.pair_dist(t + k)
        newly = active & (Dk > R) & (ttf == CENSORED)
        ttf[newly] = k
    return ttf


def build_dataset(env: ETRIEnvV2):
    """Returns per-live-link arrays: predicted contact time, normalized
    current distance (baseline signal), and actual time-to-failure."""
    R = float(env.p.link_range_km)
    cap = float(env.p.TREM_CAP_S)
    N = int(env.N)
    iu, ju = np.triu_indices(N, k=1)

    preds, dists, ttfs = [], [], []
    for t in range(env.p.horizon):
        D0 = env.pair_dist(t)
        active = (D0 <= R)
        pred = predicted_trem(env, t, R, cap)
        ttf = actual_ttf(env, t, active, R)
        m = active[iu, ju]
        preds.append(pred[iu, ju][m])
        dists.append((D0[iu, ju][m]) / R)
        ttfs.append(ttf[iu, ju][m])
    return (np.concatenate(preds),
            np.concatenate(dists),
            np.concatenate(ttfs).astype(float))


def detector_scores(pred, dist, cap):
    """Higher score = more imminent, for each detector."""
    return {
        PROPOSED: cap - np.minimum(pred, cap),  # short contact time -> high
        BASELINE: dist,                         # near range edge -> high
    }


def evaluate(pred, dist, ttf):
    cap = float(ETRIEnvV2().p.TREM_CAP_S)
    scores = detector_scores(pred, dist, cap)
    rows = {}
    for tau in TAUS:
        y = (ttf <= tau).astype(int)
        rows[tau] = {"base_rate": float(y.mean()), "n_pos": int(y.sum())}
        for name, s in scores.items():
            rows[tau][name] = {
                "roc_auc": float(roc_auc_score(y, s)) if 0 < y.mean() < 1 else float("nan"),
                "ap": float(average_precision_score(y, s)) if y.any() else float("nan"),
            }
        # thresholded operating point for the proposed detector:
        # flag if predicted contact time <= tau (the router's own rule)
        yhat = (pred <= tau).astype(int)
        rows[tau]["op"] = {
            "precision": float(precision_score(y, yhat, zero_division=0)),
            "recall": float(recall_score(y, yhat, zero_division=0)),
            "f1": float(f1_score(y, yhat, zero_division=0)),
        }
    return rows, scores


def fig_roc(ttf, scores, out_dir):
    """ROC for both detectors at the tightest horizon (tau=2s)."""
    tau = TAUS[0]
    y = (ttf <= tau).astype(int)
    fig, ax = plt.subplots(figsize=(6.4, 5.4))
    for name, c in [(PROPOSED, HL), (BASELINE, GY)]:
        fpr, tpr, _ = roc_curve(y, scores[name])
        a = roc_auc_score(y, scores[name])
        ax.plot(fpr, tpr, color=c, lw=2, label=f"{name}  (AUC={a:.3f})")
    ax.plot([0, 1], [0, 1], "--", color="#aaa", lw=1)
    ax.set_xlabel("False positive rate")
    ax.set_ylabel("True positive rate")
    ax.set_title(f"Detecting links that fail within {tau}s - ROC")
    ax.legend(loc="lower right")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    p = os.path.join(out_dir, "figures", "fig_roc.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def fig_pr(ttf, scores, out_dir):
    tau = TAUS[0]
    y = (ttf <= tau).astype(int)
    fig, ax = plt.subplots(figsize=(6.4, 5.4))
    for name, c in [(PROPOSED, HL), (BASELINE, GY)]:
        prec, rec, _ = precision_recall_curve(y, scores[name])
        ap = average_precision_score(y, scores[name])
        ax.plot(rec, prec, color=c, lw=2, label=f"{name}  (AP={ap:.3f})")
    ax.axhline(y.mean(), color="#aaa", ls=":", lw=1, label="base rate")
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_title(f"Detecting links that fail within {tau}s - Precision/Recall")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    p = os.path.join(out_dir, "figures", "fig_pr.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def fig_calibration(pred, ttf, out_dir):
    m = ttf <= LOOKAHEAD_W
    x, y = pred[m], ttf[m]
    mae = float(np.mean(np.abs(np.minimum(x, LOOKAHEAD_W) - y)))
    rng = np.arange(len(x))
    if len(x) > 6000:
        rng = rng[::len(x) // 6000]
    fig, ax = plt.subplots(figsize=(6.2, 5.4))
    ax.scatter(x[rng], y[rng], s=6, alpha=0.15, color=BL, edgecolor="none")
    ax.plot([0, LOOKAHEAD_W], [0, LOOKAHEAD_W], "--", color=HL, lw=1.5,
            label="perfect (y=x)")
    ax.set_xlim(0, 21)
    ax.set_ylim(0, LOOKAHEAD_W + 1)
    ax.set_xlabel("Predicted remaining contact time (s, capped 20)")
    ax.set_ylabel("Actual time to failure (s)")
    ax.set_title(f"Predictor calibration on failing links\nMAE = {mae:.2f} s")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    p = os.path.join(out_dir, "figures", "fig_calibration.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p, mae


def write_report(rows, pred, ttf, mae, figs, out_dir):
    n = len(pred)
    frac_fail_w = float((ttf <= LOOKAHEAD_W).mean())
    L, out = [], None
    lines = []
    w = lines.append
    w("# Proactive Link-Failure Anomaly Detection")
    w("")
    w("Treats the routing model's remaining-contact-time predictor as an "
      "anomaly detector for imminent link loss, and measures how well it "
      "flags real failures before they happen - benchmarked against a "
      "distance-only baseline.")
    w("")
    w(f"- Samples: `{n:,}` live links across the full horizon (every second, "
      f"all in-range satellite pairs).")
    w(f"- Look-ahead window: `{LOOKAHEAD_W}` s; `{frac_fail_w*100:.1f}%` of "
      f"live links fail within it.")
    w("- Ground truth is the link's actual future; predictors use only "
      "information available at decision time.")
    w("")
    w("> Framing: spotting a link about to drop *before* it drops is the core "
      "of proactive fault management in any dynamic network. Here the link is "
      "a satellite ISL, but the detection problem is the one an NOC watches "
      "for.")
    w("")
    w("## Detector comparison")
    w("")
    w("Ranking quality (threshold-independent): higher is better.")
    w("")
    w("| Imminent horizon | Base rate | Detector | ROC-AUC | Avg precision |")
    w("|---|---|---|---|---|")
    for tau in TAUS:
        r = rows[tau]
        for name in (PROPOSED, BASELINE):
            w(f"| fails within {tau}s | {r['base_rate']*100:.1f}% | {name} | "
              f"{r[name]['roc_auc']:.3f} | {r[name]['ap']:.3f} |")
    w("")
    # headline delta
    d2 = rows[2][PROPOSED]["roc_auc"] - rows[2][BASELINE]["roc_auc"]
    w(f"The contact-time detector uses the *direction* of relative motion, so "
      f"it separates approaching from receding links near the range edge - "
      f"worth **+{d2:.3f} ROC-AUC** over the distance-only snapshot at the 2s "
      f"horizon.")
    w("")
    w(f"![roc]({os.path.relpath(figs['roc'], out_dir)})")
    w("")
    w(f"![pr]({os.path.relpath(figs['pr'], out_dir)})")
    w("")
    w("## Operating point")
    w("")
    w("At the router's own rule - *flag if predicted contact time <= horizon* "
      "- the proposed detector reaches:")
    w("")
    w("| Imminent horizon | Precision | Recall | F1 |")
    w("|---|---|---|---|")
    for tau in TAUS:
        op = rows[tau]["op"]
        w(f"| fails within {tau}s | {op['precision']*100:.1f}% | "
          f"{op['recall']*100:.1f}% | {op['f1']:.3f} |")
    w("")
    w("## Predictor calibration")
    w("")
    w(f"On links that actually fail within the window, predicted remaining "
      f"life tracks real time-to-failure with **MAE = {mae:.2f} s**.")
    w("")
    w(f"![calibration]({os.path.relpath(figs['calib'], out_dir)})")
    w("")
    w("## Honest limits")
    w("")
    w("- One-slot linear extrapolation is most accurate for near-term "
      "failures and degrades with the horizon - which is the regime proactive "
      "routing actually needs.")
    w("- Recall is high but precision is moderate at the tight threshold: the "
      "detector errs toward flagging, which for routing means caution, not a "
      "missed failure.")
    w("- Ground truth is the simulator, not measured telemetry; the "
      "contribution is the detection *method*, portable to real link metrics.")
    w("")
    w("---")
    w("")
    w("*Generated by `detect.py`. Figures in `figures/`.*")
    p = os.path.join(out_dir, "README.md")
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    args = ap.parse_args()
    os.makedirs(os.path.join(args.out, "figures"), exist_ok=True)

    env = ETRIEnvV2()
    pred, dist, ttf = build_dataset(env)
    rows, scores = evaluate(pred, dist, ttf)

    figs = {}
    figs["roc"] = fig_roc(ttf, scores, args.out)
    figs["pr"] = fig_pr(ttf, scores, args.out)
    figs["calib"], mae = fig_calibration(pred, ttf, args.out)
    report = write_report(rows, pred, ttf, mae, figs, args.out)

    print(f"samples: {len(pred):,}")
    for tau in TAUS:
        r = rows[tau]
        print(f"  tau<={tau}s base={r['base_rate']*100:.1f}%  "
              f"proposed AUC={r[PROPOSED]['roc_auc']:.3f}  "
              f"baseline AUC={r[BASELINE]['roc_auc']:.3f}  "
              f"P={r['op']['precision']*100:.1f}% R={r['op']['recall']*100:.1f}%")
    print("calibration MAE:", round(mae, 3), "s")
    print("wrote:", report)


if __name__ == "__main__":
    main()
