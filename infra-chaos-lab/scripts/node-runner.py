#!/usr/bin/env python3
"""Autarker On-Node-Messlaeufer fuer das GitOps-Resilienz-Messframework."""

import argparse
import csv
import datetime
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

KUBECONFIG_PATH = "/etc/rancher/k3s/k3s.yaml"
INJECT_SCRIPT = "/home/ubuntu/inject-iptables.sh"
PHASES = ("baseline", "fault", "recovery", "post_recovery")

PROMQL_QUERIES: Dict[str, str] = {
    "reconcile_duration_p95_sec": 'histogram_quantile(0.95, sum(rate(argocd_app_reconcile_bucket[1m])) by (le))',
    "reconcile_duration_median_sec": 'histogram_quantile(0.50, sum(rate(argocd_app_reconcile_bucket[1m])) by (le))',
    "reconcile_duration_avg_sec": 'sum(rate(argocd_app_reconcile_sum[1m])) / sum(rate(argocd_app_reconcile_count[1m]))',
    "git_request_duration_p95_sec": 'histogram_quantile(0.95, sum(rate(argocd_git_request_duration_seconds_bucket[1m])) by (le))',
    "git_request_rate": 'sum(rate(argocd_git_request_total[1m])) or vector(0)',
    "sync_succeeded_rate": 'sum(rate(argocd_app_sync_total{phase="Succeeded"}[1m])) or vector(0)',
    "sync_failed_rate": 'sum(rate(argocd_app_sync_total{phase=~"Failed|Error"}[1m])) or vector(0)',
    "controller_memory_bytes": 'sum(container_memory_working_set_bytes{namespace="argocd", container=~".*application-controller.*|controller"})',
    "controller_cpu_cores": 'sum(rate(container_cpu_usage_seconds_total{namespace="argocd", container=~".*application-controller.*|controller"}[1m]))',
    "controller_cpu_throttled_rate": 'sum(rate(container_cpu_cfs_throttled_seconds_total{namespace="argocd", container=~".*application-controller.*|controller"}[1m])) or vector(0)',
    "repo_server_memory_bytes": 'sum(container_memory_working_set_bytes{namespace="argocd", container=~".*repo-server.*"})',
    "repo_server_cpu_cores": 'sum(rate(container_cpu_usage_seconds_total{namespace="argocd", container=~".*repo-server.*"}[1m]))',
    "repo_server_cpu_throttled_rate": 'sum(rate(container_cpu_cfs_throttled_seconds_total{namespace="argocd", container=~".*repo-server.*"}[1m])) or vector(0)',
    "monitoring_memory_bytes": 'sum(container_memory_working_set_bytes{namespace="monitoring", container!="", container!="POD"})',
    "monitoring_cpu_cores": 'sum(rate(container_cpu_usage_seconds_total{namespace="monitoring", container!="", container!="POD"}[1m]))',
    "host_tcp_retrans_rate": 'sum(rate(node_netstat_Tcp_RetransSegs[1m])) or vector(0)',
    "cpu_steal_rate": 'sum(rate(node_cpu_seconds_total{mode="steal"}[1m])) or vector(0)',
    "swap_used_bytes": '(node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes) or vector(0)',
    "swap_page_rate": '(rate(node_vmstat_pswpin[1m]) + rate(node_vmstat_pswpout[1m])) or vector(0)',
}


def utc_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def run_cmd(cmd: List[str], timeout: int = 10) -> Optional[subprocess.CompletedProcess]:
    env = os.environ.copy()
    env.setdefault("KUBECONFIG", KUBECONFIG_PATH)
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:
        return None


def kubectl(*args: str) -> Tuple[bool, str]:
    res = run_cmd(["kubectl", *args])
    if res is None or res.returncode != 0:
        return False, ""
    return True, res.stdout.strip()


