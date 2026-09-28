<div align="center">

<img src=".github/assets/cover.png" alt="Parcel Scout v3" width="100%">

# Parcel Scout v3

**Property research agent for North Texas: eight source connectors, four county clerk scrapers, ownership resolution, and a map-first UI.**

<p>
<a href="https://ibiraheel.com/p/parcel-scout"><img alt="Case study" src="https://img.shields.io/badge/Case%20study-ibiraheel.com-0b0c10?style=for-the-badge&labelColor=c8f560"></a>
</p>

<p>
<img alt="Python" src="https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white">
<img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white">
<img alt="PostGIS" src="https://img.shields.io/badge/PostGIS-4169E1?style=flat-square&logo=postgresql&logoColor=white">
<img alt="Redis" src="https://img.shields.io/badge/Redis-FF4438?style=flat-square&logo=redis&logoColor=white">
<img alt="SQLAlchemy" src="https://img.shields.io/badge/SQLAlchemy-D71F00?style=flat-square&logo=sqlalchemy&logoColor=white">
<img alt="Next.js" src="https://img.shields.io/badge/Next.js-000000?style=flat-square&logo=nextdotjs&logoColor=white">
<img alt="Docker" src="https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white">
</p>

</div>

<br>

> **8 data sources, 4 counties, one ownership graph**  
> for Equitify, sourcing off-market parcels in Dallas, Tarrant, Collin, and Denton

## What it did

An orchestrator runs appraisal-district, permit, violation, crime, clerk, EPA, and FEMA connectors into PostGIS, resolves owners into entities, and scores opportunity and risk. Address-search pipelines completed for all four counties.

<sub>Outcome: measured.</sub>

## How it works

<p align="center"><img src=".github/assets/architecture.svg" alt="Architecture" width="100%"></p>

1. Every source is a SourceConnector subclass with discover and write phases, so dry runs are free.
2. Sync log table records every run, so a bad scrape is visible and reversible.
3. Ownership resolver links parcels to owners to entities across sources, which is where the value is.
4. PostGIS and Redis in docker-compose; FastAPI routes per domain (owners, filings, debt, risk, export).

## Run it locally

```bash
docker compose up -d                       # PostGIS + Redis
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload          # API on :8000 (run from the repo root)
cd frontend && npm install && npm run dev  # map UI on :3000
```

`DATABASE_URL` defaults to the compose database; set it to point anywhere else.

## Repository layout

```
├── backend/
│   ├── connectors/
│   ├── models/
│   ├── routes/
│   ├── services/
│   ├── __init__.py
│   ├── config.py
│   ├── database.py
│   ├── main.py
│   ├── orchestrator.py
│   └── requirements.txt
├── db/
│   └── init/
├── frontend/
│   ├── public/
│   ├── src/
│   ├── AGENTS.md
│   ├── CLAUDE.md
│   ├── eslint.config.mjs
│   ├── next-env.d.ts
│   ├── next.config.ts
│   ├── package.json
│   ├── postcss.config.mjs
│   ├── README.md
│   ├── tsconfig.json
│   └── tsconfig.tsbuildinfo
├── scrapers/
│   ├── __init__.py
│   ├── collin_address_search_progress.json
│   ├── collin_clerk.py
│   ├── collin_ownership_pipeline.py
│   ├── dallas_address_search_progress.json
│   ├── dallas_clerk.py
│   ├── denton_address_search_progress.json
│   ├── denton_clerk.py
│   ├── tarrant_address_search_progress.json
│   └── tarrant_clerk.py
├── scripts/
│   ├── migrate_duckdb_to_postgres.py
│   └── migrate_events.py
└── docker-compose.yml
```

---

<div align="center">

<sub>Built by <a href="https://github.com/ibi-raheel">Muhammad Ibrahim Raheel</a> · more work at <a href="https://ibiraheel.com">ibiraheel.com</a></sub>

</div>
