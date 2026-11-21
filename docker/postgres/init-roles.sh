#!/usr/bin/env bash
# Create the four roles and the databases the pipeline expects.
#
# Used two ways:
#   - by the postgres image on first start (mounted into
#     /docker-entrypoint-initdb.d/), connecting over the local socket;
#   - by CI against the service container, with PGHOST/PGUSER/PGPASSWORD set.
#
# Passwords come from the environment and reach Postgres as psql variables
# (:'name'), so a password is quoted by psql and never spliced into SQL.
# Table-level grants are not made here: they are migration 0013's.
set -euo pipefail

: "${MOS_OWNER_PASSWORD:?}" "${MOS_PIPELINE_PASSWORD:?}" "${MOS_NOCODB_PASSWORD:?}" "${MOS_TRIGGER_PASSWORD:?}"
MOS_DATABASES="${MOS_DATABASES:-mosferics mosferics_test}"

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-${PGUSER:-postgres}}" --dbname postgres \
    -v owner="$MOS_OWNER_PASSWORD" -v pipeline="$MOS_PIPELINE_PASSWORD" \
    -v nocodb="$MOS_NOCODB_PASSWORD" -v trigger="$MOS_TRIGGER_PASSWORD" <<'SQL'
CREATE ROLE mos_owner    LOGIN PASSWORD :'owner';     -- runs migrations
CREATE ROLE mos_pipeline LOGIN PASSWORD :'pipeline';  -- runs the pipeline
CREATE ROLE mos_nocodb   LOGIN PASSWORD :'nocodb';    -- the review interface
CREATE ROLE mos_trigger  LOGIN PASSWORD :'trigger';   -- a narrow role for a distributed trigger
SQL

for db in $MOS_DATABASES; do
    psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-${PGUSER:-postgres}}" --dbname postgres \
        -c "CREATE DATABASE \"$db\" OWNER mos_owner"
done
