"""
X-LLM-Search – Main Application

FastAPI backend providing:
- Proxied /api/models endpoint that fetches from the LLM backend
- Search via WebSocket with live progress events and conversation memory
- Static file serving for the frontend
"""

import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure the project root is on the path so package imports work
sys.path.append(str(Path(__file__).parent.parent))

import httpx
from backend.config import load_settings
from backend.llm_client import LLMClient
from backend.pipeline import SearchEvent, SearchPipeline
from backend.search_engine import SearXNGClient
from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global client instances (rebuilt when settings change)
# ---------------------------------------------------------------------------

settings = load_settings()
searxng_client: SearXNGClient = None  # type: ignore


def build_clients() -> None:
    """Instantiate SearXNG and LLM clients from current settings."""
    global searxng_client
    searxng_client = SearXNGClient(settings.searxng.url)


@asynccontextmanager
async def lifespan(app: FastAPI):
    build_clients()
    logger.info("X-LLM-Search started — LLM backend: %s", settings.llm_backend.url)
    yield
    if searxng_client:
        await searxng_client.close()
    logger.info("X-LLM-Search stopped")


app = FastAPI(title="X-LLM-Search", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the single-page frontend."""
    return HTMLResponse(content=(STATIC_DIR / "index.html").read_text())


@app.get("/api/models")
async def list_models():
    """
    Proxy GET /v1/models from the local LLM backend and return the model list.
    Returns an empty list with an error message if the backend is unreachable
    so the frontend can still render gracefully.
    """
    llm_base = settings.llm_backend.url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{llm_base}/v1/models")
            resp.raise_for_status()
            data = resp.json()

        # OpenAI-compatible format: { "data": [ { "id": "model-name" }, ... ] }
        # Some backends (e.g. LM Studio) may also return a flat list — handle both.
        raw = data.get("data", data if isinstance(data, list) else [])
        models = [m["id"] for m in raw if isinstance(m, dict) and "id" in m]
        return {"models": models, "error": None}

    except Exception as e:
        logger.warning("Could not fetch models from LLM backend: %s", e)
        # Return empty list instead of 502 so the frontend still loads
        return {"models": [], "error": str(e)}


@app.websocket("/ws/search")
async def search_ws(websocket: WebSocket):
    """
    WebSocket endpoint for standard search with live progress events.

    Client sends:  { "query": "...", "model": "..." }
    Server emits:  SearchEvent JSON objects as the search progresses, then
                   { "type": "answer", "content": "<text>", "sources": [...] }
    """
    await websocket.accept()
    try:
        raw = await websocket.receive_text()
        payload = json.loads(raw)
        query = payload.get("query", "").strip()
        model_override = payload.get("model") or settings.llm_backend.model
        history = payload.get("history") or []
        is_mobile = bool(payload.get("is_mobile", False))

        if not query:
            await websocket.send_json({"type": "error", "message": "Query is empty"})
            await websocket.close()
            return

        client = LLMClient(settings.llm_backend.url, model_override)

        async def emit(event: SearchEvent) -> None:
            await websocket.send_json(event.to_dict())

        engine = SearchPipeline(searxng_client, client, emit)
        answer, sources = await engine.normal_search(
            query, history=history, is_mobile=is_mobile
        )

        await websocket.send_json(
            {
                "type": "answer",
                "content": answer,
                "sources": sources,
            }
        )

    except WebSocketDisconnect:
        logger.info("Client disconnected during search")
    except Exception as e:
        logger.error("WebSocket search error: %s", e)
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
