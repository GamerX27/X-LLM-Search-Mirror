"""
X-LLM-Search – search orchestration engine.

Performs multi-query web searches with LLM-powered query expansion, reasoning,
and summarisation. Progress events are streamed via a callback so the frontend
can show live updates.
"""

import logging
import re
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from backend.llm_client import LLMClient
from backend.search_engine import SearXNGClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Progress event types
# ---------------------------------------------------------------------------


class SearchEvent:
    """A progress event emitted during the search pipeline."""

    def __init__(self, event_type: str, data: dict) -> None:
        self.event_type = event_type  # "search", "analyze", "followup", "report"
        self.data = data
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "type": self.event_type,
            **self.data,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Web page fetcher
# ---------------------------------------------------------------------------


async def fetch_page_content(url: str, timeout: float = 20.0) -> Optional[str]:
    """Fetch and return the text content of a web page."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()

            html = response.text
            clean = re.sub(
                r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE
            )
            clean = re.sub(
                r"<style[^>]*>.*?</style>", "", clean, flags=re.DOTALL | re.IGNORECASE
            )
            clean = re.sub(r"<br/>", "\n", clean, flags=re.IGNORECASE)
            clean = re.sub(r"</p>", "\n\n", clean, flags=re.IGNORECASE)
            clean = re.sub(r"</li>", "\n", clean, flags=re.IGNORECASE)
            clean = re.sub(r"<[^>]+>", "", clean)
            clean = re.sub(r"\s+", " ", clean).strip()

            # Truncate to keep input within the LLM's context window
            return clean[:3000]

    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Context-window safety
# ---------------------------------------------------------------------------

# At ~4 chars/token, 8 000 chars ≈ 2 000 tokens — leaves headroom for
# system instructions and the model's response within a 4 k-token window.
MAX_FINDINGS_CHARS = 8_000


def _trim_findings(text: str) -> str:
    """Trim accumulated findings to fit safely in the model's context window."""
    if len(text) <= MAX_FINDINGS_CHARS:
        return text
    trimmed = text[-MAX_FINDINGS_CHARS:]
    nl = trimmed.find("\n")
    return "[...earlier findings trimmed for context...]\n" + (
        trimmed[nl + 1 :] if nl != -1 else trimmed
    )


def _build_attachment_context(attachment: dict | None) -> str:
    """Return a context prefix string from an attachment dict, or empty string."""
    if not attachment:
        return ""
    if attachment.get("type") == "text" and attachment.get("content"):
        fname = attachment.get("filename", "file")
        content = attachment["content"][:4000]  # stay within context window
        return f"[Attached document \u2014 {fname}]\n{content}\n\n"
    return ""


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

SUMMARIZE_PROMPT = """You are a research assistant. Based on the following search results, provide a concise and informative answer to the user's query.

Query: {query}

Search Results:
{results_text}

Provide a well-structured answer with key findings. Cite sources where applicable using [Source N] notation.
"""


QUERY_EXPANSION_PROMPT = """Generate {n} specific web search queries for the following question. Each query should target a different angle or phrasing.

Question: {query}

Output only the queries, one per line, no numbering:
QUERY: <search query>
"""


# ---------------------------------------------------------------------------
# Source record type
# ---------------------------------------------------------------------------

# {"num": 1, "title": "Page title", "url": "https://..."}
SourceRecord = Dict[str, object]


# ---------------------------------------------------------------------------
# Search pipeline
# ---------------------------------------------------------------------------


