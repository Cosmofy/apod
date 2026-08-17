from fastapi import FastAPI
from app.errors import register_error_handler

app = FastAPI()

register_error_handler(app)

# to be removed
@app.get("/")
async def root():
    return {"message": "Hello World"}

@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


# get picture by date (default = today, otherwise has data argument)
@app.get("/apod")
async def get_apod(get_picture):
    # check redis cache
    # check database
    # check nasa api

    # get picture by keywords (has to search vector database)
