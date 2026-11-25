#!/usr/bin/env python3
"""Wissenschaftlicher Diagramm-Generator (PDF & PNG) fuer das GitOps-Messframework."""

import argparse
import glob
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

plt.rcParams.update({
    "font.family": "serif",
    "font.size": 10,
    "axes.labelsize": 11,
    "axes.titlesize": 12,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.fontsize": 8.5,
    "lines.linewidth": 1.5,
    "grid.alpha": 0.4,
    "grid.linestyle": "--",
})


def save_fig(out_dir: str, stem: str) -> None:
    plt.tight_layout()
    for ext in ("pdf", "png"):
        plt.savefig(os.path.join(out_dir, f"{stem}.{ext}"), dpi=300)
    plt.close()


def phase_intervals(df: pd.DataFrame, target: str) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    if "experiment_phase" not in df.columns or df.empty:
        return []
    intervals, start, last_ts = [], None, None
    for ts, active in zip(df.index, df["experiment_phase"] == target):
        if active and start is None:
            start = ts
        elif not active and start is not None and last_ts is not None:
            intervals.append((start, last_ts))
            start = None
        last_ts = ts
    if start is not None and last_ts is not None:
        intervals.append((start, last_ts))
    return intervals


def shade_phases(ax, df: pd.DataFrame, with_labels: bool = False) -> None:
    for phase, color, alpha, lbl in [("fault", "#e41a1c", 0.11, "Störungsfenster"), ("recovery", "#ff7f00", 0.13, "Erholungsphase")]:
        for idx, (t0, t1) in enumerate(phase_intervals(df, phase)):
            ax.axvspan(t0, t1, color=color, alpha=alpha, label=(lbl if with_labels and idx == 0 else None))


def draw_box(ax, groups: List[np.ndarray], labels: List[str], colors: List[str]) -> None:
    try:
        bp = ax.boxplot(groups, tick_labels=labels, patch_artist=True, showfliers=False)
    except TypeError:
        bp = ax.boxplot(groups, labels=labels, patch_artist=True, showfliers=False)
    for patch, col in zip(bp["boxes"], colors):
        patch.set_facecolor(col)
        patch.set_alpha(0.6)


def add_headroom(ax, factor: float = 1.28, min_top: float = 0.5, pad_bottom: bool = False) -> None:
    ys: List[float] = []
    for line in ax.get_lines():
        ydata = np.asarray(line.get_ydata(), dtype=float)
        finite = ydata[np.isfinite(ydata)]
        if finite.size:
            ys.extend([float(np.min(finite)), float(np.max(finite))])
    ymax = max(ys) if ys else 0.0
    top = max(min_top, ymax * factor)
    bottom = -0.04 * top if pad_bottom or (ys and min(ys) <= 0.0) else 0.0
    ax.set_ylim(bottom, top)


def strip_box(ax, specs: List[Tuple[str, List[Optional[float]], str]], title: str, ylabel: str) -> None:
    valid = [(lbl, [float(v) for v in vals if v is not None], col) for lbl, vals, col in specs]
    valid = [(lbl, vals, col) for lbl, vals, col in valid if vals]
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(True)
    if not valid:
        ax.text(0.5, 0.5, "Keine Daten", ha="center", va="center", transform=ax.transAxes)
        return
    draw_box(ax, [np.array(v) for _, v, _ in valid], [lbl for lbl, _, _ in valid], [col for _, _, col in valid])
    all_vals = [v for _, vals, _ in valid for v in vals]
    for idx, (_, vals, _) in enumerate(valid, start=1):
        offsets = np.linspace(-0.07, 0.07, len(vals)) if len(vals) > 1 else np.array([0.0])
        ax.scatter(float(idx) + offsets, vals, color="#222222", zorder=5, s=28, alpha=0.85)
    global_min, global_max = min(all_vals), max(all_vals)
    top = max(0.5, global_max * 1.18)
    if global_min >= 0.0:
        ax.set_ylim(-0.04 * top, top)


