#!/usr/bin/env python3
"""Drive pg_stat_statements into repeated entry_dealloc / gc_qtexts cycles.

Workers:
  big     - SELECT ... WHERE id IN (<~50k literals>) with a different list
            length on every call, so every execution is a brand new queryid
            with a ~300 KB normalised text (the pattern from the blog post).
  filler  - small "normal app" queries with varying IN-list lengths.  These
            keep the hash table full (forcing entry_dealloc) and keep
            mean_query_len low, which makes need_gc_qtexts() fire sooner.
  probe   - a Patroni-style health check with statement_timeout=2000,
            recording its latency so the stall caused by GC is visible.

Runs until the server log contains --target-gc gc_qtexts events, or until
--max-seconds elapses.
"""
import argparse
import csv
import random
import re
import sys
import threading
import time

import psycopg2

GC_RE = re.compile(r"pg_stat_statements gc_qtexts: duration=")

stop = threading.Event()
counters_lock = threading.Lock()
counters = {"big": 0, "filler": 0, "probe": 0, "probe_timeouts": 0, "errors": 0}


def bump(key, n=1):
    with counters_lock:
        counters[key] += n


def connect(dsn, **settings):
    conn = psycopg2.connect(dsn, application_name=settings.pop("application_name", "pgss_gc_rig"))
    conn.autocommit = True
    with conn.cursor() as cur:
        for k, v in settings.items():
            cur.execute("SELECT set_config(%s, %s, false)", (k, str(v)))
    return conn


def big_worker(dsn, min_params, max_params, rows):
    conn = connect(dsn, application_name="rig_big")
    ids = [str(random.randint(1, rows)) for _ in range(max_params)]
    with conn.cursor() as cur:
        while not stop.is_set():
            n = random.randint(min_params, max_params)
            sql = "SELECT id, status FROM orders WHERE id IN (" + ",".join(ids[:n]) + ")"
            try:
                cur.execute(sql)
                cur.fetchall()
                bump("big")
            except psycopg2.Error as e:
                bump("errors")
                print(f"big worker error: {e}".strip(), file=sys.stderr)
    conn.close()


def filler_worker(dsn, rows):
    conn = connect(dsn, application_name="rig_filler")
    statuses = ["'new'", "'paid'", "'shipped'", "'cancelled'", "'returned'"]
    with conn.cursor() as cur:
        while not stop.is_set():
            # ~100 * 100 * 5 distinct shapes -> plenty of churn for max=5000
            a = random.randint(1, 100)
            b = random.randint(1, 100)
            c = random.randint(1, 5)
            sql = (
                "SELECT id FROM orders WHERE id IN ("
                + ",".join(str(random.randint(1, rows)) for _ in range(a))
                + ") OR customer_id IN ("
                + ",".join(str(random.randint(1, 1000)) for _ in range(b))
                + ") AND status IN ("
                + ",".join(statuses[:c])
                + ") LIMIT 1"
            )
            try:
                cur.execute(sql)
                cur.fetchall()
                bump("filler")
            except psycopg2.Error as e:
                bump("errors")
                print(f"filler worker error: {e}".strip(), file=sys.stderr)
    conn.close()


def probe_worker(dsn, interval, csv_path):
    # Mirrors Patroni < 4.1.4: hardcoded 2s statement_timeout, and the session
    # is tracked by pg_stat_statements like any other.
    conn = connect(dsn, application_name="rig_probe", statement_timeout=2000)
    with open(csv_path, "w", newline="") as f, conn.cursor() as cur:
        w = csv.writer(f)
        w.writerow(["epoch", "latency_ms", "status"])
        while not stop.is_set():
            t0 = time.time()
            status = "ok"
            try:
                cur.execute("SELECT pg_is_in_recovery(), pg_current_wal_lsn(), now()")
                cur.fetchall()
            except psycopg2.errors.QueryCanceled:
                status = "timeout"
                bump("probe_timeouts")
            except psycopg2.Error as e:
                status = "error"
                print(f"probe error: {e}".strip(), file=sys.stderr)
            latency = (time.time() - t0) * 1000
            w.writerow([f"{t0:.6f}", f"{latency:.3f}", status])
            f.flush()
            bump("probe")
            time.sleep(max(0.0, interval - latency / 1000))
    conn.close()


def count_gc(log_path):
    try:
        with open(log_path, errors="replace") as f:
            return sum(1 for line in f if GC_RE.search(line))
    except FileNotFoundError:
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--log", required=True, help="server log file to watch for gc_qtexts events")
    ap.add_argument("--probe-csv", required=True)
    ap.add_argument("--target-gc", type=int, default=10)
    ap.add_argument("--max-seconds", type=int, default=1800)
    ap.add_argument("--big-workers", type=int, default=4)
    ap.add_argument("--filler-workers", type=int, default=4)
    ap.add_argument("--min-params", type=int, default=49000)
    ap.add_argument("--max-params", type=int, default=50000)
    ap.add_argument("--probe-interval", type=float, default=0.05)
    ap.add_argument("--rows", type=int, default=100000)
    args = ap.parse_args()

    baseline = count_gc(args.log)
    threads = [threading.Thread(target=probe_worker, args=(args.dsn, args.probe_interval, args.probe_csv))]
    threads += [threading.Thread(target=big_worker, args=(args.dsn, args.min_params, args.max_params, args.rows))
                for _ in range(args.big_workers)]
    threads += [threading.Thread(target=filler_worker, args=(args.dsn, args.rows))
                for _ in range(args.filler_workers)]
    for t in threads:
        t.daemon = True
        t.start()

    start = time.time()
    gc_seen = 0
    try:
        while True:
            time.sleep(5)
            gc_seen = count_gc(args.log) - baseline
            elapsed = time.time() - start
            with counters_lock:
                c = dict(counters)
            print(f"[{elapsed:6.0f}s] gc_qtexts={gc_seen}/{args.target_gc} big={c['big']} "
                  f"filler={c['filler']} probe={c['probe']} probe_timeouts={c['probe_timeouts']} "
                  f"errors={c['errors']}", flush=True)
            if gc_seen >= args.target_gc:
                break
            if elapsed > args.max_seconds:
                print(f"Gave up after {args.max_seconds}s with only {gc_seen} gc_qtexts events", file=sys.stderr)
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)

    return 0 if gc_seen >= args.target_gc else 1


if __name__ == "__main__":
    sys.exit(main())
