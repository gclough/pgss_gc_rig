# pg_stat_statements garbage-collection test rig

Reproduces the workload from
[The Garbage Collection Trap](https://gregclough.com/2026/10/07/the-garbage-collection-trap-how-pg_stat_statements-garbage-collection-triggered-patroni-failovers/)
and measures how long each pg_stat_statements GC holds `pgss->lock` exclusively.

```bash
./run_rig.sh                      # defaults: pg_stat_statements.max=5000, stop after 10 GCs
TARGET_GC=50 BIG_WORKERS=8 ./run_rig.sh
python3 report.py --log run/data/log/postgresql.log --probe-csv run/probe.csv --show-dealloc
```

## Why a patched pg_stat_statements?

Stock PostgreSQL has no setting that logs GC duration. The closest is
`log_min_messages = debug1`, which prints `pgss gc of queries file shrunk size from X to Y`
but no timing, and nothing at all for hash-table eviction. In the PGDG RPM build,
`gc_qtexts()` and `entry_dealloc()` are inlined, so they cannot be uprobed either.

`pgss_log_gc.patch` adds one GUC, `pg_stat_statements.log_gc` (superuser, default off).
When it is on, it logs:

```
LOG:  pg_stat_statements gc_qtexts: duration=45.997 ms old_size=30932162 new_size=15518937 entries=4923
LOG:  pg_stat_statements entry_dealloc: duration=0.706 ms entries=5000 evicted=250 mean_query_len=3104
```

`build.sh` builds PostgreSQL 17.6 (the same version as `/usr/pgsql-17`) with the patch into
`./pg`. The system install is not touched.

## Files

| file | purpose |
|---|---|
| `pgss_log_gc.patch` | adds `pg_stat_statements.log_gc` |
| `build.sh` | download, verify, patch and build PG into `./pg` |
| `run_rig.sh` | initdb a throwaway cluster in `./run`, load it, report, stop it |
| `loadgen.py` | big ~50k-literal `IN` lists (new queryid every call), small filler queries, Patroni-style 2s-timeout probe |
| `report.py` | log + probe CSV -> table |

## Reading the results

* **gc_qtexts** rewrites the whole query-text file while holding the exclusive lock. This
  is the stall the blog post describes: every backend blocks behind it, including ones
  that only need the shared lock to update an existing entry. `worst_probe_ms` shows what
  the health check experienced during that window. `-` means no probe sample overlapped
  the GC.
* **entry_dealloc** runs every time a new queryid arrives while the table is full. Each run
  is cheap, but it runs constantly.
* GC cost scales with the text-file size, which is about `2 * pg_stat_statements.max * mean_query_len`
  at trigger time. With mostly small filler queries, the file stays around 30 MB and GC takes
  tens of ms. When big queries dominate the table (`FILLER_WORKERS=0`), the file can reach
  several GB before GC fires. On a box with limited RAM that can make `gc_qtexts` fail and
  discard every query text (logged as `FAILED`).