def get_app_status(app_name: str) -> Tuple[str, str, str, Optional[str], Optional[str], bool]:
    ok, out = kubectl("-n", "argocd", "get", "application", app_name, "-o", "json")
    if not ok or not out:
        return "Unknown", "Unknown", "KubectlError", None, None, False
    try:
        status = json.loads(out).get("status") or {}
        sync_s = (status.get("sync") or {}).get("status") or "Unknown"
        health_s = (status.get("health") or {}).get("status") or "Unknown"
        conds_list = [c for c in (status.get("conditions") or []) if isinstance(c, dict) and c.get("type")]
        conds = " ".join(c["type"] for c in conds_list)
        cond_ts = conds_list[0].get("lastTransitionTime") if conds_list else None
        return sync_s, health_s, conds, status.get("reconciledAt"), cond_ts, True
    except Exception:
        return "Unknown", "Unknown", "StatusParseError", None, None, False


def get_workload_label() -> Optional[str]:
    ok, out = kubectl("-n", "workloads", "get", "deployment", "baseline-workload", "-o", "jsonpath={.metadata.labels.experiment}")
    return out if ok else None


def set_workload_label(value: str) -> bool:
    return kubectl("-n", "workloads", "label", "deployment", "baseline-workload", f"experiment={value}", "--overwrite")[0]


def trigger_hard_refresh(app_name: str) -> bool:
    return kubectl("-n", "argocd", "annotate", "application", app_name, "argocd.argoproj.io/refresh=hard", "--overwrite")[0]


