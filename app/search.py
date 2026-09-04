import asyncio
from dataclasses import dataclass, field
import logging
import re
from typing import Literal

from openai import OpenAIError
from opentelemetry import trace
from pydantic import BaseModel, Field
from turso_serverless import OperationalError

from app.database import (
    DatabaseSearchMatch,
    search_lexical_apods,
    search_vector_apods,
)
from app.embeddings import create_query_embedding
from app.errors import Code, Error
from app.source import SourceApod


RANK_FUSION_CONSTANT = 60
LEXICAL_WEIGHT = 0.45
SEMANTIC_WEIGHT = 0.55

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ApodSearchResult(SourceApod):
    relevance_score: float = Field(ge=0.0, le=1.0)
    match_types: list[Literal["lexical", "semantic"]]


class ApodSearchResponse(BaseModel):
    query: str
    search_mode: Literal["hybrid", "lexical", "semantic"]
    results: list[ApodSearchResult]


@dataclass
class FusedMatch:
    apod: SourceApod
    score: float = 0.0
    match_types: set[Literal["lexical", "semantic"]] = field(default_factory=set)


def normalize_search_query(value: str) -> str:
    query = " ".join(value.split())
    searchable_characters = re.findall(r"[^\W_]", query, flags=re.UNICODE)
    if not query or len(query) > 200 or not searchable_characters:
        raise Error(Code.INVALID_SEARCH_QUERY)
    return query


def build_fts_query(query: str) -> str:
    tokens = re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE)
    if not tokens:
        raise Error(Code.INVALID_SEARCH_QUERY)
    return " AND ".join(f'"{token}"*' for token in tokens)


def fuse_search_results(
    lexical_matches: list[DatabaseSearchMatch],
    semantic_matches: list[DatabaseSearchMatch],
    limit: int,
) -> list[ApodSearchResult]:
    fused: dict[str, FusedMatch] = {}

    for rank, match in enumerate(lexical_matches, 1):
        key = match.apod.date.isoformat()
        candidate = fused.setdefault(key, FusedMatch(apod=match.apod))
        candidate.score += LEXICAL_WEIGHT / (RANK_FUSION_CONSTANT + rank)
        candidate.match_types.add("lexical")

    for rank, match in enumerate(semantic_matches, 1):
        key = match.apod.date.isoformat()
        candidate = fused.setdefault(key, FusedMatch(apod=match.apod))
        candidate.score += SEMANTIC_WEIGHT / (RANK_FUSION_CONSTANT + rank)
        candidate.match_types.add("semantic")

    ordered = sorted(
        fused.values(),
        key=lambda candidate: (candidate.score, candidate.apod.date),
        reverse=True,
    )[:limit]
    maximum_score = ordered[0].score if ordered else 1.0
    return [
        ApodSearchResult(
            **candidate.apod.model_dump(),
            relevance_score=round(candidate.score / maximum_score, 6),
            match_types=sorted(candidate.match_types),
        )
        for candidate in ordered
    ]


async def hybrid_search_apods(query: str, limit: int) -> ApodSearchResponse:
    normalized_query = normalize_search_query(query)
    fts_query = build_fts_query(normalized_query)
    candidate_limit = min(100, max(30, limit * 5))

    with tracer.start_as_current_span("apod.search") as span:
        span.set_attribute("apod.search.requested_limit", limit)
        span.set_attribute("apod.search.candidate_limit", candidate_limit)

        lexical_result, embedding_result = await asyncio.gather(
            asyncio.to_thread(search_lexical_apods, fts_query, candidate_limit),
            asyncio.to_thread(create_query_embedding, normalized_query),
            return_exceptions=True,
        )

        lexical_matches: list[DatabaseSearchMatch] = []
        semantic_matches: list[DatabaseSearchMatch] = []
        lexical_available = not isinstance(lexical_result, BaseException)
        semantic_available = not isinstance(embedding_result, BaseException)

        if lexical_available:
            lexical_matches = lexical_result
        elif isinstance(lexical_result, OperationalError):
            logger.error(
                "lexical APOD search failed",
                exc_info=(
                    type(lexical_result),
                    lexical_result,
                    lexical_result.__traceback__,
                ),
                extra={"event": "apod.search.lexical.failed", "dependency": "apod:turso"},
            )
        else:
            raise lexical_result

        if semantic_available:
            try:
                semantic_matches = await asyncio.to_thread(
                    search_vector_apods,
                    embedding_result,
                    candidate_limit,
                )
            except OperationalError:
                semantic_available = False
                logger.exception(
                    "semantic APOD search failed",
                    extra={"event": "apod.search.semantic.failed", "dependency": "apod:turso"},
                )
        elif isinstance(embedding_result, (OpenAIError, ValueError)):
            logger.error(
                "APOD query embedding failed",
                exc_info=(
                    type(embedding_result),
                    embedding_result,
                    embedding_result.__traceback__,
                ),
                extra={"event": "apod.search.embedding.failed", "dependency": "apod:openai"},
            )
        else:
            raise embedding_result

        if not lexical_available and not semantic_available:
            raise Error(Code.SEARCH_UNAVAILABLE)

        if lexical_available and semantic_available:
            search_mode: Literal["hybrid", "lexical", "semantic"] = "hybrid"
        elif lexical_available:
            search_mode = "lexical"
        else:
            search_mode = "semantic"

        results = fuse_search_results(
            lexical_matches,
            semantic_matches,
            limit,
        )
        span.set_attribute("apod.search.mode", search_mode)
        span.set_attribute("apod.search.result_count", len(results))
        logger.info(
            "APOD search completed",
            extra={
                "event": "apod.search.completed",
                "source": search_mode,
            },
        )
        return ApodSearchResponse(
            query=normalized_query,
            search_mode=search_mode,
            results=results,
        )
