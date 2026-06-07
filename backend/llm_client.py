"""
LLM client that supports multiple backends via OpenAI-compatible APIs.

Supported backends:
- Ollama (http://host.docker.internal:1234)
- LM Studio (http://host.docker.internal:12111)
- Any OpenAI-compatible server

Both Ollama and LM Studio expose an OpenAI-compatible API endpoint, so we use
the official `openai` Python client with a custom base URL.
"""

import logging
import re
from typing import List, Tuple

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class LLMClient:
    """
    Client for interacting with local LLM backends.

    Uses the OpenAI-compatible API which is supported by Ollama, LM Studio,
    and most other local inference servers.
    """

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

        # OpenAI-compatible client pointing to the local backend
        self.client = AsyncOpenAI(
            api_key="local",  # Placeholder key; local backends ignore auth
            base_url=f"{self.base_url}/v1",
        )

    async def list_models(self) -> List[str]:
        """Fetch the list of available models from the backend."""
        try:
            response = await self.client.models.list()
            return [m.id for m in response.data]
        except Exception as e:
            logger.error(f"Failed to fetch models: {e}")
            return []

    async def chat_completion(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the response text."""
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = response.choices[0].message.content or ""
            logger.debug(f"LLM completion ({self.model}): {len(content)} chars")
            return content
        except Exception as e:
            logger.error(f"LLM request failed: {e}")
            raise

    async def chat_completion_thinking(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Tuple[str, str]:
        """
        Send a chat completion request and extract chain-of-thought reasoning.

        Works with:
        - Ollama native thinking models (think=True → message.thinking field)
        - Models that embed thinking in <think>...</think> tags in content
        - Regular models (returns empty string for thinking)

        Returns:
            (content, thinking) — thinking is empty string when not available.
        """
        # First attempt: request thinking via Ollama's native parameter
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body={"think": True},
            )
            raw = response.choices[0].message.content or ""
            thinking = ""

            # Ollama >= 0.7 exposes a dedicated .thinking field
            msg = response.choices[0].message
            native = getattr(msg, "thinking", None)
            if native:
                thinking = str(native).strip()

            # Fallback: parse <think>…</think> blocks embedded in content
            if not thinking and "<think>" in raw:
                m = re.search(r"<think>(.*?)</think>", raw, re.DOTALL)
                if m:
                    thinking = m.group(1).strip()
                    raw = re.sub(
                        r"<think>.*?</think>", "", raw, flags=re.DOTALL
                    ).strip()

            # Safety net: if stripping think tags left content empty but we have
            # native thinking, the model returned its answer inside the thinking
            # block only (common with some Ollama builds).  Surface it as content.
            if not raw and thinking:
                logger.warning(
                    "Content empty after think-tag stripping — using thinking as answer"
                )
                raw = thinking
                thinking = ""

            if thinking:
                logger.debug(
                    f"Thinking captured ({len(thinking)} chars), "
                    f"content {len(raw)} chars"
                )
            return raw, thinking

        except Exception as e:
            logger.warning(
                f"Thinking completion failed ({e}), retrying without think=True"
            )

        # Fallback: plain completion without thinking parameter
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            raw = response.choices[0].message.content or ""
            # Still try to extract <think> tags if model embeds them anyway
            thinking = ""
            if "<think>" in raw:
                m = re.search(r"<think>(.*?)</think>", raw, re.DOTALL)
                if m:
                    thinking = m.group(1).strip()
                    raw = re.sub(
                        r"<think>.*?</think>", "", raw, flags=re.DOTALL
                    ).strip()
            # Same safety net as above
            if not raw and thinking:
                logger.warning(
                    "Content empty after think-tag stripping (fallback path) — using thinking as answer"
                )
                raw = thinking
                thinking = ""
            return raw, thinking
        except Exception as e:
            logger.error(f"LLM request failed: {e}")
            raise

    async def chat_completion_vision(
        self,
        text_prompt: str,
        image_data_url: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        """
        Send a vision request: text prompt + image (data URL or https URL).

        Works with Ollama vision models (llava, bakllava, minicpm-v, etc.)
        and any OpenAI-compatible vision backend.

        Returns the model's text response.
        """
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": text_prompt},
                            {"type": "image_url", "image_url": {"url": image_data_url}},
                        ],
                    }
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"Vision request failed ({e}), falling back to text-only")
            # Fall back to text-only so the search still works
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": text_prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content or ""

    def get_model_info(self) -> dict:
        """Return current model configuration."""
        return {
            "type": "openai_compatible",
            "url": self.base_url,
            "model": self.model,
        }
