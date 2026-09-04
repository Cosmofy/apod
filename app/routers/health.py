from redis import RedisError
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pprint import pprint
from app.database import connect_database
import logging
from turso_serverless import OperationalError
from asyncio import to_thread

router = APIRouter(prefix="/health", tags=["health"])
logger = logging.getLogger(name=__name__)

# health check used by uptime kuma and incident io
# red if completely down
@router.get(path="/live", status_code=200, summary="app liveness check")
def live(request: Request) -> JSONResponse:
    # pprint(request.app.title)
    return JSONResponse(
        status_code=200,
        content= {
            "status": "ok",
            "app": request.app.title,
            "version": request.app.version
        }
    )


# path should be GET /health/ready
# yellow if any unavailable, degraded
@router.get(path="/ready", status_code=200, summary='app readiness check')
async def ready(request: Request) -> JSONResponse:

    # request has scope as a key, and the value of that contains app, asgi, client, endpoint, fastapi
    # the app is the actual FastAPI app.
    try:
        logger.info(msg="checking redis readiness", extra={"dependency": "apod:redis"})
        redis_ready = await request.app.state.redis_client.ping() # does not ever return False, throws RedisError
    except RedisError:
        redis_ready = False
        logger.exception(msg="redis readiness check failed", extra={"dependency": "apod:redis"})

    # helper codeblock
    def check_turso_readiness() -> bool:
        connection = None
        try:
            connection = connect_database()
            return connection.execute("SELECT 67, 69").fetchone() == (67, 69)
        finally:
            if connection is not None:
                logger.info(msg="closing turso connection", extra={"dependency": "apod:turso"})
                connection.close()

    try:
        logger.info(msg="checking turso readiness", extra={"dependency": "apod:turso"})
        turso_ready = await to_thread(check_turso_readiness)
    except OperationalError:
        turso_ready = False
        logger.exception(msg="turso readiness check failed", extra={"dependency": "apod:turso"})

    dependencies = {
        "redis": "ok" if redis_ready else "unavailable",
        "turso": "ok" if turso_ready else "unavailable",
    }

    if redis_ready and turso_ready:
        return JSONResponse(
            status_code=200,
            content={"status": "ready", "dependencies": dependencies},
        )

    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "dependencies": dependencies},
    )
