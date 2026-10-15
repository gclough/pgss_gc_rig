#!/usr/bin/env bash
# pg_stat_statements garbage-collection test rig.
#
#   1. builds (once) PostgreSQL with the pg_stat_statements.log_gc patch
#   2. initdb's a throwaway cluster with log_gc = on
#   3. hammers it with huge, ever-changing IN-list queries until at least
#      TARGET_GC gc_qtexts events are in the log
#   4. prints a table of timestamp / GC duration
#
# Tunables (env vars): PGSS_MAX TARGET_GC MAX_SECONDS BIG_WORKERS FILLER_WORKERS
#                      MIN_PARAMS MAX_PARAMS ROWS PORT KEEP_RUNNING
set -euo pipefail

RIG_DIR="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${RIG_DIR}/pg"
RUN_DIR="${RUN_DIR:-${RIG_DIR}/run}"
DATA="${RUN_DIR}/data"
LOG="${DATA}/log/postgresql.log"
PROBE_CSV="${RUN_DIR}/probe.csv"

PORT="${PORT:-55432}"
PGSS_MAX="${PGSS_MAX:-5000}"
TARGET_GC="${TARGET_GC:-10}"
MAX_SECONDS="${MAX_SECONDS:-1800}"
BIG_WORKERS="${BIG_WORKERS:-4}"
FILLER_WORKERS="${FILLER_WORKERS:-4}"
MIN_PARAMS="${MIN_PARAMS:-49000}"
MAX_PARAMS="${MAX_PARAMS:-50000}"
ROWS="${ROWS:-100000}"
KEEP_RUNNING="${KEEP_RUNNING:-0}"

"${RIG_DIR}/build.sh"

PG_CTL="${PREFIX}/bin/pg_ctl"
PSQL=("${PREFIX}/bin/psql" -X -q -v ON_ERROR_STOP=1 -h "${RUN_DIR}" -p "${PORT}" -d postgres)
DSN="host=${RUN_DIR} port=${PORT} dbname=postgres"

stop_server() {
    if [[ "${KEEP_RUNNING}" != 1 ]] && "${PG_CTL}" -D "${DATA}" status > /dev/null 2>&1; then
        "${PG_CTL}" -D "${DATA}" -m fast -w stop > /dev/null
    fi
}
trap stop_server EXIT

# Fresh cluster every run so the log only contains this run's events.
if [[ -d "${DATA}" ]]; then
    "${PG_CTL}" -D "${DATA}" -m immediate stop > /dev/null 2>&1 || true
    rm -rf "${DATA}"
fi
mkdir -p "${RUN_DIR}"
"${PREFIX}/bin/initdb" -D "${DATA}" -A trust -U "$(id -un)" > "${RUN_DIR}/initdb.log"

cat >> "${DATA}/postgresql.conf" <<EOF

# ---- pgss_gc_rig ----
listen_addresses = ''
port = ${PORT}
unix_socket_directories = '${RUN_DIR}'
max_connections = 50
shared_buffers = 128MB

shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.max = ${PGSS_MAX}
pg_stat_statements.track = all
pg_stat_statements.log_gc = on       # patched GUC: log entry_dealloc/gc_qtexts duration

logging_collector = on
log_directory = 'log'
log_filename = 'postgresql.log'
log_line_prefix = '%m [%p] '
log_min_error_statement = panic      # don't echo 300 KB statements into the log
EOF

"${PG_CTL}" -D "${DATA}" -l "${RUN_DIR}/startup.log" -w start > /dev/null

"${PSQL[@]}" <<EOF
CREATE EXTENSION pg_stat_statements;
CREATE TABLE orders (id bigint PRIMARY KEY, customer_id int, status text);
INSERT INTO orders
SELECT g, (random() * 1000)::int,
       (ARRAY['new','paid','shipped','cancelled','returned'])[1 + (random() * 4)::int]
FROM generate_series(1, ${ROWS}) g;
CREATE INDEX ON orders (customer_id);
ANALYZE orders;
EOF

echo "Cluster up (port ${PORT}, pg_stat_statements.max=${PGSS_MAX}); generating load until ${TARGET_GC} gc_qtexts events..."
set +e
python3 "${RIG_DIR}/loadgen.py" --dsn "${DSN}" --log "${LOG}" --probe-csv "${PROBE_CSV}" \
    --target-gc "${TARGET_GC}" --max-seconds "${MAX_SECONDS}" \
    --big-workers "${BIG_WORKERS}" --filler-workers "${FILLER_WORKERS}" \
    --min-params "${MIN_PARAMS}" --max-params "${MAX_PARAMS}" --rows "${ROWS}"
rc=$?
set -e

"${PSQL[@]}" -c "SELECT dealloc, stats_reset FROM pg_stat_statements_info"

python3 "${RIG_DIR}/report.py" --log "${LOG}" --probe-csv "${PROBE_CSV}" | tee "${RUN_DIR}/report.txt"
echo
echo "Server log: ${LOG}"
echo "Report:     ${RUN_DIR}/report.txt"
exit "${rc}"
