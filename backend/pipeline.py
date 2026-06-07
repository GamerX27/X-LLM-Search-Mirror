"""
X-LLM-Search – search orchestration engine.

Inspired by Brave Search AI and LM Studio tool-use search:
  1. LLM expands the user query into 5 targeted search queries (different angles)
  2. All queries are searched in parallel, results deduplicated
  3. Full page content is fetched for the top results (not just snippets)
  4. LLM performs a gap analysis and issues 1-2 follow-up searches
  5. Final synthesis uses system+user message pair with full conversation history

Progress events are streamed via a callback so the frontend shows live updates.
"""

import asyncio
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
        self.event_type = event_type
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

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


async def fetch_page_content(url: str, timeout: float = 15.0) -> Optional[str]:
    """Fetch and return the main text content of a web page."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout, headers=_HEADERS, follow_redirects=True
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

            html = response.text
            # Remove scripts, styles, nav, footer clutter
            clean = re.sub(
                r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE
            )
            clean = re.sub(
                r"<style[^>]*>.*?</style>", "", clean, flags=re.DOTALL | re.IGNORECASE
            )
            clean = re.sub(
                r"<nav[^>]*>.*?</nav>", "", clean, flags=re.DOTALL | re.IGNORECASE
            )
            clean = re.sub(
                r"<footer[^>]*>.*?</footer>", "", clean, flags=re.DOTALL | re.IGNORECASE
            )
            clean = re.sub(
                r"<header[^>]*>.*?</header>", "", clean, flags=re.DOTALL | re.IGNORECASE
            )
            clean = re.sub(r"<br\s*/?>", "\n", clean, flags=re.IGNORECASE)
            clean = re.sub(r"</p>", "\n\n", clean, flags=re.IGNORECASE)
            clean = re.sub(r"</li>", "\n", clean, flags=re.IGNORECASE)
            clean = re.sub(r"<[^>]+>", "", clean)
            clean = re.sub(r"[ \t]+", " ", clean)
            clean = re.sub(r"\n{3,}", "\n\n", clean).strip()

            # Keep up to 4000 chars per page — enough substance, within context limits
            return clean[:4000]

    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Context-window safety
# ---------------------------------------------------------------------------

# At ~4 chars/token, 8 000 chars ≈ 2 000 tokens for retrieved content.
# Keeping this modest ensures the full prompt (system + context + results)
# stays well inside the 4k–8k context window of typical local models.
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


def _build_conversation_context(history: list) -> str:
    """Return a formatted conversation history prefix, or empty string."""
    if not history:
        return ""
    lines = []
    for h in history[-6:]:  # keep last 3 exchanges (6 messages) to avoid overflow
        role = h.get("role", "")
        content = str(h.get("content", ""))[:600]  # truncate each turn
        if role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")
    if not lines:
        return ""
    return "Previous conversation:\n" + "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

# System identity used in every LLM call
SEARCH_SYSTEM_PROMPT = (
    "You are an expert research assistant with real-time web search capabilities. "
    "You search the internet thoroughly and synthesize findings into accurate, "
    "well-structured, and fully-cited answers. "
    "Always cite facts using [Source N] notation referencing the provided sources."
)

# Forces strategic, multi-angle query generation — inspired by Brave Search AI
QUERY_EXPANSION_PROMPT = """\
You are a professional search strategist. Convert the user's question into {n} highly effective, \
diverse web search queries that together will find comprehensive information from multiple sources.

Think step-by-step:
1. What is the core information need? (factual lookup, how-to, recent news, comparison, etc.)
2. What different angles must be covered? (causes, effects, examples, official docs, expert opinion, current status)
3. What precise terminology will surface authoritative sources? (technical terms, proper names, official phrases)
4. Which queries should target recent information vs. background knowledge?

Rules for each query:
- Cover a DIFFERENT aspect or source type than the others
- Use specific, precise language that experts and authoritative sites use
- Avoid generic phrasing — be as targeted as possible
- Include time signals ("2024", "latest", "current") where recency matters

User question: {query}

Output exactly {n} search queries, one per line:
QUERY: <targeted search query>
"""

# Gap analysis — asks the LLM what's still missing after the first search round
GAP_ANALYSIS_PROMPT = """\
You are a research analyst reviewing partial search results. Identify the most critical \
information gaps that still need to be filled to fully answer the original question.

Original question: {query}

Information found so far (summaries):
{findings_summary}

