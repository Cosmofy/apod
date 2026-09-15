from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI
import redis.asyncio as redis

from app.errors import Error, handle_error
from app.config import Settings
from app.observability import configure_logging, log_requests
from app.routers import apod, earth_observatory, health, internal, vector
from app.telemetry import configure_telemetry

OPENAPI_TAGS = [
    {
        "name": "health",
        "description": "Check whether the APOD service and its dependencies are available.",
    },
    {
        "name": "apod",
        "description": "Retrieve NASA's Astronomy Picture of the Day by date.",
    },
    {
        "name": "earth-observatory",
        "description": "Retrieve NASA Earth Observatory's current Image of the Day.",
    },
    {
        "name": "vector",
        "description": "Search historical Astronomy Pictures of the Day using vector similarity.",
    },
]

configure_logging()

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with (httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as http_client, redis.from_url(Settings().redis_url) as redis_client):
        app.state.http_client = http_client
        app.state.redis_client = redis_client
        yield


# app creation and startup
app = FastAPI(
    lifespan=lifespan, # opens shared clients at startup and closes them at shutdown
    title="Cosmofy Pictures API",
    summary="Retrieve Cosmofy's NASA astronomy and Earth Observatory pictures.",
    description="Provides APOD retrieval, discovery, and the current Earth Observatory Image of the Day.",
    version="1.0.0",
    openapi_tags=OPENAPI_TAGS, # describes and orders endpoint groups in the documentation
    terms_of_service="https://github.com/Cosmofy/pictures",
    contact={"name": "Cosmofy", "url": "https://github.com/Cosmofy"},
    license_info={"name": "Proprietary"},
)
configure_telemetry(app)
app.middleware("http")(log_requests)
app.add_exception_handler(Error, handle_error)
app.include_router(health.router)
app.include_router(apod.router)
app.include_router(earth_observatory.router)
app.include_router(vector.router)
app.include_router(internal.router)