def read_vmstat_swap() -> Tuple[int, int]:
    vals = {"pswpin": 0, "pswpout": 0}
    with open("/proc/vmstat", "r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) == 2 and parts[0] in vals:
                vals[parts[0]] = int(parts[1])
    return vals["pswpin"], vals["pswpout"]


def read_repo_server_retrans() -> Optional[Tuple[str, int]]:
    """Liest (netns_inode, Tcp:RetransSegs) gezielt aus dem argocd-repo-server-Pod."""
    try:
        host_ns = os.readlink("/proc/1/ns/net")
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                args = fh.read().split(b"\x00")
            if not any(os.path.basename(a) == "argocd-repo-server" for a in [x.decode("utf-8", "ignore") for x in args[:3] if x]):
                continue
            ns_inode = os.readlink(f"/proc/{pid}/ns/net")
            if ns_inode == host_ns:
                continue
            with open(f"/proc/{pid}/net/snmp", "r", encoding="utf-8") as fh:
                tcp_lines = [ln for ln in fh if ln.startswith("Tcp:")]
            if len(tcp_lines) >= 2:
                idx = tcp_lines[0].split().index("RetransSegs")
                return ns_inode, int(tcp_lines[1].split()[idx])
        except (OSError, ValueError):
            continue
    return None


def read_iptables_drops() -> Optional[int]:
    res = run_cmd(["iptables", "-t", "mangle", "-L", "FORWARD", "-v", "-n", "-x"], timeout=5)
    if res is None or res.returncode != 0:
        return None
    lines = [ln for ln in res.stdout.splitlines() if "gitops-chaos" in ln]
    counts = [int(ln.split()[0]) for ln in lines if ln.split() and ln.split()[0].isdigit()]
    return max(counts) if counts else None


def cleanup_iptables() -> None:
    if os.path.exists(INJECT_SCRIPT):
        run_cmd([INJECT_SCRIPT, "cleanup"], timeout=15)


def prom_get(url: str, timeout: int = 15) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload if payload.get("status") == "success" else None
    except Exception:
        return None


def discover_prometheus_url(port: int = 30090) -> str:
    candidates = [f"http://127.0.0.1:{port}"]
    ok, cip = kubectl("-n", "monitoring", "get", "svc", "prometheus-server", "-o", "jsonpath={.spec.clusterIP}")
    if ok and cip:
        candidates.extend([f"http://{cip}", f"http://{cip}:9090"])
    for url in candidates:
        if prom_get(f"{url}/api/v1/query?query=up", timeout=3):
            return url
    raise RuntimeError(f"Kein erreichbarer Prometheus-Endpunkt gefunden: {candidates}")


def query_prom_range(base_url: str, query: str, start_ts: float, end_ts: float, step: str = "5s") -> Dict[float, Optional[float]]:
    params = urllib.parse.urlencode({"query": query, "start": f"{start_ts:.3f}", "end": f"{end_ts:.3f}", "step": step})
    payload = prom_get(f"{base_url}/api/v1/query_range?{params}", timeout=20)
    results = ((payload or {}).get("data") or {}).get("result") or []
    if not results:
        return {}
    out: Dict[float, Optional[float]] = {}
    for ts_raw, val_raw in results[0].get("values") or []:
        val = float(val_raw)
        out[float(ts_raw)] = None if (math.isnan(val) or math.isinf(val)) else val
    return out


def collect_env_info(prom_url: str) -> Dict[str, Any]:
    k3s_res = run_cmd(["k3s", "--version"], timeout=5)
    _, argo_imgs = kubectl("-n", "argocd", "get", "pods", "-o", "jsonpath={.items[*].spec.containers[*].image}")
    _, mon_imgs = kubectl("-n", "monitoring", "get", "pods", "-o", "jsonpath={.items[*].spec.containers[*].image}")
    ram_mib, swappiness = None, None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    ram_mib = round(int(line.split()[1]) / 1024, 1)
                    break
        with open("/proc/sys/vm/swappiness", "r", encoding="utf-8") as fh:
            swappiness = int(fh.read().strip())
    except OSError:
        pass
    p = prom_get(f"{prom_url}/api/v1/query?{urllib.parse.urlencode({'query': 'count(argocd_git_request_total)'})}", timeout=10)
    res = ((p or {}).get("data") or {}).get("result") or []
    series_count = int(float(res[0]["value"][1])) if res else None
    if series_count is not None and series_count > 2:
        print(f"[WARN] Moegliches Prometheus Double-Scraping: count(argocd_git_request_total) = {series_count}", flush=True)
    repo_retrans = read_repo_server_retrans()
    if repo_retrans is None:
        print("[WARN] argocd-repo-server Pod-netns nicht gefunden (pod_tcp_retrans_rate evtl. 0.0).", flush=True)
    return {
        "kernel": os.uname().release,
        "k3s_version": k3s_res.stdout.splitlines()[0].strip() if k3s_res and k3s_res.returncode == 0 and k3s_res.stdout else None,
        "argocd_images": sorted(set(argo_imgs.split())),
        "monitoring_images": sorted(set(mon_imgs.split())),
        "total_ram_mib": ram_mib,
        "vm_swappiness": swappiness,
        "prom_git_series_count": series_count,
        "repo_server_netns_detected": repo_retrans is not None,
    }


def compute_stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"count": 0, "mean": 0.0, "std": 0.0, "median": 0.0, "min": 0.0, "max": 0.0}
    return {
        "count": len(values), "mean": round(statistics.mean(values), 6),
        "std": round(statistics.stdev(values), 6) if len(values) >= 2 else 0.0,
        "median": round(statistics.median(values), 6),
        "min": round(min(values), 6), "max": round(max(values), 6),
    }


def classify_phase(ts_f: float, fault_start: float, fault_end: float, recovered: Optional[float]) -> str:
    if ts_f < fault_start:
        return "baseline"
    if ts_f <= fault_end:
        return "fault"
    return "recovery" if (recovered is not None and ts_f <= recovered) else "post_recovery"


