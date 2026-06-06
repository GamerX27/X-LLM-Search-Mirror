"""
Configuration management for X-LLM-Search.

Default URLs use Docker Compose service names so containers can
talk to each other over the internal Docker network:
  - LLM backend : http://ollama:11434   (Ollama container)
  - SearXNG     : http://searxng:8080   (SearXNG container)
"""

import json
from pathlib import Path

from pydantic import BaseModel, Field

CONFIG_DIR = Path("/app/config")
CONFIG_FILE = CONFIG_DIR / "settings.json"


class LLMBackendConfig(BaseModel):
    type: str = Field(default="ollama")
    # Use the Compose service name — resolves inside the Docker network
    url: str = Field(default="http://ollama:11434")
    model: str = Field(default="")  # populated at runtime via GET /v1/models


class SearXNGConfig(BaseModel):
    url: str = Field(default="http://searxng:8080")
    format_: str = Field(default="json", alias="format")


class AppSettings(BaseModel):
    llm_backend: LLMBackendConfig = Field(default_factory=LLMBackendConfig)
    searxng: SearXNGConfig = Field(default_factory=SearXNGConfig)


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> AppSettings:
    ensure_config_dir()
    if CONFIG_FILE.exists():
        try:
            return AppSettings(**json.loads(CONFIG_FILE.read_text()))
        except Exception:
            pass
    settings = AppSettings()
    save_settings(settings)
    return settings


def save_settings(settings: AppSettings) -> None:
    ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(settings.model_dump(), indent=2))