def plot_latency_timeseries(df: pd.DataFrame, out_dir: str, prefix: str) -> None:
    """Plot 1: Zeitverlauf der Reconcile- und Git-Request-Latenzen mit echten NaN-Luecken."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.5, 5.6), sharex=True)

    shade_phases(ax1, df, with_labels=True)
    for col, lbl, col_hex, ls in [
        ("reconcile_duration_p95_sec", "Reconcile p95 (1m-Fenster)", "#d95f02", "-"),
        ("reconcile_duration_avg_sec", "Reconcile Mittelwert (sum/count)", "#7570b3", "--"),
        ("reconcile_duration_median_sec", "Reconcile Median (p50)", "#1b9e77", "-"),
    ]:
        if col in df.columns:
            ax1.plot(df.index, df[col], label=lbl, color=col_hex, linestyle=ls, marker=".", markersize=3.5)
    ax1.set_title("A: Reconcile-Latenz im Versuchsverlauf (keine Interpolation bei Inaktivität)")
    ax1.set_ylabel("Latenz [s]")
    ax1.grid(True)
    add_headroom(ax1, factor=1.38, min_top=0.5)
    ax1.legend(loc="upper left", ncol=3)

    shade_phases(ax2, df)
    if "git_request_duration_p95_sec" in df.columns:
        ax2.plot(df.index, df["git_request_duration_p95_sec"], label="Git-Request p95 (repo-server)", color="#377eb8", marker=".", markersize=3.5)
    ax2.set_title("B: Git-Request-Latenz (p95, 1m-Fenster)")
    ax2.set_ylabel("Latenz [s]")
    ax2.set_xlabel("Zeitpunkt (UTC)")
    ax2.grid(True)
    add_headroom(ax2, factor=1.28, min_top=0.5)
    ax2.legend(loc="upper left")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    save_fig(out_dir, f"{prefix}latency_distribution")


def plot_resources(df: pd.DataFrame, out_dir: str, prefix: str) -> None:
    """Plot 2: RAM und CPU von Controller, Repo-Server und Monitoring-Overhead."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.5, 5.6), sharex=True)

    shade_phases(ax1, df, with_labels=True)
    max_ram_mib = 0.0
    for col, lbl, c, ls in [
        ("controller_memory_bytes", "Controller RAM (Working Set)", "#7570b3", "-"),
        ("repo_server_memory_bytes", "Repo-Server RAM", "#377eb8", "-"),
        ("monitoring_memory_bytes", "Monitoring-Namespace (Observer)", "#666666", "--"),
    ]:
        if col in df.columns:
            vals_mib = df[col] / (1024 * 1024)
            finite = vals_mib.dropna()
            if not finite.empty:
                max_ram_mib = max(max_ram_mib, float(finite.max()))
            ax1.plot(df.index, vals_mib, label=lbl, color=c, linestyle=ls)
    ax1.axhline(y=512, color="#d95f02", linestyle=":", label="Controller Limit (512 MiB)")
    ax1.axhline(y=384, color="#377eb8", linestyle=":", alpha=0.8, label="Repo-Server Limit (384 MiB)")
    ax1.set_title("A: Speicherverbrauch (Control Plane vs. Limits & Observer-Overhead)")
    ax1.set_ylabel("RAM [MiB]")
    ax1.grid(True)
    ax1.set_ylim(0.0, max(720.0, max_ram_mib * 1.35))
    ax1.legend(loc="upper left", ncol=3)

    shade_phases(ax2, df)
    for col, lbl, c, ls in [
        ("controller_cpu_cores", "Controller CPU", "#e7298a", "-"),
        ("repo_server_cpu_cores", "Repo-Server CPU (TLS/Git)", "#377eb8", "-"),
        ("monitoring_cpu_cores", "Monitoring CPU (Observer)", "#666666", "--"),
    ]:
        if col in df.columns:
            ax2.plot(df.index, df[col] * 1000, label=lbl, color=c, linestyle=ls)
    ax2.set_title("B: CPU-Auslastung im Versuchsverlauf")
    ax2.set_ylabel("CPU [mCores]")
    ax2.set_xlabel("Zeitpunkt (UTC)")
    ax2.grid(True)
    add_headroom(ax2, factor=1.32, min_top=5.0, pad_bottom=True)
    ax2.legend(loc="upper left", ncol=3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    save_fig(out_dir, f"{prefix}controller_resources")


def plot_resilience_timeline(df: pd.DataFrame, out_dir: str, prefix: str) -> None:
    """Plot 3: Mehrschichtiger Verlauf (Latenz, Git-/Sync-Ops, Pod-Retransmissions & Kernel-Drops)."""
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10.5, 7.6), sharex=True)

    shade_phases(ax1, df, with_labels=True)
    if "reconcile_duration_p95_sec" in df.columns:
        ax1.plot(df.index, df["reconcile_duration_p95_sec"], label="Reconcile p95 [s]", color="#d95f02", marker=".", markersize=3)
    if "git_request_duration_p95_sec" in df.columns:
        ax1.plot(df.index, df["git_request_duration_p95_sec"], label="Git-Request p95 [s]", color="#7570b3", linestyle="--", marker=".", markersize=3)
    ax1.set_title("Mehrschichtige Fehlerkette: Latenz, Operationsraten und Kernel-Netzwerk")
    ax1.set_ylabel("Latenz [s]")
    ax1.grid(True)
    add_headroom(ax1, factor=1.38, min_top=0.5)
    ax1.legend(loc="upper left", ncol=2)

    shade_phases(ax2, df)
    for col, lbl, c in [
        ("git_request_rate", "Git-Requests/s (1m-Mittel)", "#377eb8"),
        ("sync_succeeded_rate", "Auto-Sync Erfolg/s (1m-Mittel)", "#1b9e77"),
        ("sync_failed_rate", "Sync Fehler/s (1m-Mittel)", "#e41a1c"),
    ]:
        if col in df.columns and (col != "sync_failed_rate" or df[col].max() > 0):
            ax2.plot(df.index, df[col], label=lbl, color=c)
    ax2.set_ylabel("Ops / s")
    ax2.grid(True)
    add_headroom(ax2, factor=1.45, min_top=0.02, pad_bottom=True)
    if "app_degraded_state" in df.columns:
        ax2_t = ax2.twinx()
        ax2_t.step(df.index, df["app_degraded_state"], where="post", color="#e41a1c", alpha=0.65, linestyle=":", label="Degraded/Drift (0/1)")
        ax2_t.set_ylim(-0.08, 1.50)
        ax2_t.set_yticks([0, 1])
        ax2_t.set_ylabel("Status [0/1]", color="#e41a1c")
        l1, lb1 = ax2.get_legend_handles_labels()
        l2, lb2 = ax2_t.get_legend_handles_labels()
        ax2.legend(l1 + l2, lb1 + lb2, loc="upper left", ncol=2)
    else:
        ax2.legend(loc="upper left")

    shade_phases(ax3, df)
    for col, lbl, c, ls in [
        ("iptables_drop_rate", "iptables FORWARD Drops/s (5s-Takt)", "#e41a1c", "-"),
        ("pod_tcp_retrans_rate", "repo-server TCP Retrans/s (5s-Takt)", "#333333", "-"),
        ("host_tcp_retrans_rate", "Host-netns TCP Retrans/s (1m-Mittel)", "#999999", "--"),
    ]:
        if col in df.columns:
            ax3.plot(df.index, df[col], label=lbl, color=c, linestyle=ls)
    ax3.set_ylabel("Pakete / s")
    ax3.set_xlabel("Zeitpunkt (UTC)")
    ax3.grid(True)
    add_headroom(ax3, factor=1.40, min_top=0.5, pad_bottom=True)
    ax3.legend(loc="upper left", ncol=2)
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    save_fig(out_dir, f"{prefix}resilience_timeline")