def export_csv(
    prom_url: str, start_ts: float, end_ts: float, fault_start: float, fault_end: float,
    recovered_ts: Optional[float], local_samples: List[Dict[str, Any]], csv_path: str,
) -> Tuple[int, Dict[str, Dict[str, float]], Dict[str, Dict[str, Dict[str, float]]]]:
    """Exportiert PromQL- und Kernel-Zeitreihen ohne ffill() oder 0.0-Ersetzung fuer Latenzen."""
    raw = {name: query_prom_range(prom_url, q, start_ts, end_ts) for name, q in PROMQL_QUERIES.items()}
    all_ts = sorted({ts for s in raw.values() for ts in s})
    prom_cols = list(PROMQL_QUERIES.keys())
    local_cols = ["pod_tcp_retrans_rate", "iptables_drop_rate", "app_degraded_state"]
    all_cols = prom_cols + local_cols

    overall: Dict[str, List[float]] = {m: [] for m in all_cols}
    by_phase: Dict[str, Dict[str, List[float]]] = {p: {m: [] for m in all_cols} for p in PHASES}

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "experiment_phase"] + all_cols)
        for ts_f in all_ts:
            phase = classify_phase(ts_f, fault_start, fault_end, recovered_ts)
            row: List[Any] = [utc_iso(ts_f), phase]
            for m in prom_cols:
                val = raw[m].get(ts_f)
                row.append("" if val is None else val)
                if val is not None:
                    overall[m].append(val)
                    by_phase[phase][m].append(val)
            candidates = [
                s for s in local_samples
                if (s["ts"] < fault_start) == (ts_f < fault_start)
                and (s["ts"] <= fault_end) == (ts_f <= fault_end)
            ]
            nearest = min(candidates, key=lambda s: abs(s["ts"] - ts_f)) if candidates else None
            valid_local = nearest is not None and abs(nearest["ts"] - ts_f) <= 10.0
            for lm in local_cols:
                if valid_local:
                    lval = float(nearest[lm])  # type: ignore[index]
                    if phase != "fault" and lm == "iptables_drop_rate":
                        lval = 0.0
                    overall[lm].append(lval)
                    by_phase[phase][lm].append(lval)
                    row.append(round(lval, 6))
                else:
                    row.append("")
            writer.writerow(row)

    stats = {m: compute_stats(vals) for m, vals in overall.items()}
    pstats = {p: {m: compute_stats(vals) for m, vals in by_phase[p].items()} for p in PHASES}
    return len(all_ts), stats, pstats


def wait_until_steady(app_name: str, check_label: bool, timeout_sec: int = 90) -> None:
    deadline = time.time() + timeout_sec
    last = ("Unknown", "Unknown", "Unknown")
    while time.time() < deadline:
        s_stat, h_stat, conds, _, _, ok = get_app_status(app_name)
        last = (s_stat, h_stat, conds)
        if ok and s_stat == "Synced" and h_stat == "Healthy" and not conds and ((not check_label) or get_workload_label() == "baseline-reconciliation"):
            return
        time.sleep(3)
    raise RuntimeError(f"Ausgangszustand nicht stabil nach {timeout_sec}s: {last}")


class KernelSampler:
    """Erfasst alle 5s repo-server Pod-Retransmissions, FORWARD-Drops und den App-Zustand."""

    def __init__(self, app_name: str):
        self.app_name = app_name
        self.prev_ts = time.time()
        retrans = read_repo_server_retrans()
        self.prev_ns, self.prev_retrans = retrans if retrans else (None, None)
        self.prev_drops = read_iptables_drops()
        self.max_drops = self.prev_drops or 0
        self.last_degraded = 0.0
        self.drift_pending: bool = False
        self.samples: List[Dict[str, Any]] = []

    def sample(self) -> Tuple[str, str, str, Optional[str], Optional[str], bool, bool, Dict[str, Any]]:
        now = time.time()
        dt = max(0.5, now - self.prev_ts)
        retrans, retrans_rate = read_repo_server_retrans(), 0.0
        if retrans:
            ns_inode, count = retrans
            if ns_inode == self.prev_ns and self.prev_retrans is not None:
                retrans_rate = max(0, count - self.prev_retrans) / dt
            self.prev_ns, self.prev_retrans = ns_inode, count

        curr_drops = read_iptables_drops()
        if curr_drops is None:
            drop_rate = 0.0
        else:
            self.max_drops = max(self.max_drops, curr_drops)
            drop_rate = (curr_drops if self.prev_drops is None else max(0, curr_drops - self.prev_drops)) / dt
        self.prev_drops, self.prev_ts = curr_drops, now

        s_stat, h_stat, conds, rec_at, cond_ts, ok = get_app_status(self.app_name)
        is_fully_healthy = (self.last_degraded == 0.0)
        if ok:
            if self.drift_pending and s_stat == "Synced" and h_stat == "Healthy" and not conds:
                if get_workload_label() == "baseline-reconciliation":
                    self.drift_pending = False
            is_fully_healthy = (s_stat == "Synced" and h_stat == "Healthy" and not conds and not self.drift_pending)
            self.last_degraded = 0.0 if is_fully_healthy else 1.0
        rec = {"ts": now, "pod_tcp_retrans_rate": round(retrans_rate, 4), "iptables_drop_rate": round(drop_rate, 4), "app_degraded_state": self.last_degraded}
        self.samples.append(rec)
        return s_stat, h_stat, conds, rec_at, cond_ts, ok, is_fully_healthy, rec


