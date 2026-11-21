#!/usr/bin/env bash
# Migrate the test database to head, then run every test against it.
# A skipped test means the database was not reached, so it fails the run,
# as in CI.
set -euo pipefail
DATABASE_URL="$TEST_OWNER_URL" alembic upgrade head
python -m pytest -rs -p no:cacheprovider "$@" | tee /tmp/pytest.log
if grep -qE '[0-9]+ skipped' /tmp/pytest.log; then
    echo "tests were skipped: the database tests did not run" >&2
    exit 1
fi
