#!/usr/bin/env python3
"""Turn pg_stat_statements log_gc lines into a table.

For every gc_qtexts event prints the timestamp, GC duration, text file size
before/after, and the worst health-probe latency seen while it ran.  Also
summarises the (much more frequent) entry_dealloc events.
"""
import argparse
import csv
import re
import statistics
from datetime import datetime

# log_line_prefix = '%m [%p] '  ->  2026-10-15 03:50:01.123 BST [12345] LOG:  ...
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) (?P<tz>\S+) \[(?P<pid>\d+)\] LOG:  "
    r"pg_stat_statements (?P<kind>gc_qtexts|entry_dealloc): duration=(?P<ms>[\d.]+) ms (?P<rest>.*)$"
)


def parse_log(path):
    events = []
    with open(path, errors="replace") as f:
        for line in f:
            m = LINE_RE.match(line)
            if not m:
                continue
            ts = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S.%f")
            detail = dict(kv.split("=", 1) for kv in m["rest"].split() if "=" in kv)
            events.append({
                "ts": ts, "tz": m["tz"], "pid": int(m["pid"]), "kind": m["kind"],
                "ms": float(m["ms"]), "detail": detail, "failed": "FAILED" in m["rest"],
            })
    return events


def load_probe(path):
    if not path:
        return []
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append((float(r["epoch"]), float(r["latency_ms"]), r["status"]))
    return rows


def worst_probe(probe, end_ts, ms):
    """Worst probe overlapping [end - duration, end] (log timestamp is when GC finished)."""
    end = end_ts.timestamp()
    start = end - ms / 1000
    hits = [(lat, st) for t0, lat, st in probe if t0 <= end and t0 + lat / 1000 >= start]
    if not hits:
        return "-", "-"
    lat, _ = max(hits)
    return f"{lat:,.1f}", str(sum(1 for _, st in hits if st == "timeout"))


def mb(v):
    return f"{int(v) / 1048576:,.1f}"


def table(headers, rows):
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    fmt = " | ".join("{:>%d}" % w for w in widths)
    out = [fmt.format(*headers), "-+-".join("-" * w for w in widths)]
    out += [fmt.format(*r) for r in rows]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--probe-csv")
    ap.add_argument("--show-dealloc", action="store_true", help="list every entry_dealloc event too")
    args = ap.parse_args()

    events = parse_log(args.log)
    probe = load_probe(args.probe_csv)
    gcs = [e for e in events if e["kind"] == "gc_qtexts"]
    deallocs = [e for e in events if e["kind"] == "entry_dealloc"]

    print(f"\npg_stat_statements query-text garbage collections (gc_qtexts): {len(gcs)}\n")
    rows = []
    for i, e in enumerate(gcs, 1):
        d = e["detail"]
        worst, timeouts = worst_probe(probe, e["ts"], e["ms"])
        rows.append([
            i, e["ts"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " " + e["tz"], e["pid"],
            f"{e['ms']:,.3f}", mb(d.get("old_size", 0)), mb(d.get("new_size", 0)),
            d.get("entries", "-") + (" FAILED" if e["failed"] else ""), worst, timeouts,
        ])
    print(table(["#", "timestamp (GC end)", "pid", "gc_duration_ms", "file_before_MB",
                 "file_after_MB", "entries", "worst_probe_ms", "probe_timeouts"], rows))

    if gcs:
        ms = [e["ms"] for e in gcs]
        print(f"\ngc_qtexts duration: min {min(ms):,.1f} ms, median {statistics.median(ms):,.1f} ms, "
              f"max {max(ms):,.1f} ms")

    if deallocs:
        ms = [e["ms"] for e in deallocs]
        print(f"\nentry_dealloc (hash table eviction) events: {len(deallocs)}, "
              f"min {min(ms):,.3f} ms, median {statistics.median(ms):,.3f} ms, max {max(ms):,.3f} ms")
        if args.show_dealloc:
            print()
            print(table(["timestamp", "pid", "dealloc_ms", "entries", "evicted", "mean_query_len"],
                        [[e["ts"].strftime("%H:%M:%S.%f")[:-3], e["pid"], f"{e['ms']:,.3f}",
                          e["detail"].get("entries", "-"), e["detail"].get("evicted", "-"),
                          e["detail"].get("mean_query_len", "-")] for e in deallocs]))

    if probe:
        lats = [lat for _, lat, _ in probe]
        timeouts = sum(1 for _, _, st in probe if st == "timeout")
        print(f"\nhealth probe (statement_timeout=2000): {len(probe)} samples, "
              f"max {max(lats):,.1f} ms, timeouts {timeouts}")


if __name__ == "__main__":
    main()
