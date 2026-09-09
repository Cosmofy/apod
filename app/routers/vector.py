from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from app.errors import Code, Error
from app.nasa import resolve_date
from app.search import ApodSearchResponse, hybrid_search_apods
from app.similarity import ApodSimilarityResponse, similar_apods


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


class SimilarityRoute(APIRoute):
    """Use APOD's error envelope only for this endpoint's query validation."""

    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original_handler(request)
            except RequestValidationError as error:
                if any(item["loc"][:2] == ("query", "date") for item in error.errors()):
                    raise Error(Code.INVALID_DATE_FORMAT) from error
                raise Error(Code.INVALID_SIMILARITY_REQUEST) from error

        return handler


similarity_router = APIRouter(route_class=SimilarityRoute)


@similarity_router.get(
    "/similar",
    status_code=200,
    summary="find astronomy pictures similar to a stored APOD",
    response_model=ApodSimilarityResponse,
)
async def get_similar_apods(
    date: Annotated[str, Query(description="Source APOD date in YYYY-MM-DD format.")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> ApodSimilarityResponse:
    return await similar_apods(resolve_date(date), limit)


router.include_router(similarity_router)
