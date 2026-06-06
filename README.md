# ✦ X-LLM-Search

A self-hosted, privacy-first AI search interface powered by local language models.  
It expands your queries into multiple targeted searches, reads source pages, reasons about the results, and delivers a cited answer — all running on your own hardware with no data sent to third-party AI services.

---

## Features

| Feature | Description |
|---|---|
| **Multi-query expansion** | The LLM rewrites your question into several targeted search queries, covering different angles, before searching |
| **Live progress UI** | Streaming WebSocket interface shows each query, page read, and thinking step as it happens |
| **Chain-of-thought reasoning** | Supports thinking/reasoning models (Qwen QwQ, DeepSeek-R1, Phi-4 Reasoning, etc.) — the model's reasoning is shown as a collapsible inline section |
| **Vision / image analysis** | Attach an image and the LLM analyzes it alongside the search results |
| **PDF support** | Upload a PDF; the text is extracted server-side and injected as context for the LLM |
| **TXT file support** | Attach any plain-text file as additional context |
| **Clickable citations** | `[Source N]` references in answers link directly to the original pages |
| **Collapsible references** | A Sources dropdown at the bottom of each answer lists every cited page with favicon |
| **Model picker** | Switch between any model available in your Ollama instance without restarting |
| **Fully local** | No OpenAI key, no cloud calls — LLM inference runs in Ollama on your own machine |

---

## Architecture

```
Browser
  │
  │  WebSocket (/ws/search)
  ▼
FastAPI app  (backend/main.py)
  │
  ├── SearchPipeline  (backend/pipeline.py)
  │     ├── LLMClient  ──────────────────────► Ollama  (port 11434)
  │     │     ├── chat_completion()            any OpenAI-compatible backend
  │     │     ├── chat_completion_thinking()   extracts <think> blocks
  │     │     └── chat_completion_vision()     multimodal image + text
  │     │
  │     └── SearXNGClient  ─────────────────► SearXNG  (port 8080)
  │           └── search()                    proxies to Google, Bing, DDG, etc.
  │
  └── Static files  (backend/static/)
        ├── index.html
        ├── css/style.css
        └── js/app.js
```

### Search pipeline (per request)

```
User query
    │
    ▼
LLM: expand into N targeted queries
    │
    ▼  (for each query)
SearXNG: web search  →  top results (title, URL, snippet)
    │
    ▼  (for each result URL)
Fetch page content  →  strip HTML  →  truncate to 3 000 chars
    │
    ▼
LLM: summarise findings with [Source N] citations
    │
    ▼
Answer + sources streamed to browser via WebSocket
```

---

## Tech Stack

| Component | Technology |
|---|---|
| **Backend** | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) (async Python) |
| **WebSocket streaming** | FastAPI native WebSocket support |
| **LLM inference** | [Ollama](https://ollama.com/) — runs models locally via OpenAI-compatible API |
| **LLM client** | Official `openai` Python SDK pointed at Ollama |
| **Web search** | [SearXNG](https://searxng.github.io/searxng/) — self-hosted meta-search engine |
| **HTTP client** | [httpx](https://www.python-httpx.org/) (async) |
| **PDF parsing** | [pypdf](https://pypdf.readthedocs.io/) |
| **Data validation** | [Pydantic v2](https://docs.pydantic.dev/) |
| **Frontend** | Vanilla HTML / CSS / JS — no framework, no build step |
| **Containerisation** | Docker + Docker Compose |

---

## Prerequisites

- **Docker** and **Docker Compose** installed
- An **AMD GPU** with ROCm support (the default Ollama image is `ollama:rocm`)  
  → For Nvidia, change the image to `ollama/ollama` and swap the `devices` block for `deploy: resources: reservations: devices`  
  → For CPU-only, use `ollama/ollama` and remove the `devices` block entirely
- At least one model pulled in Ollama (see [Pulling a model](#pulling-a-model))

---

## Deployment

### 1. Clone the repository

```bash
git clone <your-repo-url> x-llm-search
cd x-llm-search
```

### 2. (Optional) Configure SearXNG

The default `searxng_config/settings.yml` is ready to use.  
If you want to change the secret key (recommended for networked installs):

```yaml
# searxng_config/settings.yml
server:
  secret_key: "change-me-to-something-random"
```

### 3. Start the stack

```bash
docker compose up -d
```

This starts three containers:

| Container | Purpose | Port |
|---|---|---|
| `ollama-x-llm-search` | Local LLM inference (Ollama) | 11434 |
| `searxng-x-llm-search` | Privacy-respecting meta-search | 8080 |
| `x-llm-search` | FastAPI app + frontend | **8000** |

Open **http://localhost:8000** in your browser.

### 4. Pulling a model

The model picker in the UI lists every model already downloaded in Ollama.  
Pull a model via the Ollama container:

```bash
# General-purpose (fast, small context window)
docker exec ollama-x-llm-search ollama pull llama3.2

# Reasoning / thinking model (slower, better answers)
docker exec ollama-x-llm-search ollama pull qwq

# Vision model (required for image attachments)
docker exec ollama-x-llm-search ollama pull llava

# Larger, higher-quality option
docker exec ollama-x-llm-search ollama pull qwen2.5:14b
```

Refresh the model picker in the UI — it will appear immediately.

---

## Configuration

Settings are stored in `config/settings.json` and auto-generated on first run:

```json
{
  "llm_backend": {
    "type": "ollama",
    "url": "http://ollama:11434",
    "model": ""
  },
  "searxng": {
    "url": "http://searxng:8080",
    "format_": "json"
  }
}
```

The `url` values use Docker Compose service names so containers can reach each other over the internal network. If you run Ollama or SearXNG outside of Docker, update these to point at the correct host.

---

## File Attachments

| Type | How it works |
|---|---|
| **Image** (jpg / png / gif / webp) | Encoded as base64 in the browser and sent to the LLM as a vision message alongside the query. Requires a vision-capable model (e.g. `llava`, `minicpm-v`). |
| **PDF** | Uploaded to `/api/parse-pdf`; the server extracts text with `pypdf` and injects it as document context before the LLM prompt. |
| **TXT** | Read in the browser with the FileReader API and injected as document context. |

---

## Updating

```bash
docker compose pull        # pull updated base images
docker compose up -d --build   # rebuild the app image with latest code
```

---

## Stopping

```bash
docker compose down        # stop and remove containers
docker compose down -v     # also remove the Ollama model volume (⚠ deletes downloaded models)
```

---

## Project Structure

```
X-LLM-Search/
├── backend/
│   ├── main.py            # FastAPI app — routes and WebSocket handlers
│   ├── pipeline.py        # Search pipeline: query expansion, fetch, summarise
│   ├── llm_client.py      # Ollama / OpenAI-compatible LLM client (incl. vision + thinking)
│   ├── search_engine.py   # SearXNG HTTP client
│   ├── config.py          # Settings model + load/save helpers
│   ├── requirements.txt   # Python dependencies
│   └── static/
│       ├── index.html
│       ├── css/style.css
│       └── js/app.js
├── config/
│   └── settings.json      # Auto-generated runtime config (gitignore this if sensitive)
├── searxng_config/
│   └── settings.yml       # SearXNG configuration
├── Dockerfile
└── docker-compose.yml
```

---

## License

MIT — do whatever you want with it.