def run_interval_loop(duration: float, interval: float, sampler: KernelSampler) -> None:
    end_ts, next_tick = time.time() + duration, time.time() + interval
    while time.time() < end_ts:
        sleep_for = min(next_tick - time.time(), max(0.0, end_ts - time.time()))
        if sleep_for > 0:
            time.sleep(sleep_for)
        if time.time() >= end_ts:
            break
        sampler.sample()
        next_tick += interval


def execute_single_run(run_idx: int, total_runs: int, args: argparse.Namespace, prom_url: str, batch_id: str) -> Dict[str, Any]:
    is_control = args.probability <= 0.0
    print(f"\n--- Lauf {run_idx}/{total_runs} [{'KONTROLLE (0%)' if is_control else f'CHAOS ({int(args.probability * 100)}%)'}] ---", flush=True)
    cleanup_iptables()
    if not args.no_drift:
        set_workload_label("baseline-reconciliation")
    wait_until_steady(args.app_name, check_label=not args.no_drift)

    swap_in_start, swap_out_start = read_vmstat_swap()
    sampler = KernelSampler(args.app_name)
    baseline_start_ts = time.time()
    print(f"[Lauf {run_idx}] Baseline ({args.baseline_wait}s)...", flush=True)
    run_interval_loop(args.baseline_wait, args.poll_interval, sampler)
    sampler.sample()

    chaos_log = os.path.join(args.out_dir, f"chaos_run{run_idx}.log")
    fault_start_ts = time.time()
    fault_end_ev = threading.Event()
    fault_end_holder: Dict[str, Optional[float]] = {"ts": None}
    chaos_proc, chaos_fh = None, None

    if not is_control:
        env = {**os.environ, "PROBABILITY": f"{args.probability:.2f}", "DURATION": str(args.duration)}
        chaos_fh = open(chaos_log, "w", encoding="utf-8")
        chaos_proc = subprocess.Popen([INJECT_SCRIPT, "inject"], env=env, stdout=chaos_fh, stderr=subprocess.STDOUT)

    def _waiter() -> None:
        if chaos_proc is not None:
            chaos_proc.wait()
        else:
            time.sleep(args.duration)
        fault_end_holder["ts"] = time.time()
        fault_end_ev.set()

    waiter = threading.Thread(target=_waiter, daemon=True)
    waiter.start()

    first_non_normal_ts = fault_detected_ts = fault_end_ts = recovered_ts = None
    drift_injected_ts: Optional[float] = None
    stable_recovered_ts: Optional[float] = None
    first_fault_state = first_fault_cond_ts = recovered_reconciled_at = None
    transitions: List[Dict[str, Any]] = []
    last_key: Optional[Tuple[str, str, str]] = None
    drift_applied = False
    next_refresh_ts, next_poll = fault_start_ts + 33.0, fault_start_ts + 4.0

    try:
        # Hard-Refresh vor Drift verhindert sofortigen Cache-Hit-Self-Heal ueber Port 6443
        time.sleep(3.0)
        trigger_hard_refresh(args.app_name)
        time.sleep(1.0)
        if not args.no_drift:
            drift_applied = set_workload_label("drifted")
            if drift_applied:
                drift_injected_ts = time.time()
                sampler.drift_pending = True
            print(f"[Lauf {run_idx}] Drift injiziert (experiment=drifted, ok={drift_applied})", flush=True)

        while (time.time() - fault_start_ts) < (args.duration + args.post_fault_timeout):
            now = time.time()
            elapsed = now - fault_start_ts
            if fault_end_ts is None and fault_end_ev.is_set():
                fault_end_ts = fault_end_holder["ts"] or now
                print(f"[Lauf {run_idx}] Stoerungsfenster beendet ({fault_end_ts - fault_start_ts:.2f}s).", flush=True)

            s_stat, h_stat, conds, rec_at, cond_ts, ok, is_fully_healthy, rec = sampler.sample()
            if not ok:
                print(f"  [+{elapsed:05.1f}s] [WARN] kubectl fehlgeschlagen ({conds}), ignoriere Tick.", flush=True)
            else:
                if (s_stat, h_stat, conds) != last_key:
                    last_key = (s_stat, h_stat, conds)
                    transitions.append({"elapsed_sec": round(elapsed, 2), "timestamp_utc": utc_iso(now), "sync": s_stat, "health": h_stat, "conditions": conds, "reconciled_at": rec_at})

                if ("Error" in conds or s_stat == "Unknown") and fault_detected_ts is None:
                    fault_detected_ts = now
                    first_fault_state = {"sync": s_stat, "health": h_stat, "conditions": conds}
                    first_fault_cond_ts = cond_ts
                    print(f"[Lauf {run_idx}] [FAULT] Netzfehler: Sync={s_stat} Cond={conds or 'None'}", flush=True)

                if not is_fully_healthy:
                    if first_non_normal_ts is None:
                        first_non_normal_ts = drift_injected_ts if (drift_applied and drift_injected_ts is not None and s_stat == "Synced" and not conds) else now
                    stable_recovered_ts = None
                    recovered_reconciled_at = None
                else:
                    if (drift_applied or first_non_normal_ts is not None or fault_detected_ts is not None) and stable_recovered_ts is None:
                        stable_recovered_ts = now
                        recovered_reconciled_at = rec_at
                        if first_non_normal_ts is None and drift_injected_ts is not None:
                            first_non_normal_ts = drift_injected_ts

                print(f"  [+{elapsed:05.1f}s] Sync={s_stat:<9} Health={h_stat:<8} Retrans={rec['pod_tcp_retrans_rate']:.1f}/s Drops={rec['iptables_drop_rate']:.1f}/s" + (f" [{conds}]" if conds else ""), flush=True)

                if fault_end_ts is not None:
                    lbl_ok = (not drift_applied) or (get_workload_label() == "baseline-reconciliation")
                    sampler.drift_pending = not lbl_ok
                    if not lbl_ok:
                        is_fully_healthy = False
                        stable_recovered_ts = None
                    if is_fully_healthy and lbl_ok:
                        recovered_ts = stable_recovered_ts or now
                        if recovered_reconciled_at is None:
                            recovered_reconciled_at = rec_at
                        if not is_control:
                            print(f"[Lauf {run_idx}] [OK] Konvergiert (Synced & Healthy, Label bereinigt).", flush=True)
                        break
                    if not is_control and not drift_applied and (fault_detected_ts or first_non_normal_ts) is None and (now - fault_end_ts) >= 45.0:
                        print(f"[Lauf {run_idx}] [HINWEIS] Keine Degradierung beobachtet; beende Beobachtung.", flush=True)
                        break

            if fault_end_ts is None and now >= (next_refresh_ts - 0.5):
                next_refresh_ts += 30.0
                trigger_hard_refresh(args.app_name)

            next_poll += args.poll_interval
            sleep_for = next_poll - time.time()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_poll = time.time()
    finally:
        if chaos_proc is not None and chaos_proc.poll() is None:
            chaos_proc.terminate()
            try:
                chaos_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chaos_proc.kill()
        waiter.join(timeout=2)
        if chaos_fh is not None:
            chaos_fh.close()
        cleanup_iptables()
        if drift_applied and recovered_ts is None:
            set_workload_label("baseline-reconciliation")
        if fault_end_ts is None:
            fault_end_ts = fault_end_holder["ts"] or time.time()

    obs_end_ts = time.time()
    sampler.sample()

    final_drops = 0
    if os.path.exists(chaos_log):
        with open(chaos_log, "r", encoding="utf-8", errors="ignore") as fh:
            matches = re.findall(r"FINAL_DROPS=(\d+)", fh.read())
            if matches:
                final_drops = int(matches[-1])
    total_drops = max(sampler.max_drops, final_drops)

    print(f"[Lauf {run_idx}] Post-Recovery-Stabilisierung ({args.post_wait}s)...", flush=True)
    run_interval_loop(args.post_wait, args.poll_interval, sampler)
    sampler.sample()
    post_wait_end_ts = time.time()

    swap_in_end, swap_out_end = read_vmstat_swap()
    d_in, d_out = swap_in_end - swap_in_start, swap_out_end - swap_out_start
    swap_pure = (d_in == 0 and d_out == 0)

    degraded_start = fault_detected_ts or first_non_normal_ts
    dwell_sec = round(max(0.0, recovered_ts - degraded_start), 3) if (not is_control and degraded_start is not None and recovered_ts is not None) else None
    mttr_sec = round(max(0.0, recovered_ts - fault_end_ts), 3) if (not is_control and recovered_ts is not None) else None
    recovery_end_ts = (recovered_ts if recovered_ts is not None else obs_end_ts) if not is_control else fault_end_ts

    csv_name = f"metrics_{batch_id}_run{run_idx}.csv"
    sample_count, stats, phase_stats = export_csv(
        prom_url, baseline_start_ts, post_wait_end_ts, fault_start_ts, fault_end_ts,
        recovery_end_ts, sampler.samples, os.path.join(args.out_dir, csv_name),
    )
    dwell_str = f"{dwell_sec}s" if dwell_sec is not None else "-"
    mttr_str = f"{mttr_sec}s" if mttr_sec is not None else "-"
    print(f"[Lauf {run_idx}] Dwell={dwell_str} | MTTR_post={mttr_str} | Drops={total_drops} | Swap={'PASS' if swap_pure else f'FAIL({d_in}/{d_out})'}", flush=True)

    ts_map = {
        "baseline_start": baseline_start_ts, "fault_start": fault_start_ts,
        "first_non_normal": first_non_normal_ts, "fault_detected": fault_detected_ts,
        "fault_end": fault_end_ts, "recovered": recovered_ts, "post_wait_end": post_wait_end_ts,
    }
    return {
        "run_index": run_idx, "is_control": is_control, "csv_file": csv_name,
        "sample_count": sample_count, "kernel_drops_total": total_drops,
        "timestamps_unix": ts_map,
        "timestamps_utc": {k: utc_iso(v) for k, v in ts_map.items()},
        "k8s_server_timestamps": {"first_fault_condition_ts": first_fault_cond_ts, "recovered_reconciled_at": recovered_reconciled_at},
        "actual_fault_duration_sec": round(fault_end_ts - fault_start_ts, 3),
        "dwell_time_sec": dwell_sec, "mttr_post_fault_sec": mttr_sec,
        "first_fault_state": first_fault_state, "transitions": transitions,
        "swap_purity": {"delta_pswpin": d_in, "delta_pswpout": d_out, "passed": swap_pure},
        "prometheus_stats": stats, "phase_stats": phase_stats,
    }


