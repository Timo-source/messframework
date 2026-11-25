#!/usr/bin/env python3
"""Vollautomatisierter End-to-End-Orchestrator fuer Warm-up, Parameter-Sweep und Auswertung."""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from typing import List

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
]


def get_tf_ip(tf_dir: str) -> str:
    res = subprocess.run(["terraform", "output", "-raw", "instance_public_ip"], cwd=tf_dir, capture_output=True, text=True, timeout=10)
    return res.stdout.strip() if res.returncode == 0 else ""


def ssh_cmd(host: str, key: str, remote_cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", *SSH_OPTS, "-i", key, f"ubuntu@{host}", remote_cmd], capture_output=True, text=True, timeout=20)


def wait_for_cluster(host: str, key: str, timeout_sec: int = 600) -> None:
    print(f"[1/4] Warte auf Bootstrap & Steady State auf {host}...", flush=True)
    check = "[ -f /var/log/bootstrap-complete.log ] && sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n argocd get app baseline-workload-app -o jsonpath='{.status.sync.status}|{.status.health.status}'"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        res = ssh_cmd(host, key, check)
        if res.returncode == 0 and res.stdout.strip() == "Synced|Healthy":
            mem = ssh_cmd(host, key, "free -m")
            print("[OK] Cluster bereit (Synced & Healthy).\n" + mem.stdout.strip(), flush=True)
            return
        time.sleep(10)
    raise RuntimeError(f"Cluster wurde innerhalb von {timeout_sec}s nicht bereit.")


def run_warmup(host: str, key: str, run_exp: str, data_dir: str, cooldown: int) -> None:
    print("\n[2/4] Starte kurzen Warm-up-Lauf (1x 60s @ 80% Drop)...", flush=True)
    prob_dir = os.path.join(data_dir, "p80")
    before = set(os.listdir(prob_dir)) if os.path.isdir(prob_dir) else set()
    subprocess.run(
        [sys.executable, run_exp, "--host", host, "--ssh-key", key, "--runs", "1", "--duration", "60", "--probability", "0.80", "--skip-plots"],
        check=True,
    )
    after = set(os.listdir(prob_dir)) if os.path.isdir(prob_dir) else set()
    created = after - before
    for fname in sorted(created):
        if fname.startswith("summary_") and fname.endswith(".json"):
            with open(os.path.join(prob_dir, fname), "r", encoding="utf-8") as fh:
                s = json.load(fh)
            env = s.get("environment") or {}
            run0 = (s.get("runs") or [{}])[0]
            netns_ok = env.get("repo_server_netns_detected")
            series_cnt = env.get("prom_git_series_count")
            drops = run0.get("kernel_drops_total")
            swap_ok = (run0.get("swap_purity") or {}).get("passed")
            print(f"[Warm-up Check] netns={netns_ok} | prom_series={series_cnt} | drops={drops} | swap_pure={swap_ok}", flush=True)
            if not netns_ok or (series_cnt or 0) > 2 or not swap_ok or not drops:
                print("[WARN] Warm-up-Check auffaellig – bitte Log/Daten im Blick behalten!", flush=True)

    for entry in created:
        target = os.path.join(prob_dir, entry)
        if os.path.isdir(target):
            shutil.rmtree(target)
        elif os.path.isfile(target):
            os.remove(target)
    if os.path.isdir(prob_dir) and not os.listdir(prob_dir):
        os.rmdir(prob_dir)
    print(f"[OK] Warm-up-Dateien bereinigt. Warte {cooldown}s Cooldown...", flush=True)
    if cooldown > 0:
        time.sleep(cooldown)


def main() -> None:
    parser = argparse.ArgumentParser(description="Automatisiert Warm-up, Parameter-Sweep und Sweep-Plot.")
    parser.add_argument("--host", default=None)
    parser.add_argument("--ssh-key", default=os.path.expanduser("~/.ssh/id_ed25519"))
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--probs", type=float, nargs="+", default=[0.0, 0.5, 0.8, 1.0])
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--batch-cooldown", type=int, default=60)
    parser.add_argument("--skip-warmup", action="store_true")
    args = parser.parse_args()

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    data_dir = os.path.join(base_dir, "data")
    run_exp = os.path.join(base_dir, "scripts", "run-experiment.py")
    plot_py = os.path.join(base_dir, "analysis", "plot-results.py")
    os.makedirs(data_dir, exist_ok=True)

    host = args.host or get_tf_ip(os.path.join(base_dir, "terraform"))
    if not host:
        print("[FEHLER] Keine EC2-IP ermittelt (terraform apply ausgefuehrt?).", file=sys.stderr)
        sys.exit(1)

    wait_for_cluster(host, args.ssh_key)
    if not args.skip_warmup:
        run_warmup(host, args.ssh_key, run_exp, data_dir, args.batch_cooldown)

    print(f"\n[3/4] Starte Parameter-Sweep ueber {len(args.probs)} Stufen (je {args.runs} Laeufe)...", flush=True)
    for idx, prob in enumerate(args.probs):
        print(f"\n=== Sweep-Stufe {idx + 1}/{len(args.probs)}: p={prob:.2f} ===", flush=True)
        subprocess.run(
            [sys.executable, run_exp, "--host", host, "--ssh-key", args.ssh_key, "--runs", str(args.runs), "--duration", str(args.duration), "--probability", f"{prob:.2f}"],
            check=True,
        )
        if idx < len(args.probs) - 1 and args.batch_cooldown > 0:
            print(f"[Cooldown] Warte {args.batch_cooldown}s vor naechster Sweep-Stufe...", flush=True)
            time.sleep(args.batch_cooldown)

    all_summaries = sorted(glob.glob(os.path.join(data_dir, "p*", "summary_*.json")))
    if len(all_summaries) >= 2:
        print("\n[4/4] Erzeuge gemeinsames Sweep-Vergleichsdiagramm...", flush=True)
        subprocess.run([sys.executable, plot_py, "--summaries", *all_summaries], check=True)

    print("\n=== Sweep komplett abgeschlossen ===")
    for s in all_summaries:
        print(f"  Summary: {s}")
    print("WICHTIG: Jetzt 'terraform destroy' im terraform-Verzeichnis ausfuehren!")


if __name__ == "__main__":
    main()
