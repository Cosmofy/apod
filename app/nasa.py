import asyncio
import httpx
from datetime import date as Date, datetime
from zoneinfo import ZoneInfo
from pydantic import ValidationError
from app.config import Settings
from app.source import SourceApod
from app.errors import Error, Code

def resolve_date(requested_date: Date | None) -> Date:
    today = datetime.now(ZoneInfo("America/Denver")).date() # 2 hour delay even though apod updates at 12:05 AM Eastern Time every day (tested in 2025 June)
    target_date = requested_date or today
    if target_date < Date(1995, 6, 16): raise Error(Code.DATE_TOO_EARLY) #  first day of apod
    elif target_date > today: raise Error(Code.DATE_IN_FUTURE) # overshot
    return target_date

async def fetch_apod(requested_date: Date) -> SourceApod:
    settings = Settings() # import envs
    async with httpx.AsyncClient(timeout=15) as client:
        for attempt in range(1, 3):
            try:
                response = await client.get(
                    "https://api.nasa.gov/planetary/apod",
                    params= {
                        "api_key": settings.nasa_api_key,
                        "date": requested_date.isoformat()
                    }
                )
            except httpx.RequestError:
                if attempt == 2: raise Error(Code.NASA_UNAVAILABLE)
                await asyncio.sleep(0.5)
                continue

            if 500 <= response.status_code < 600:
                if attempt == 2: raise Error(Code.NASA_UNAVAILABLE)
                await asyncio.sleep(0.5)
                continue
            break

    match response.status_code:
        case 200: pass # success
        case 404: raise Error(Code.NOT_FOUND) # no apod found for that date (should never happen, can try to hit ellanan wrapper ig)
        case 429: raise Error(Code.NASA_RATE_LIMITED)
        case _: raise Error(Code.INVALID_NASA_RESPONSE)

    try: apod = SourceApod.model_validate_json(response.content)
    except (ValueError, ValidationError): raise Error(Code.INVALID_NASA_RESPONSE)
    if apod.date != requested_date or not (apod.url or apod.hdurl): raise Error(Code.INVALID_NASA_RESPONSE)
    return apod
