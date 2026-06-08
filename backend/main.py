"""
X-LLM-Search – Main Application

FastAPI backend providing:
- Proxied /api/models (hides the internal query-planning model from the UI)
- /api/autocomplete  combining SearXNG, DuckDuckGo, and LLM suggestions
- Search via WebSocket with live progress, cancellation, and dual-model pipeline
- Static file serving for the frontend
"""

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

import httpx
from backend.config import load_settings
from backend.llm_client import LLMClient
from backend.pipeline import SearchEvent, SearchPipeline
from backend.search_engine import SearXNGClient
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------

# Lightweight model used ONLY for query planning — never shown in the UI picker.
QUERY_MODEL = "hf.co/unsloth/gemma-4-E2B-it-GGUF:UD-Q2_K_XL"

# Models hidden from the /api/models endpoint so they don't appear in the picker.
HIDDEN_MODELS: set[str] = {QUERY_MODEL}

# ---------------------------------------------------------------------------
# Global client instances
# ---------------------------------------------------------------------------

settings = load_settings()
searxng_client: SearXNGClient = None  # type: ignore


def build_clients() -> None:
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
# Helpers
# ---------------------------------------------------------------------------


async def _warm_model(client: LLMClient) -> None:
    """
    Send a minimal 1-token request to load the model into GPU memory.
    Called as a fire-and-forget task the moment a WebSocket connection arrives,
    so the query-planning model is ready before the user message is processed.
    """
    try:
        await client.chat_completion(
            messages=[{"role": "user", "content": "."}],
            max_tokens=1,
            temperature=0.0,
        )
        logger.debug("Pre-warmed %s", client.model)
    except Exception as exc:
        logger.debug("Pre-warm failed for %s: %s", client.model, exc)


async def _unload_model(model: str) -> None:
    """
    Tell Ollama to immediately evict a model from GPU memory (keep_alive=0).
    Called at the start of each search so the big answer model is cleared
    before the small query-planning model loads, avoiding VRAM contention.
    """
    llm_base = settings.llm_backend.url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{llm_base}/api/generate",
                json={"model": model, "keep_alive": 0},
            )
        logger.info("Unloaded model from GPU: %s", model)
    except Exception as exc:
        logger.debug("Could not unload %s: %s", model, exc)


async def _set_keep_alive(model: str, keep_alive: str) -> None:
    """
    Set a model's keep_alive via the native Ollama API.
    Used after synthesis to schedule auto-unload (e.g. 30s).
    The OpenAI-compat endpoint ignores extra_body keep_alive, so we use
    the native /api/generate endpoint instead.
    """
    llm_base = settings.llm_backend.url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{llm_base}/api/generate",
                json={"model": model, "keep_alive": keep_alive},
            )
        logger.info("Set keep_alive=%s for %s", keep_alive, model)
    except Exception as exc:
        logger.debug("Could not set keep_alive for %s: %s", model, exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return HTMLResponse(content=(STATIC_DIR / "index.html").read_text())


@app.get("/api/models")
async def list_models():
    """
    Proxy GET /v1/models from the LLM backend.
    Filters out HIDDEN_MODELS so the query-planning model never appears in the UI.
    """
    llm_base = settings.llm_backend.url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{llm_base}/v1/models")
            resp.raise_for_status()
            data = resp.json()

        raw = data.get("data", data if isinstance(data, list) else [])
        models = [
            m["id"]
            for m in raw
            if isinstance(m, dict) and "id" in m and m["id"] not in HIDDEN_MODELS
        ]
        return {"models": models, "error": None}

    except Exception as e:
        logger.warning("Could not fetch models from LLM backend: %s", e)
        return {"models": [], "error": str(e)}


@app.get("/api/autocomplete")
async def autocomplete(q: str = ""):
    """
    Return up to 8 search-query suggestions for the partial query `q`.

    Sources (run in parallel, 2 s total timeout):
      1. SearXNG /autocompleter
      2. DuckDuckGo AC API
      3. Gemma query model — generates 3 contextual completions via native Ollama API
    """
    q = q.strip()
    if len(q) < 2:
        return JSONResponse({"suggestions": []})

    seen: set[str] = set()
    suggestions: list[str] = []

    def _add(items: list[str]) -> None:
        for s in items:
            s = s.strip()
            if s and s.lower() not in seen and len(s) >= len(q):
                seen.add(s.lower())
                suggestions.append(s)

    async def _searxng() -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(
                    f"{settings.searxng.url}/autocompleter",
                    params={"q": q, "format": "json"},
                )
                data = r.json()
                # SearXNG returns [query, [suggestions, ...]]
                if (
                    isinstance(data, list)
                    and len(data) > 1
                    and isinstance(data[1], list)
                ):
                    return data[1]
        except Exception:
            pass
        return []

    async def _duckduckgo() -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(
                    "https://duckduckgo.com/ac/",
                    params={"q": q, "type": "list"},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                data = r.json()
                if (
                    isinstance(data, list)
                    and len(data) > 1
                    and isinstance(data[1], list)
                ):
                    return data[1]
        except Exception:
            pass
        return []

    async def _llm_suggestions() -> list[str]:
        # Uses the query-planning model via the native Ollama API (think=False)
        # so we get real text back instead of empty OpenAI-compat content.
        try:
            qc = LLMClient(settings.llm_backend.url, QUERY_MODEL)
            content = await asyncio.wait_for(
                qc.chat_completion(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You complete partial search queries. "
                                "Output exactly 3 distinct full search queries, "
                                "one per line, no numbering, no labels."
                            ),
                        },
                        {"role": "user", "content": f"Partial query: {q}"},
                    ],
                    max_tokens=80,
                    temperature=0.6,
                    think=False,
                ),
                timeout=5.0,
            )
            lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
            logger.debug("LLM autocomplete suggestions: %s", lines)
            return lines
        except Exception as exc:
            logger.debug("LLM autocomplete failed: %s", exc)
            return []

    results = await asyncio.gather(_searxng(), _duckduckgo(), _llm_suggestions())
    for r in results:
        _add(r)

    return JSONResponse({"suggestions": suggestions[:8]})


