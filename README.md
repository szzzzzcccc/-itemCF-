# Movie Recommendation System

A full-stack movie recommendation system built on top of `MovieLens 20M` and `TMDb`.

The project covers the complete recommendation workflow, including data ingestion, candidate generation, ranking, reranking, online serving, and offline scheduling. It combines recommender-system logic with a deployable engineering stack so that the application can be started locally and used directly.

## Features

- Multi-channel retrieval:
  - Popular retrieval
  - Genre-preference retrieval
  - Long-tail exploration retrieval
  - ItemCF retrieval
  - Two-Tower retrieval
- Ranking with `LightGBM Ranker`
- Diversity-aware reranking with `MMR`
- Online application with login, search, movie detail pages, rating history, rating updates, and personalized recommendations
- Offline workflow orchestration with `Airflow`

## Tech Stack

- Frontend: Vue 3 (CDN) + Vue Router + Nginx
- Backend: FastAPI
- Database: PostgreSQL
- Offline pipeline: Airflow
- Models: ItemCF + Two-Tower + LightGBM Ranker + MMR
- Deployment: Docker Compose

## Repository Structure

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

## Included Assets

This repository includes the files required for direct local startup:

- `backend/app/artifacts_runtime/`
  trained Two-Tower and LightGBM artifacts
- `demo_data/`
  preloaded database seed files used for local initialization
- `frontend/static/data/movies.json`
  frontend movie display data

The repository is designed so that a fresh local setup does not require re-downloading the original full raw dataset or retraining models before the first run.

## Quick Start

### 1. Start all services

```powershell
docker compose up --build -d
```

On the first startup, PostgreSQL automatically imports the bundled seed data from `demo_data/`.

### 2. Open the application

- Frontend: [http://127.0.0.1:5174](http://127.0.0.1:5174)
- Backend health check: [http://127.0.0.1:8002/health](http://127.0.0.1:8002/health)
- Airflow: [http://127.0.0.1:8081](http://127.0.0.1:8081)

Airflow default account:

- Username: `admin`
- Password: `admin123`

## Test Accounts

- `user101 / pass101`
- `user202 / pass202`
- `user303 / pass303`
- `user000 / pass000`

## Core Pipeline

### Retrieval

- Popular retrieval provides a global fallback pool
- Genre-preference retrieval expands candidates based on user taste
- Long-tail retrieval introduces less popular but relevant items
- ItemCF retrieval uses item-item similarity from interaction history
- Two-Tower retrieval matches user and movie embeddings, with Faiss-based ANN support in the online service

### Ranking

- `LightGBM Ranker` scores merged candidates using retrieval signals, user preference features, and movie attributes

### Reranking

- `MMR` is applied to improve result diversity while preserving relevance

## Offline Workflow

The repository includes an Airflow pipeline for offline processing:

- incremental rating event processing
- sparse recall rebuilding
- Two-Tower training
- LightGBM training-sample generation
- LightGBM training

## Utility Scripts

- `scripts/build_recalls.ps1`
- `scripts/build_twotower.ps1`
- `scripts/build_lgb_ranker.ps1`
- `scripts/prepare_frontend_data.ps1`
- `scripts/start_airflow.ps1`

## Rebuild Local State

To rebuild the local database from the bundled seed files:

```powershell
docker compose down -v
docker compose up --build -d
```

## Notes

- The project includes preloaded seed data and trained artifacts for direct local use.
- The original raw training dataset and large intermediate processing outputs are not required for the first run.
