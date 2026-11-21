#!/usr/bin/env python3
"""WiFi-resilienter Orchestrator fuer das GitOps-Resilienz-Experiment."""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=3",
]


def get_tf_ip(tf_dir: str) -> str:
    try:
        res = subprocess.run(["terraform", "output", "-raw", "instance_public_ip"], cwd=tf_dir, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def ssh_run(host: str, key: str, cmd: str, check: bool = True, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", *SSH_OPTS, "-i", key, f"ubuntu@{host}", cmd], capture_output=True, text=True, check=check, timeout=timeout)


def scp_transfer(src: str, dst: str, key: str, timeout: int = 60) -> None:
    subprocess.run(["scp", *SSH_OPTS, "-i", key, src, dst], check=True, timeout=timeout)


def poll_remote(host: str, key: str, remote_dir: str, offset: int, pid: Optional[int]) -> Tuple[str, int, str, bool]:
    pid_check = f"[ -d /proc/{pid} ]" if pid else f"pgrep -u root -f {shlex.quote('node-runner.py.*' + os.path.basename(remote_dir))} >/dev/null 2>&1"
    script = (
        f"LOG={shlex.quote(remote_dir + '/runner.log')}; "
        f"STATE=RUNNING; "
        f"if [ -f {shlex.quote(remote_dir + '/DONE')} ]; then STATE=DONE; "
        f"elif [ -f {shlex.quote(remote_dir + '/FAILED')} ]; then STATE=FAILED; "
        f"elif ! ({pid_check}); then STATE=STOPPED; fi; "
        f"SIZE=$(stat -c%s \"$LOG\" 2>/dev/null || echo 0); "
        f"echo \"__STATE__:$STATE:$SIZE\"; "
        f"if [ \"$SIZE\" -ge {offset} ]; then tail -c +{offset} \"$LOG\" 2>/dev/null || true; fi"
    )
    try:
        res = ssh_run(host, key, script, check=False, timeout=15)
        lines = res.stdout.splitlines(keepends=True)
        if res.returncode == 255 or not lines or not lines[0].startswith("__STATE__:"):
            return "NET_ERR", offset, "", False
        _, state, size = lines[0].strip().split(":")
        return state, int(size) + 1, "".join(lines[1:]), True
    except (subprocess.TimeoutExpired, OSError):
        return "NET_ERR", offset, "", False


def print_summary(summary: Dict) -> None:
    agg = summary.get("aggregate", {})
    runs = summary.get("runs", [])
    print(f"\n=== Ergebnisuebersicht: Batch {summary.get('batch_id')} ===")
    print(f"Laeufe: {agg.get('runs_total')} | Geheilt: {agg.get('runs_recovered')} | Swap-Rein: {agg.get('runs_swap_pure')}")
    print(f"{'Lauf':<6} {'Dwell[s]':<9} {'MTTR[s]':<9} {'p95[s]':<8} {'Avg[s]':<8} {'CtrlRAM':<9} {'RepoRAM':<9} {'Retr/s':<8} {'Drops':<7} {'Swap'}")

    for r in runs:
        ps = r.get("prometheus_stats") or {}
        dwell = f"{r['dwell_time_sec']:.1f}" if r.get("dwell_time_sec") is not None else "-"
        mttr = f"{r['mttr_post_fault_sec']:.1f}" if r.get("mttr_post_fault_sec") is not None else "-"
        p95 = ps.get("reconcile_duration_p95_sec", {}).get("max", 0.0)
        avg = ps.get("reconcile_duration_avg_sec", {}).get("max", 0.0)
        ctrl = ps.get("controller_memory_bytes", {}).get("max", 0.0) / (1024 * 1024)
        repo = ps.get("repo_server_memory_bytes", {}).get("max", 0.0) / (1024 * 1024)
        retr = ps.get("pod_tcp_retrans_rate", {}).get("max", 0.0)
        swap = "PASS" if (r.get("swap_purity") or {}).get("passed") else "FAIL"
        print(f"#{r['run_index']:<5} {dwell:<9} {mttr:<9} {p95:<8.3f} {avg:<8.3f} {ctrl:<9.1f} {repo:<9.1f} {retr:<8.2f} {r.get('kernel_drops_total', 0):<7} {swap}")

    if agg.get("runs_total", 0) > 1:
        print("-" * 80)
        for label, key in [
            ("Dwell Time [s]", "dwell_time_sec"),
            ("MTTR post-fault [s]", "mttr_post_fault_sec"),
            ("Peak Reconcile p95 [s]", "peak_reconcile_p95_sec"),
            ("Peak Reconcile Avg [s]", "peak_reconcile_avg_sec"),
            ("Peak Controller RAM [MiB]", "peak_controller_memory_mib"),
            ("Peak Repo-Server RAM [MiB]", "peak_repo_server_memory_mib"),
            ("Peak Monitoring RAM [MiB]", "peak_monitoring_memory_mib"),
        ]:
            st = agg.get(key) or {}
            if st.get("count", 0) > 0:
                print(f"  {label:<28} Mean={st['mean']:7.2f} | SD={st['std']:6.2f} | Med={st['median']:7.2f} | {st['min']:.2f}..{st['max']:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="WiFi-resilienter GitOps-Chaos-Orchestrator.")
    parser.add_argument("--host", default=None)
    parser.add_argument("--ssh-key", default=os.path.expanduser("~/.ssh/id_ed25519"))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--probability", type=float, default=0.80)
    parser.add_argument("--baseline-wait", type=int, default=30)
    parser.add_argument("--post-wait", type=int, default=90)
    parser.add_argument("--cooldown", type=int, default=60)
    parser.add_argument("--no-drift", action="store_true")
    parser.add_argument("--attach", default=None)
    parser.add_argument("--skip-plots", action="store_true")
    args = parser.parse_args()

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    data_dir = os.path.join(base_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    host = args.host or get_tf_ip(os.path.join(base_dir, "terraform"))
    if not host:
        print("[FEHLER] Keine EC2-IP ermittelt. Laeuft das Cluster? (terraform apply)", file=sys.stderr)
        sys.exit(1)

    batch_id = args.attach or datetime.now().strftime("%Y%m%d_%H%M%S")
    remote_dir = f"/home/ubuntu/chaos-runs/{batch_id}"
    remote_pid: Optional[int] = None

    if not args.attach:
        print(f"Starte Batch {batch_id} auf {host} (runs={args.runs}, p={args.probability:.2f}, dur={args.duration}s)...")
        ssh_run(host, args.ssh_key, f"mkdir -p {shlex.quote(remote_dir)}")
        for fname in ("inject-iptables.sh", "node-runner.py"):
            scp_transfer(os.path.join(base_dir, "scripts", fname), f"ubuntu@{host}:/home/ubuntu/{fname}", args.ssh_key, timeout=30)

        ssh_run(host, args.ssh_key, "sed -i 's/\\r$//' /home/ubuntu/inject-iptables.sh /home/ubuntu/node-runner.py && chmod +x /home/ubuntu/inject-iptables.sh /home/ubuntu/node-runner.py")
        runner_args = (
            f"--batch-id {shlex.quote(batch_id)} --out-dir {shlex.quote(remote_dir)} "
            f"--runs {args.runs} --duration {args.duration} --probability {args.probability:.4f} "
            f"--baseline-wait {args.baseline_wait} --post-wait {args.post_wait} --cooldown {args.cooldown}"
            + (" --no-drift" if args.no_drift else "")
        )
        inner = f"nohup python3 -u /home/ubuntu/node-runner.py {runner_args} > {shlex.quote(remote_dir + '/runner.log')} 2>&1 < /dev/null & echo $!"
        res = ssh_run(host, args.ssh_key, f"sudo sh -c {shlex.quote(inner)}")
        try:
            remote_pid = int(res.stdout.strip().splitlines()[-1])
        except Exception:
            remote_pid = None
        print(f"[OK] node-runner.py aktiv (PID: {remote_pid or 'unbekannt'}).")
    else:
        print(f"Verbinde wieder mit Batch {batch_id} auf {host}...")

    offset, net_warned = 1, False
    while True:
        state, offset, chunk, ok = poll_remote(host, args.ssh_key, remote_dir, offset, remote_pid)
        if not ok:
            if not net_warned:
                print("\n[WARN] SSH unterbrochen – Messung laeuft lokal auf EC2 weiter...", flush=True)
                net_warned = True
            time.sleep(5)
            continue
        if net_warned:
            print("[OK] SSH wiederhergestellt.\n", flush=True)
            net_warned = False
        if chunk:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        if state == "DONE":
            break
        if state in ("FAILED", "STOPPED"):
            try:
                scp_transfer(f"ubuntu@{host}:{remote_dir}/runner.log", os.path.join(data_dir, f"runner_{batch_id}.log"), args.ssh_key)
            except Exception:
                pass
            print(f"\n[FEHLER] Remote-Laeufer beendet mit Status {state}.", file=sys.stderr)
            sys.exit(1)
        time.sleep(5)

    local_summary = os.path.join(data_dir, f"summary_{batch_id}.json")
    scp_transfer(f"ubuntu@{host}:{remote_dir}/summary_{batch_id}.json", local_summary, args.ssh_key)
    scp_transfer(f"ubuntu@{host}:{remote_dir}/runner.log", os.path.join(data_dir, f"runner_{batch_id}.log"), args.ssh_key)

    with open(local_summary, "r", encoding="utf-8") as fh:
        summary = json.load(fh)

    csvs: List[str] = []
    for run in summary.get("runs", []):
        if run.get("csv_file"):
            local_csv = os.path.join(data_dir, run["csv_file"])
            scp_transfer(f"ubuntu@{host}:{remote_dir}/{run['csv_file']}", local_csv, args.ssh_key)
            csvs.append(local_csv)

    print_summary(summary)

    if not args.skip_plots and csvs:
        plot_script = os.path.join(base_dir, "analysis", "plot-results.py")
        for csv_path in csvs:
            subprocess.run([sys.executable, plot_script, "--input", csv_path], check=True)
        if len(summary.get("runs", [])) >= 2:
            subprocess.run([sys.executable, plot_script, "--summary", local_summary], check=True)

    print(f"\n[OK] Fertig: {local_summary}")


if __name__ == "__main__":
    main()
