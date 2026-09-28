from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .database import check_connection
from .routes import properties, owners, documents, filings, stats, search, export, debt, entities, admin, ownership

app = FastAPI(
    title="Parcel Scout v3 API",
    description="Real estate intelligence platform — DFW parcel data",
    version="3.0.0",
)

# CORS — allow Next.js dev server and same-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "*",  # tighten in production
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all routers under /api prefix
API_PREFIX = "/api"

app.include_router(properties.router, prefix=API_PREFIX)
app.include_router(owners.router, prefix=API_PREFIX)
app.include_router(documents.router, prefix=API_PREFIX)
app.include_router(filings.router, prefix=API_PREFIX)
app.include_router(stats.router, prefix=API_PREFIX)
app.include_router(search.router, prefix=API_PREFIX)
app.include_router(export.router, prefix=API_PREFIX)
app.include_router(debt.router, prefix=API_PREFIX)
app.include_router(entities.router, prefix=API_PREFIX)
app.include_router(admin.router, prefix=API_PREFIX)
app.include_router(ownership.router, prefix=API_PREFIX)


@app.get("/api/health")
def health():
    db_ok = check_connection()
    return JSONResponse(
        content={"status": "ok" if db_ok else "degraded", "database": db_ok},
        status_code=200 if db_ok else 503,
    )


@app.get("/")
def root():
    return {
        "service": "Parcel Scout v3 API",
        "version": "3.0.0",
        "docs": "/docs",
        "health": "/api/health",
    }
