#!/usr/bin/env bash
set -euo pipefail

echo "[airflow] waiting for metadata database..."

python - <<'PY'
import time
import psycopg

dsn = "host=postgres port=5432 dbname=airflow_meta user=airflow password=airflow"
for _attempt in range(60):
    try:
        with psycopg.connect(dsn):
            print("[airflow] metadata database is ready")
            break
    except Exception:
        time.sleep(2)
else:
    raise SystemExit("[airflow] metadata database is not reachable")
PY

airflow db migrate

airflow users create \
  --username "${_AIRFLOW_WWW_USER_USERNAME:-admin}" \
  --password "${_AIRFLOW_WWW_USER_PASSWORD:-admin123}" \
  --firstname Admin \
  --lastname User \
  --role Admin \
  --email admin@example.com || true

airflow scheduler &

exec airflow webserver