If important aspects are still uncovered, output up to {n} additional targeted search queries \
that would fill the most critical gaps. If the existing results are already sufficient, output nothing.

Each query on its own line:
QUERY: <gap-filling search query>
"""

# Final synthesis — used as the user turn with SEARCH_SYSTEM_PROMPT as system
SYNTHESIZE_PROMPT = """\
Using the web search results below, provide a comprehensive, accurate answer to this question.

Question: {query}

{conversation_context}Web Search Results:
{results_text}

Write a well-structured answer. Cite every factual claim with [Source N]. \
Use headers (##) for complex multi-part answers. \
If results conflict, note the discrepancy. \
If the search results don't fully answer the question, clearly say what's missing.\
"""

# Appended to SYNTHESIZE_PROMPT when the client is a mobile device
MOBILE_FORMAT_INSTRUCTION = """

FORMAT FOR MOBILE SCREEN: The user is reading on a phone in portrait mode. \
Follow these rules strictly:
- Keep every paragraph to 2-3 sentences maximum — no walls of text
- Prefer bullet-point lists (- item) over dense paragraphs
- Bold (**word**) the single most important term in each section
- Avoid wide multi-column tables; use simple bullet lists instead
- Lead with the key answer in the very first sentence
- Use short, clear sentences
"""


# ---------------------------------------------------------------------------
# Source record type
# ---------------------------------------------------------------------------

SourceRecord = Dict[str, object]


# ---------------------------------------------------------------------------
# Search pipeline
# ---------------------------------------------------------------------------


class SearchPipeline:
    """
    Orchestrates the full search pipeline:
      query expansion → multi-search → page fetch → gap analysis → synthesise.

    Mirrors the approach used by Brave Search AI and LM Studio's tool-use search:
    the LLM actively decides what to search, reads actual page content (not just
    snippets), and can issue follow-up queries when gaps are identified.
    """

    def __init__(
        self,
        searxng_client: SearXNGClient,
        llm_client: LLMClient,
        on_event: Callable[[SearchEvent], Awaitable[None]],
    ) -> None:
        self.searxng = searxng_client
        self.llm = llm_client
        self.on_event = on_event

    # ── Query expansion ────────────────────────────────────────────────────

    async def _expand_query(self, query: str, n: int = 5) -> List[str]:
        """Use the LLM to generate n targeted, diverse search queries.

        Falls back to [query] if the LLM returns nothing useful.
        """
        prompt = QUERY_EXPANSION_PROMPT.format(query=query, n=n)
        try:
            content, thinking = await self.llm.chat_completion_thinking(
                messages=[
                    {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=400,
            )
            if thinking:
                await self.on_event(
                    SearchEvent(
                        "thinking", {"content": thinking, "label": "Query planning"}
                    )
                )
            queries = [
                line.replace("QUERY:", "").strip()
                for line in content.split("\n")
                if line.strip().startswith("QUERY:")
            ][:n]
            if queries:
                logger.info(
                    "Expanded '%s' → %d queries: %s", query, len(queries), queries
                )
                return queries
        except Exception as e:
            logger.warning(f"Query expansion failed: {e}")
        return [query]

    # ── Gap analysis ───────────────────────────────────────────────────────

    async def _gap_analysis(
        self, query: str, findings_summary: str, n: int = 2
    ) -> List[str]:
        """Ask the LLM if additional searches are needed to fill gaps.

        Returns a list of follow-up queries (may be empty if results are sufficient).
        """
        prompt = GAP_ANALYSIS_PROMPT.format(
            query=query,
            findings_summary=findings_summary[:3000],
            n=n,
        )
        try:
            content, _ = await self.llm.chat_completion_thinking(
                messages=[
                    {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
                max_tokens=200,
            )
            queries = [
                line.replace("QUERY:", "").strip()
                for line in content.split("\n")
                if line.strip().startswith("QUERY:")
            ][:n]
            return queries
        except Exception as e:
            logger.warning(f"Gap analysis failed: {e}")
            return []

    # ── Main search pipeline ───────────────────────────────────────────────

    async def normal_search(
        self,
        query: str,
        history: list | None = None,
        is_mobile: bool = False,
    ) -> Tuple[str, List[SourceRecord]]:
        """
        Full search pipeline:
          1. LLM expands the query into 5 targeted queries (different angles)
          2. All queries are searched; results are deduplicated
          3. Full page content fetched for top results
          4. LLM identifies gaps → 1-2 follow-up searches
          5. Final synthesis using all gathered context + conversation history

        When is_mobile=True the synthesis prompt instructs the LLM to produce
        short paragraphs, bullet points and bold key terms suitable for a
        narrow phone screen.

        Returns (answer_text, sources).
        """
        history = history or []

        # ── Step 1: Query expansion ──────────────────────────────────────────
        await self.on_event(
            SearchEvent("analyze", {"status": "Planning search strategy…"})
        )
        expanded = await self._expand_query(query, n=5)
        await self.on_event(
            SearchEvent("queries", {"queries": expanded, "original": query})
        )

        # ── Step 2: Search all expanded queries ──────────────────────────────
        sources: List[SourceRecord] = []
        snippet_map: Dict[int, str] = {}  # src_num → snippet, for fallback merge
        seen_urls: set = set()
        src_num = 0

        for eq in expanded:
            await self.on_event(SearchEvent("search", {"query": eq}))
            try:
                results = await self.searxng.search(eq, num_results=7)
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
                snippet_map[src_num] = r.snippet
                sources.append({"num": src_num, "title": r.title, "url": r.url})

        if not sources:
            await self.on_event(SearchEvent("complete", {"status": "Done"}))
            return f"No search results found for '{query}'.", []

        # ── Step 3: Fetch full page content for top sources ──────────────────
        await self.on_event(SearchEvent("analyze", {"status": "Reading top pages…"}))

        # Fetch top 4 pages concurrently; keeps prompt size manageable
        pages_to_fetch = [s for s in sources[:4]]
        fetch_tasks = [fetch_page_content(str(s["url"])) for s in pages_to_fetch]

        page_contents = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        enriched_text = ""
        for s, content in zip(pages_to_fetch, page_contents):
            if isinstance(content, Exception) or not content:
                continue
            await self.on_event(
                SearchEvent(
                    "fetch_page", {"url": str(s["url"]), "title": str(s["title"])}
                )
            )
            enriched_text += (
                f"[Source {s['num']}] {s['title']}\nURL: {s['url']}\n"
                f"Full content:\n{content[:2500]}\n\n"  # cap per-page to stay within budget
            )

        # Merge enriched page content with snippet-only results
        fetched_nums = {
            s["num"]
            for s, c in zip(pages_to_fetch, page_contents)
            if not isinstance(c, Exception) and c
        }
        snippet_only_text = "".join(
            f"[Source {s['num']}] {s['title']}\nURL: {s['url']}\nSnippet: {snippet_map.get(s['num'], '')}\n\n"
            for s in sources
            if s["num"] not in fetched_nums
        )
        combined_text = enriched_text + snippet_only_text

        # ── Step 4: Gap analysis — follow-up searches ─────────────────────────
        await self.on_event(
            SearchEvent("analyze", {"status": "Checking for information gaps…"})
        )

        # Summarise what we have so far for the gap-analysis prompt
        findings_summary = "\n".join(
            f"- [{s['num']}] {s['title']} ({s['url']})" for s in sources[:15]
        )
        gap_queries = await self._gap_analysis(query, findings_summary, n=2)

        for gq in gap_queries:
            await self.on_event(SearchEvent("search", {"query": gq}))
            try:
                extra_results = await self.searxng.search(gq, num_results=5)
            except Exception as e:
                logger.warning(f"Gap search failed for '{gq}': {e}")
                extra_results = []

            await self.on_event(
                SearchEvent(
                    "search_complete", {"query": gq, "result_count": len(extra_results)}
                )
            )

            for r in extra_results:
                if r.url in seen_urls:
                    continue
                seen_urls.add(r.url)
                src_num += 1
                combined_text += f"[Source {src_num}] {r.title}\nURL: {r.url}\nSnippet: {r.snippet}\n\n"
                sources.append({"num": src_num, "title": r.title, "url": r.url})

        # ── Step 5: Final synthesis ───────────────────────────────────────────
        await self.on_event(SearchEvent("analyze", {"status": "Synthesizing answer…"}))

        conversation_context = _build_conversation_context(history)
        base_prompt = SYNTHESIZE_PROMPT + (
            MOBILE_FORMAT_INSTRUCTION if is_mobile else ""
        )
        prompt = base_prompt.format(
            query=query,
            conversation_context=conversation_context,
            results_text=_trim_findings(combined_text),
        )

        answer, answer_thinking = await self.llm.chat_completion_thinking(
            messages=[
                {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=3072,
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
