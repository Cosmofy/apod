from typing import Annotated

from fastapi import APIRouter, Query

from app.search import ApodSearchResponse, hybrid_search_apods


router = APIRouter(prefix="/vector", tags=["vector"])


@router.get(
    "/search",
    status_code=200,
    summary="search astronomy pictures of the day",
    response_model=ApodSearchResponse,
)
async def search_apods(
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> ApodSearchResponse:
    return await hybrid_search_apods(q, limit)
