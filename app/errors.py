from fastapi import Request
from fastapi.responses import JSONResponse
from enum import Enum

class Code(Enum):
    DATE_TOO_EARLY = (400, "NASA's Astronomy Picture of the Day archive begins on June 16, 1995.")
    DATE_IN_FUTURE = (400, "NASA's Astronomy Picture of the Day is not available for future dates.")
    NOT_FOUND = (404, "No Astronomy Picture of the Day was found for the requested date.")
    NASA_UNAVAILABLE = (502, "NASA's Astronomy Picture of the Day service is temporarily unavailable.")
    INVALID_DATE_FORMAT = (422, "Date must use the YYYY-MM-DD format.")
    NASA_RATE_LIMITED = (503, "NASA's Astronomy Picture of the Day server is temporarily busy. Please try again later.")
    INVALID_NASA_RESPONSE = (502, "NASA returned an invalid Astronomy Picture of the Day response.")
    INTERNAL_ERROR = (500, "An unexpected error occurred.")
    APOD_REQUEST_IN_PROGRESS = (503, "The requested Astronomy Picture of the Day is currently being retrieved. Please try again shortly.")

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message

class Error(Exception):
    def __init__(self, code: Code):
        self.code = code
        super().__init__(code.name) # exmaple DATE_TOO_EARLY

async def handle_error(_request: Request, _error: Error) -> JSONResponse:
    return JSONResponse(status_code=_error.code.status, content=
    {
        "error": {
            "code": _error.code.name,
            "message": _error.code.message
        }
    })