@app.websocket("/ws/search")
async def search_ws(websocket: WebSocket):
    """
    WebSocket endpoint for search with live progress and cancellation.

    Client sends:  { "query": "...", "model": "...", "history": [...], "is_mobile": bool }
    Client can also send: { "type": "cancel" } to abort mid-search.
    Server emits:  SearchEvent JSON objects, then { "type": "answer", ... }
                   or { "type": "cancelled" } if aborted.
    """
    await websocket.accept()

    # Pre-warm the lightweight query model the instant a connection arrives,
    # so it's loaded into GPU memory before we need it for query expansion.
    query_client = LLMClient(settings.llm_backend.url, QUERY_MODEL)
    asyncio.create_task(_warm_model(query_client))

    search_task: asyncio.Task | None = None

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

        # Evict the big answer model from GPU before the small query model loads.
        # Awaited so the VRAM is actually free before query expansion starts.
        await _unload_model(model_override)

        answer_client = LLMClient(settings.llm_backend.url, model_override)

        async def emit(event: SearchEvent) -> None:
            await websocket.send_json(event.to_dict())

        engine = SearchPipeline(searxng_client, query_client, answer_client, emit)

        # Run search as a cancellable task
        search_task = asyncio.create_task(
            engine.normal_search(query, history=history, is_mobile=is_mobile)
        )

        # Concurrently listen for a cancel message from the client
        async def _listen_for_cancel() -> None:
            try:
                msg = await websocket.receive_text()
                data = json.loads(msg)
                if data.get("type") == "cancel":
                    if search_task and not search_task.done():
                        search_task.cancel()
            except Exception:
                # WebSocket closed — cancel the search
                if search_task and not search_task.done():
                    search_task.cancel()

        cancel_listener = asyncio.create_task(_listen_for_cancel())

        try:
            answer, sources = await search_task
            cancel_listener.cancel()
            # Schedule the big model to unload 30 s after answering so it
            # doesn't sit in VRAM forever, but stays warm for quick follow-ups.
            asyncio.create_task(_set_keep_alive(model_override, "30s"))
            await websocket.send_json(
                {"type": "answer", "content": answer, "sources": sources}
            )
        except asyncio.CancelledError:
            cancel_listener.cancel()
            logger.info("Search cancelled for query: %s", query)
            try:
                await websocket.send_json({"type": "cancelled"})
            except Exception:
                pass

    except WebSocketDisconnect:
        logger.info("Client disconnected during search")
        if search_task and not search_task.done():
            search_task.cancel()
    except Exception as e:
        logger.error("WebSocket search error: %s", e)
        if search_task and not search_task.done():
            search_task.cancel()
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
