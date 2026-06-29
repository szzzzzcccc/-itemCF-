from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator


COMMON_ENV = {
    "PYTHONPATH": "/opt/airflow/app",
    "BACKEND_INTERNAL_API_URL": "http://backend:8000",
}


with DAG(
    dag_id="movie_rec_incremental_events_pipeline",
    description="Frequent incremental pipeline for rating events and popular recall refresh.",
    start_date=datetime(2026, 6, 27),
    schedule=None,
    catchup=False,
    default_args={
        "owner": "codex",
        "depends_on_past": False,
        "retries": 1,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["movie-rec", "incremental", "events"],
) as incremental_dag:
    process_incremental_rating_events = BashOperator(
        task_id="process_incremental_rating_events",
        bash_command=(
            "cd /opt/airflow/app && "
            "python -u run_remote_job.py --job-name process_incremental_rating_events"
        ),
        env=COMMON_ENV,
    )


with DAG(
    dag_id="movie_rec_daily_offline_pipeline",
    description="Daily offline pipeline for sparse ItemCF, two-tower training, and ranker training.",
    start_date=datetime(2026, 6, 25),
    schedule=None,
    catchup=False,
    default_args={
        "owner": "codex",
        "depends_on_past": False,
        "retries": 1,
        "retry_delay": timedelta(minutes=15),
    },
    tags=["movie-rec", "offline", "daily"],
) as dag:
    process_incremental_rating_events_daily = BashOperator(
        task_id="process_incremental_rating_events",
        bash_command=(
            "cd /opt/airflow/app && "
            "python -u run_remote_job.py --job-name process_incremental_rating_events"
        ),
        env=COMMON_ENV,
    )

    build_recalls = BashOperator(
        task_id="build_sparse_recalls",
        bash_command=(
            "cd /opt/airflow/app && "
            "python -u run_remote_job.py --job-name build_sparse_recalls"
        ),
        env=COMMON_ENV,
    )

    train_twotower = BashOperator(
        task_id="train_twotower",
        bash_command=(
            "cd /opt/airflow/app && "
            "python -u run_remote_job.py --job-name train_twotower"
        ),
        env=COMMON_ENV,
    )

    build_lgb_training_samples = BashOperator(
        task_id="build_lgb_training_samples",
        bash_command=(
            "cd /opt/airflow/app && "
            "python -u run_remote_job.py --job-name build_lgb_training_samples"
        ),
        env=COMMON_ENV,
    )

    train_lgb_ranker = BashOperator(
        task_id="train_lgb_ranker",
        bash_command=(
            "cd /opt/airflow/app && "
            "python -u run_remote_job.py --job-name train_lgb_ranker"
        ),
        env=COMMON_ENV,
    )

    process_incremental_rating_events_daily >> build_recalls >> train_twotower >> build_lgb_training_samples >> train_lgb_ranker