def build_aggregate(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    def peaks(metric: str, scale: float = 1.0) -> List[float]:
        return [r["prometheus_stats"][metric]["max"] * scale for r in runs if (r.get("prometheus_stats") or {}).get(metric, {}).get("count", 0) > 0]

    return {
        "runs_total": len(runs),
        "runs_recovered": sum(1 for r in runs if (r.get("timestamps_unix") or {}).get("recovered") is not None),
        "runs_swap_pure": sum(1 for r in runs if (r.get("swap_purity") or {}).get("passed")),
        "dwell_time_sec": compute_stats([r["dwell_time_sec"] for r in runs if r["dwell_time_sec"] is not None]),
        "mttr_post_fault_sec": compute_stats([r["mttr_post_fault_sec"] for r in runs if r["mttr_post_fault_sec"] is not None]),
        "peak_reconcile_p95_sec": compute_stats(peaks("reconcile_duration_p95_sec")),
        "peak_reconcile_avg_sec": compute_stats(peaks("reconcile_duration_avg_sec")),
        "peak_git_duration_p95_sec": compute_stats(peaks("git_request_duration_p95_sec")),
        "peak_pod_tcp_retrans_rate": compute_stats(peaks("pod_tcp_retrans_rate")),
        "peak_iptables_drop_rate": compute_stats(peaks("iptables_drop_rate")),
        "peak_controller_memory_mib": compute_stats(peaks("controller_memory_bytes", 1.0 / (1024 * 1024))),
        "peak_repo_server_memory_mib": compute_stats(peaks("repo_server_memory_bytes", 1.0 / (1024 * 1024))),
        "peak_monitoring_memory_mib": compute_stats(peaks("monitoring_memory_bytes", 1.0 / (1024 * 1024))),
        "peak_controller_cpu_mcores": compute_stats(peaks("controller_cpu_cores", 1000.0)),
        "peak_repo_server_cpu_mcores": compute_stats(peaks("repo_server_cpu_cores", 1000.0)),
        "max_cpu_steal_rate": compute_stats(peaks("cpu_steal_rate")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Autarker On-Node-Messlaeufer (EC2).")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--probability", type=float, default=0.80)
    parser.add_argument("--baseline-wait", type=int, default=30)
    parser.add_argument("--post-wait", type=int, default=90)
    parser.add_argument("--cooldown", type=int, default=60)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--post-fault-timeout", type=int, default=360)
    parser.add_argument("--app-name", default="baseline-workload-app")
    parser.add_argument("--prometheus-port", type=int, default=30090)
    parser.add_argument("--no-drift", action="store_true")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    def handle_sig(signum, _frame):
        cleanup_iptables()
        if not args.no_drift:
            set_workload_label("baseline-reconciliation")
        sys.exit(128 + signum)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        prom_url = discover_prometheus_url(args.prometheus_port)
        env_info = collect_env_info(prom_url)
        print(f"Batch {args.batch_id} | Runs={args.runs} | p={args.probability:.2f} | Prom={prom_url}", flush=True)

        runs: List[Dict[str, Any]] = []
        for idx in range(1, args.runs + 1):
            runs.append(execute_single_run(idx, args.runs, args, prom_url, args.batch_id))
            if idx < args.runs and args.cooldown > 0:
                print(f"[COOLDOWN] Warte {args.cooldown}s vor Lauf {idx + 1}...", flush=True)
                time.sleep(args.cooldown)

        summary = {
            "batch_id": args.batch_id, "generated_at_utc": utc_iso(time.time()), "environment": env_info,
            "config": {
                "runs": args.runs, "duration_sec": args.duration, "probability": args.probability,
                "drift_injected": not args.no_drift, "baseline_wait_sec": args.baseline_wait,
                "post_wait_sec": args.post_wait, "cooldown_sec": args.cooldown,
                "poll_interval_sec": args.poll_interval, "app_name": args.app_name,
            },
            "aggregate": build_aggregate(runs), "runs": runs,
        }
        summary_path = os.path.join(args.out_dir, f"summary_{args.batch_id}.json")
        with open(summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
        with open(os.path.join(args.out_dir, "DONE"), "w", encoding="utf-8") as fh:
            fh.write(f"OK {utc_iso(time.time())}\n")
        print(f"\n[OK] Batch {args.batch_id} abgeschlossen: {summary_path}", flush=True)
    except Exception as exc:
        cleanup_iptables()
        if not args.no_drift:
            set_workload_label("baseline-reconciliation")
        with open(os.path.join(args.out_dir, "FAILED"), "w", encoding="utf-8") as fh:
            fh.write(f"FEHLER: {exc}\n")
        print(f"\n[FEHLER] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    finally:
        subprocess.run(["chown", "-R", "ubuntu:ubuntu", args.out_dir], check=False)


if __name__ == "__main__":
    main()
