"""
Undermind configuration.

Defaults are defined here as dataclasses; an optional TOML file overrides them.
The file is looked up at the path in the UNDERMIND_CONFIG environment variable,
falling back to config.toml at the repository root.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_CONFIG_FILENAME = "config.toml"
ENV_CONFIG_PATH = "UNDERMIND_CONFIG"

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class DraftConfig:
    """The local draft model (Krios) used for inline sentence prediction."""

    kind: str = "openai_compat"
    base_url: str = "http://localhost:13305"
    model: str = "Qwen3-8B-GGUF"
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 64
    debounce_s: float = 0.35
    timeout_s: float = 30.0
    min_buffer_chars: int = 4


@dataclass
class ConfidenceConfig:
    """Token-confidence tracking and the 95% handoff trigger."""

    threshold: float = 0.95
    mode: str = "latest"
    ewma_alpha: float = 0.4
    min_tokens: int = 2
    min_chars: int = 8


@dataclass
class PrimaryConfig:
    """The primary large LLM context pipeline that executes handed-off branches."""

    kind: str = "openai_compat"
    base_url: str = "http://localhost:13305"
    model: str = "Qwen3-8B-GGUF"
    api_key: str = ""
    system_prompt: str = (
        "You are the primary reasoning engine of a local assistant pipeline. "
        "Execute the handed-off text branch directly and respond concisely."
    )
    webhook_url: str = ""
    timeout_s: float = 120.0
    retries: int = 1
    # Optional featherweight fallback (fail-open chain; see providers/fallback.py)
    fallback_kind: str = ""
    fallback_base_url: str = ""
    fallback_model: str = ""
    fallback_api_key: str = ""
    # Wall-clock cap for the fallback rung of the chain; the primary rung is
    # capped at timeout_s. A rung that exceeds its cap is abandoned and the
    # chain fails over instead of stalling the turn.
    fallback_timeout_s: float = 60.0
    # Max silent gap (seconds) allowed while streaming a primary execution:
    # bounds a hung engine without cutting off long thinking turns, whose
    # token stream pauses only briefly. 0 disables stall detection.
    stall_timeout_s: float = 90.0


@dataclass
class DaydreamConfig:
    """Background daydreaming loop settings."""

    idle_threshold_s: float = 5.0
    min_intent_count: int = 2
    export_path: str = "data/training_export.jsonl"
    poll_interval_s: float = 0.5
    max_samples_per_intent: int = 20
    # Near-duplicate intents whose word-set Jaccard similarity meets this
    # threshold merge into one bucket (0 disables merging).
    merge_similarity: float = 0.6


@dataclass
class StoreConfig:
    """SQLite persistence settings."""

    db_path: str = "data/undermind.db"


@dataclass
class ProxyConfig:
    """HTTP proxy frontend settings."""

    host: str = "127.0.0.1"
    port: int = 11435
    cache_ttl_s: float = 30.0


@dataclass
class Config:
    """Top-level Undermind configuration."""

    draft: DraftConfig = field(default_factory=DraftConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    primary: PrimaryConfig = field(default_factory=PrimaryConfig)
    daydream: DaydreamConfig = field(default_factory=DaydreamConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    source_path: str | None = None


_SECTION_TYPES = {
    "draft": DraftConfig,
    "confidence": ConfidenceConfig,
    "primary": PrimaryConfig,
    "daydream": DaydreamConfig,
    "store": StoreConfig,
    "proxy": ProxyConfig,
}


def _apply_section(config: Config, section: str, values: dict) -> None:
    section_type = _SECTION_TYPES[section]
    current = getattr(config, section)
    valid_fields = set(section_type.__dataclass_fields__)
    unknown = sorted(set(values) - valid_fields)
    if unknown:
        raise ValueError(
            f"Unknown key(s) in [{section}] of config file: {', '.join(unknown)}"
        )
    setattr(config, section, replace(current, **values))


def _resolve_path(path_str: str | None) -> Path | None:
    if not path_str:
        return None
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def load_config(path: str | os.PathLike | None = None) -> Config:
    """
    Build a Config from defaults, optionally overridden by a TOML file.

    Lookup order for the file:
    1. Explicit ``path`` argument.
    2. ``$UNDERMIND_CONFIG``.
    3. ``config.toml`` at the repository root (used only if it exists).
    Missing or unreadable files fall back to defaults silently.
    """
    config = Config()

    if path is not None:
        candidates = [_resolve_path(str(path))]
    elif os.environ.get(ENV_CONFIG_PATH):
        candidates = [_resolve_path(os.environ[ENV_CONFIG_PATH])]
    else:
        candidates = [REPO_ROOT / DEFAULT_CONFIG_FILENAME]

    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            try:
                with open(candidate, "rb") as fh:
                    raw = tomllib.load(fh)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ValueError(f"Cannot read config file {candidate}: {exc}") from exc
            for section, values in raw.items():
                if section not in _SECTION_TYPES:
                    raise ValueError(
                        f"Unknown section [{section}] in config file {candidate}"
                    )
                if not isinstance(values, dict):
                    raise ValueError(
                        f"Section [{section}] in {candidate} must be a table"
                    )
                _apply_section(config, section, values)
            config.source_path = str(candidate)
            break

    return config
