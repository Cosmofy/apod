import asyncio
from datetime import date
import logging
import math

from opentelemetry import trace
from pydantic import BaseModel, Field
from turso_serverless import Error as DatabaseError

from app.database import find_similar_database_apods
from app.errors import Code, Error
from app.source import SourceApod


logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ApodSimilarityResult(SourceApod):
    relevance_score: float = Field(ge=0.0, le=1.0)


class ApodSimilarityResponse(BaseModel):
    date: date
    results: list[ApodSimilarityResult]


async def similar_apods(apod_date: date, limit: int) -> ApodSimilarityResponse:
    with tracer.start_as_current_span("apod.similar") as span:
        span.set_attribute("apod.similarity.requested_limit", limit)
        try:
            # to_thread preserves the current request/trace context and avoids
            # blocking FastAPI's event loop with the synchronous database client.
            matches = await asyncio.to_thread(find_similar_database_apods, apod_date, limit)
            if any(not math.isfinite(match.metric) for match in matches):
                raise ValueError("invalid cosine distance")

            seen = {apod_date}
            results = []
            for match in sorted(matches, key=lambda item: (item.metric, -item.apod.date.toordinal())):
                if match.apod.date in seen:
                    continue
                seen.add(match.apod.date)
                results.append(
                    ApodSimilarityResult(
                        **match.apod.model_dump(),
                        relevance_score=max(0.0, min(1.0, 1.0 - match.metric)),
                    )
                )
                if len(results) == limit:
                    break
        except (DatabaseError, OSError, ValueError, TypeError) as error:
            logger.exception(
                "APOD similarity unavailable",
                extra={"event": "apod.similarity.failed", "dependency": "apod:turso"},
            )
            raise Error(Code.SIMILARITY_UNAVAILABLE) from error

        span.set_attribute("apod.similarity.result_count", len(results))
        logger.info(
            "APOD similarity completed",
            extra={"event": "apod.similarity.completed", "source": "turso"},
        )
        return ApodSimilarityResponse(date=apod_date, results=results)