class SearchPipeline:
    """Orchestrates the full search pipeline: query expansion → search → fetch → summarise."""

    def __init__(
        self,
        searxng_client: SearXNGClient,
        llm_client: LLMClient,
        on_event: Callable[[SearchEvent], Awaitable[None]],
    ) -> None:
        self.searxng = searxng_client
        self.llm = llm_client
        self.on_event = on_event

    async def _expand_query(self, query: str, n: int = 3) -> List[str]:
        """Use the LLM to generate n targeted search queries from a single query.

        Falls back to [query] if the LLM returns nothing useful.
        """
        prompt = QUERY_EXPANSION_PROMPT.format(query=query, n=n)
        try:
            content, thinking = await self.llm.chat_completion_thinking(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=200,
            )
            if thinking:
                await self.on_event(
                    SearchEvent(
                        "thinking", {"content": thinking, "label": "Query expansion"}
                    )
                )
            queries = [
                line.replace("QUERY:", "").strip()
                for line in content.split("\n")
                if line.strip().startswith("QUERY:")
            ][:n]
            if queries:
                return queries
        except Exception as e:
            logger.warning(f"Query expansion failed: {e}")
        return [query]

    async def normal_search(
        self, query: str, attachment: dict | None = None
    ) -> Tuple[str, List[SourceRecord]]:
        """
        Perform a multi-query web search and generate an answer.

        The LLM first expands the query into several targeted search queries,
        searches all of them, deduplicates results, then summarises everything.

        Returns (answer_text, sources).
        """
        # ── Expand query into multiple search angles ──────────────────────────
        await self.on_event(
            SearchEvent("analyze", {"status": "Generating search queries…"})
        )
        expanded = await self._expand_query(query, n=3)
        await self.on_event(
            SearchEvent("queries", {"queries": expanded, "original": query})
        )

        # ── Search every expanded query, deduplicate across all ───────────────
        sources: List[SourceRecord] = []
        results_text = ""
        seen_urls: set = set()
        src_num = 0

        for eq in expanded:
            await self.on_event(SearchEvent("search", {"query": eq}))
            try:
                results = await self.searxng.search(eq, num_results=5)
            except Exception as e:
                logger.warning(f"Search failed for '{eq}': {e}")
                results = []

            await self.on_event(
                SearchEvent(
                    "search_complete", {"query": eq, "result_count": len(results)}
                )
            )

            for r in results:
                if r.url in seen_urls:
                    continue
                seen_urls.add(r.url)
                src_num += 1
                results_text += (
                    f"[Source {src_num}] {r.title}\nURL: {r.url}\n{r.snippet}\n\n"
                )
                sources.append({"num": src_num, "title": r.title, "url": r.url})

        if not results_text:
            await self.on_event(SearchEvent("complete", {"status": "Done"}))
            return f"No search results found for '{query}'.", []

        # ── Summarise the combined results ────────────────────────────────────
        await self.on_event(SearchEvent("analyze", {"status": "Analyzing results…"}))

        # ── Handle image attachment via vision ────────────────────────────────
        if (
            attachment
            and attachment.get("type") == "image"
            and attachment.get("data_url")
        ):
            await self.on_event(
                SearchEvent("analyze", {"status": "Analyzing image\u2026"})
            )
            image_answer = await self.llm.chat_completion_vision(
                text_prompt=f"The user asks: {query}\n\nPlease analyze the image and answer the question.",
                image_data_url=attachment["data_url"],
                temperature=0.3,
                max_tokens=1024,
            )
            if image_answer:
                await self.on_event(
                    SearchEvent(
                        "thinking", {"content": image_answer, "label": "Image analysis"}
                    )
                )
                results_text = (
                    f"[Image analysis result]\n{image_answer}\n\n" + results_text
                )

        ctx = _build_attachment_context(attachment)
        prompt = SUMMARIZE_PROMPT.format(
            query=query, results_text=_trim_findings(ctx + results_text)
        )
        answer, answer_thinking = await self.llm.chat_completion_thinking(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2048,
        )
        if answer_thinking:
            await self.on_event(
                SearchEvent(
                    "thinking",
                    {"content": answer_thinking, "label": "Answer synthesis"},
                )
            )

        await self.on_event(SearchEvent("complete", {"status": "Done"}))
        return answer, sources
