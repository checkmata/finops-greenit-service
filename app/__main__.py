"""`python -m app` — run the API with API_HOST / API_PORT (single worker: the scheduler runs in-process)."""

import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run("app.adapters.inbound.api.main:app", host=settings.API_HOST, port=settings.API_PORT, workers=1)
