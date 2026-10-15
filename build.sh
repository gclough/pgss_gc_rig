#!/usr/bin/env bash
# Build a private PostgreSQL install whose pg_stat_statements has the
# pg_stat_statements.log_gc GUC (see pgss_log_gc.patch).
#
# Stock pg_stat_statements has no way to log how long entry_dealloc() or
# gc_qtexts() take, and both are inlined in the PGDG build so they can't be
# uprobed either.  The patch only adds timing + ereport(LOG); behaviour is
# otherwise identical to upstream.
set -euo pipefail

RIG_DIR="$(cd "$(dirname "$0")" && pwd)"
PG_VERSION="${PG_VERSION:-17.6}"
BUILD_DIR="${RIG_DIR}/build"
PREFIX="${PREFIX:-${RIG_DIR}/pg}"
JOBS="${JOBS:-$(nproc)}"

TARBALL="postgresql-${PG_VERSION}.tar.bz2"
URL="https://ftp.postgresql.org/pub/source/v${PG_VERSION}/${TARBALL}"
SRC="${BUILD_DIR}/postgresql-${PG_VERSION}"

if [[ -x "${PREFIX}/bin/pg_config" && -f "$("${PREFIX}/bin/pg_config" --pkglibdir)/pg_stat_statements.so" ]]; then
    echo "Already built in ${PREFIX} (remove it to rebuild)"
    exit 0
fi

mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

if [[ ! -f "${TARBALL}" ]]; then
    curl -sSfLO "${URL}"
fi
curl -sSfL "${URL}.sha256" | sha256sum -c -

rm -rf "${SRC}"
tar xjf "${TARBALL}"
cd "${SRC}"
patch -p1 < "${RIG_DIR}/pgss_log_gc.patch"

# Minimal feature set: we only need the server and pg_stat_statements.
./configure --prefix="${PREFIX}" --without-readline --without-icu \
    --without-zlib --without-llvm > configure.log
make -s -j"${JOBS}" > make.log
make -s install > install.log
make -s -C contrib/pg_stat_statements install >> install.log

echo "Built PostgreSQL ${PG_VERSION} with patched pg_stat_statements in ${PREFIX}"
