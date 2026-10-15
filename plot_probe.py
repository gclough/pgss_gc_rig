#!/usr/bin/env python3
"""Plot health-probe latency over time with pg_stat_statements gc_qtexts events.

    python3 plot_probe.py --log run/data/log/postgresql.log --probe-csv run/probe.csv \
        --out probe_latency.png
"""
import argparse
import csv
import re
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

GC_RE = re.compile(
    r"^(\S+ \S+) (\w+) \[\d+\] LOG:  pg_stat_statements gc_qtexts: "
    r"duration=([\d.]+) ms old_size=(\d+) new_size=(\d+) entries=(\d+)(.*)$")
TZ_OFFSETS = {"CDT": -5, "CST": -6, "UTC": 0, "GMT": 0}

SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
SERIES = "#2a78d6"
TIMEOUT_RED = "#d03b3b"
GC_MARK = "#6b6a66"


def read_gcs(path):
    gcs = []
    with open(path) as f:
        for line in f:
            m = GC_RE.match(line)
            if not m:
                continue
            ts, tz, dur, old, _new, _entries, rest = m.groups()
            end = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f").replace(
                tzinfo=timezone(timedelta(hours=TZ_OFFSETS[tz])))
            gcs.append({"end": end.timestamp(), "dur_ms": float(dur),
                        "old_bytes": int(old), "failed": "FAILED" in rest})
    return gcs


def fmt_size(b):
    return f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--probe-csv", required=True)
    ap.add_argument("--out", default="probe_latency.png")
    ap.add_argument("--width-px", type=int, default=800)
    ap.add_argument("--scale", type=int, default=1, help="2 for a retina @2x image")
    ap.add_argument("--timeout-ms", type=float, default=2000)
    args = ap.parse_args()

    with open(args.probe_csv) as f:
        rows = list(csv.DictReader(f))
    t0 = float(rows[0]["epoch"])
    xs = [(float(r["epoch"]) - t0) / 60 for r in rows]
    # latency is measured from the probe's start, so plot it where it finished
    xs = [x + float(r["latency_ms"]) / 60000 for x, r in zip(xs, rows)]
    ys = [float(r["latency_ms"]) for r in rows]
    gcs = read_gcs(args.log)

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10,
        "axes.edgecolor": GRID, "axes.labelcolor": TEXT_SECONDARY,
        "xtick.color": TEXT_SECONDARY, "ytick.color": TEXT_SECONDARY,
    })
    dpi = 100 * args.scale
    fig, ax = plt.subplots(figsize=(args.width_px / 100, 4.6), dpi=dpi)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    ymax = max(max(ys), args.timeout_ms) * 1.22
    ax.set_ylim(0, ymax)
    ax.set_xlim(0, max(xs))

    for g in gcs:
        start = (g["end"] - g["dur_ms"] / 1000 - t0) / 60
        end = (g["end"] - t0) / 60
        ax.axvspan(start, end, color=GC_MARK, alpha=0.18, lw=0, zorder=1)
        ax.axvline(start, color=GC_MARK, lw=1, ls=(0, (3, 3)), zorder=1)

    ax.plot(xs, ys, color=SERIES, lw=1, zorder=3, solid_joinstyle="round")

    ax.axhline(args.timeout_ms, color=TIMEOUT_RED, lw=2, zorder=4)
    ax.text(0.4, args.timeout_ms + ymax * 0.012,
            f"Patroni health-check timeout ({args.timeout_ms / 1000:g} s)",
            color=TEXT_PRIMARY, fontsize=9, fontweight="bold", va="bottom", zorder=5)

    # Label each GC beside its marker; GCs closer than ~1.5 min share a label,
    # and a label too close to the previous one drops down so they don't collide.
    labels = []
    for i, g in enumerate(gcs, 1):
        x = (g["end"] - g["dur_ms"] / 1000 - t0) / 60
        text = f"GC #{i}\n{g['dur_ms'] / 1000:.1f} s\n{fmt_size(g['old_bytes'])}"
        if g["failed"]:
            text = f"GC #{i}\nOOM: texts\ndiscarded\n{fmt_size(g['old_bytes'])}"
        if labels and x - labels[-1][0] < 1.5:
            labels[-1][1] += f"\n\nGC #{i}\n{g['dur_ms'] / 1000:.1f} s"
            continue
        labels.append([x, text])
    prev_x, y = None, ymax * 0.97
    for x, text in labels:
        y = y - ymax * 0.24 if prev_x is not None and x - prev_x < 2.5 else ymax * 0.97
        prev_x = x
        ax.text(x + 0.25, y, text, color=TEXT_SECONDARY, fontsize=8,
                va="top", ha="left", linespacing=1.15, zorder=5)

    ax.set_xlabel("Minutes since load started")
    ax.set_ylabel("Health-check latency (ms)")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.grid(axis="y", color=GRID, lw=0.8, zorder=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)

    fig.suptitle("Health-check latency during pg_stat_statements garbage collection",
                 x=0.012, ha="left", fontsize=12.5, fontweight="bold", color=TEXT_PRIMARY)
    fig.text(0.012, 0.915,
             "Probe every 50 ms. Shaded bands are gc_qtexts runs holding the exclusive "
             "pgss lock (pg_stat_statements.max=100000).",
             ha="left", fontsize=8.5, color=TEXT_SECONDARY)
    fig.tight_layout(rect=(0, 0, 1, 0.925))
    fig.savefig(args.out, dpi=dpi, facecolor=SURFACE)
    print(args.out)


if __name__ == "__main__":
    main()