def _peak(run: Dict[str, Any], metric: str, scale: float = 1.0) -> Optional[float]:
    st = (run.get("prometheus_stats") or {}).get(metric)
    return (st["max"] * scale) if st and st.get("count", 0) > 0 else None


def plot_summary_batches(summary_paths: List[str], out_dir: str) -> None:
    """Plot 4: Cross-Run-Verteilung fuer eine Stufe (N>=2) oder mehrere Sweep-Stufen (merged pro p)."""
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for p in summary_paths:
        with open(p, "r", encoding="utf-8") as fh:
            b = json.load(fh)
        prob = int(round(((b.get("config") or {}).get("probability", 0.0)) * 100))
        grouped.setdefault(prob, []).extend(b.get("runs") or [])

    if not grouped:
        return

    sorted_probs = sorted(grouped.keys())
    if len(sorted_probs) == 1 and len(grouped[sorted_probs[0]]) < 2:
        print("[INFO] Weniger als 2 Laeufe; ueberspringe Cross-Run-Diagramm.")
        return

    fig, ((ax_t, ax_l), (ax_m, ax_n)) = plt.subplots(2, 2, figsize=(10.5, 7.6))

    if len(sorted_probs) == 1:
        prob = sorted_probs[0]
        runs = grouped[prob]
        target_dir = os.path.join(out_dir, f"p{prob}")
        os.makedirs(target_dir, exist_ok=True)
        stem = "cross_run_summary"
        strip_box(ax_t, [("Dwell Time", [r.get("dwell_time_sec") for r in runs], "#fc8d62"), ("MTTR post-fault", [r.get("mttr_post_fault_sec") for r in runs], "#66c2a5")], f"A: Selbstheilungszeiten (p={prob}%, N={len(runs)})", "Dauer [s]")
        strip_box(ax_l, [("Peak p95", [_peak(r, "reconcile_duration_p95_sec") for r in runs], "#d95f02"), ("Peak Avg", [_peak(r, "reconcile_duration_avg_sec") for r in runs], "#7570b3")], "B: Maximale Reconcile-Latenz je Lauf", "Latenz [s]")
        strip_box(ax_m, [("Controller", [_peak(r, "controller_memory_bytes", 1 / 1048576) for r in runs], "#7570b3"), ("Repo-Server", [_peak(r, "repo_server_memory_bytes", 1 / 1048576) for r in runs], "#377eb8")], "C: Spitzen-RAM je Lauf", "RAM [MiB]")
        strip_box(ax_n, [("Pod Retrans/s", [_peak(r, "pod_tcp_retrans_rate") for r in runs], "#4d4d4d"), ("iptables Drops/s", [_peak(r, "iptables_drop_rate") for r in runs], "#e41a1c")], "D: Netzstörungs-Intensität je Lauf", "Rate [1/s]")
    else:
        target_dir = out_dir
        stem = "sweep_cross_run_summary"
        palette = ["#1b9e77", "#377eb8", "#ff7f00", "#e41a1c", "#7570b3"]
        t_specs, l_specs, m_specs, n_specs = [], [], [], []
        for idx, prob in enumerate(sorted_probs):
            runs = grouped[prob]
            lbl, col = f"{prob}%", palette[idx % len(palette)]
            t_specs.append((lbl, [r.get("mttr_post_fault_sec") for r in runs], col))
            l_specs.append((lbl, [_peak(r, "reconcile_duration_p95_sec") for r in runs], col))
            m_specs.append((lbl, [_peak(r, "controller_memory_bytes", 1 / 1048576) for r in runs], col))
            n_specs.append((lbl, [_peak(r, "iptables_drop_rate") for r in runs], col))
        strip_box(ax_t, t_specs, "A: MTTR post-fault nach Paketverlust", "Dauer [s]")
        strip_box(ax_l, l_specs, "B: Peak Reconcile p95 nach Paketverlust", "Latenz [s]")
        strip_box(ax_m, m_specs, "C: Peak Controller-RAM nach Paketverlust", "RAM [MiB]")
        strip_box(ax_n, n_specs, "D: Peak FORWARD-Drops/s nach Paketverlust", "Rate [1/s]")

    ax_m.axhline(y=512, color="#d95f02", linestyle=":", linewidth=1.2, label="Controller Limit (512 MiB)")
    if len(sorted_probs) == 1:
        ax_m.axhline(y=384, color="#377eb8", linestyle="--", linewidth=1.2, label="Repo-Server Limit (384 MiB)")
    top_m = max(620.0, ax_m.get_ylim()[1])
    ax_m.set_ylim(-0.02 * top_m, top_m)
    ax_m.legend(loc="upper right")
    save_fig(target_dir, stem)


