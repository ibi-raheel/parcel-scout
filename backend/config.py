import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql://parcelscout:parcelscout_dev@localhost:5432/parcelscout",
)

# API settings
API_PREFIX = "/api"
DEFAULT_LIMIT = 5000
MAX_LIMIT = 10000
