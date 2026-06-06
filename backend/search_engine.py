"""
SearXNG search engine integration.

Queries a self-hosted SearXNG instance and returns structured results
(titles, URLs, snippets) for the search pipeline.
"""

import logging
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)


class SearchResult:
    """A single search result from SearXNG."""

    def __init__(self, title: str, url: str, snippet: str, source: str) -> None:
        self.title = title
        self.url = url
        self.snippet = snippet
        self.source = source  # e.g. "google", "duckduckgo"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
        }


class SearXNGClient:
    """Client for querying an external SearXNG instance."""

    def __init__(self, base_url: str) -> None:
        # Ensure the URL ends without a trailing slash for clean concatenation
        self.base_url = base_url.rstrip("/")
        self.http_client = httpx.AsyncClient(timeout=30.0)

    async def search(
        self,
        query: str,
        num_results: int = 10,
        categories: Optional[str] = None,
        engines: Optional[str] = None,
    ) -> List[SearchResult]:
        """
        Perform a web search via SearXNG.

        Args:
            query: The search query string.
            num_results: Maximum number of results to return.
            categories: Comma-separated categories (e.g. "general,news").
            engines: Comma-separated engine names (e.g. "google,duckduckgo").

        Returns:
            List of SearchResult objects.
        """
        params = {
            "q": query,
            "format": "json",
            "number": num_results,
        }

        if categories:
            params["categories"] = categories
        if engines:
            params["engines"] = engines

        url = f"{self.base_url}/search"

        try:
            response = await self.http_client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("results", []):
                result = SearchResult(
                    title=item.get("title", "Untitled"),
                    url=item.get("url", ""),
                    snippet=item.get("content", "")[:500],  # Truncate long snippets
                    source=item.get("engine", "unknown"),
                )
                results.append(result)

            logger.info(f"SearXNG search for '{query}' returned {len(results)} results")
            return results

        except httpx.HTTPStatusError as e:
            logger.error(
                f"SearXNG HTTP error: {e.response.status_code} - {e.response.text}"
            )
            raise
        except httpx.RequestError as e:
            logger.error(f"SearXNG request failed: {e}")
            raise

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self.http_client.aclose()
