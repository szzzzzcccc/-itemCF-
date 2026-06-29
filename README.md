# Movie Recommendation Demo

Demo-ready movie recommendation system for internship interviews.

This repository is a cleaned and runnable demo version of the project. It keeps:

- the online app
- the trained model artifacts
- the precomputed recall tables
- the frontend movie display data
- a lightweight demo database seed

It does **not** require re-downloading MovieLens 20M or re-training models before the first run.

## Stack

- Frontend: Vue 3 (CDN) + Vue Router + Nginx
- Backend: FastAPI
- Database: PostgreSQL
- Offline pipeline: Airflow
- Models: ItemCF + Two-Tower + LightGBM Ranker + MMR

## What is included

- `backend/app/artifacts_runtime/`
  pre-trained Two-Tower and LightGBM artifacts for demo use
- `demo_data/`
  database seed files required for direct local startup
- `frontend/static/data/movies.json`
  frontend display data

## Project structure

```text
.
├─airflow/
├─backend/
├─db/
├─demo_data/
├─frontend/
├─scripts/
├─docker-compose.yml
└─README.md
```

## Quick start

### 1. Start all services

```powershell
docker compose up --build -d
```

On the first startup, PostgreSQL will automatically import the demo seed from `demo_data/`.

### 2. Open the app

- Frontend: [http://127.0.0.1:5174](http://127.0.0.1:5174)
- Backend health: [http://127.0.0.1:8002/health](http://127.0.0.1:8002/health)
- Airflow: [http://127.0.0.1:8081](http://127.0.0.1:8081)

Airflow default account:

- username: `admin`
- password: `admin123`

## Demo accounts

- `user101 / pass101`
- `user202 / pass202`
- `user303 / pass303`
- `user000 / pass000`

## Optional scripts

- `scripts/build_recalls.ps1`
- `scripts/build_twotower.ps1`
- `scripts/build_lgb_ranker.ps1`
- `scripts/prepare_frontend_data.ps1`
- `scripts/start_airflow.ps1`

## Reset the demo database

If you want to rebuild the demo database from the bundled seed files:

```powershell
docker compose down -v
docker compose up --build -d
```

## Notes

- This repository is optimized for **direct local demo**.
- It intentionally includes demo artifacts and seed data.
- It intentionally excludes the original raw training dataset and large intermediate processing files.