def main() -> None:
    parser = argparse.ArgumentParser(description="Erzeugt Publikationsdiagramme aus CSV- oder Summary-JSON-Daten.")
    parser.add_argument("--input", default=None, help="Pfad zur Messdaten-CSV-Datei")
    parser.add_argument("--summary", default=None, help="Pfad zu summary_<batch_id>.json")
    parser.add_argument("--summaries", nargs="+", default=None, help="Mehrere summary_*.json fuer Parameter-Sweep")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    out_dir = args.output_dir or os.path.abspath(os.path.join(os.path.dirname(__file__), "plots"))
    os.makedirs(out_dir, exist_ok=True)

    summary_list = args.summaries or ([args.summary] if args.summary else None)
    if summary_list:
        plot_summary_batches(summary_list, out_dir)
        if not args.input:
            return

    csv_file = args.input
    if not csv_file:
        data_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
        csvs = sorted(glob.glob(os.path.join(data_root, "p*", "run*", "*.csv"))) or sorted(glob.glob(os.path.join(data_root, "*.csv")))
        if not csvs:
            print("[FEHLER] Keine CSV-Dateien gefunden!", file=sys.stderr)
            sys.exit(1)
        csv_file = csvs[-1]

    df = pd.read_csv(csv_file, parse_dates=["timestamp"], index_col="timestamp")
    abs_csv = os.path.abspath(csv_file)
    run_dir = os.path.basename(os.path.dirname(abs_csv))
    prob_dir = os.path.basename(os.path.dirname(os.path.dirname(abs_csv)))
    if re.fullmatch(r"p\d+", prob_dir) and re.fullmatch(r"run\d+", run_dir):
        plot_dir = os.path.join(out_dir, prob_dir, run_dir)
        os.makedirs(plot_dir, exist_ok=True)
        prefix = ""
    else:
        plot_dir = out_dir
        prefix = os.path.splitext(os.path.basename(csv_file))[0] + "_"

    plot_latency_timeseries(df, plot_dir, prefix)
    plot_resources(df, plot_dir, prefix)
    plot_resilience_timeline(df, plot_dir, prefix)
    print(f"[OK] Diagramme gespeichert in {plot_dir}")


if __name__ == "__main__":
    main()
