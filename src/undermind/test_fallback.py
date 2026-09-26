"""Tests for the fail-open FallbackProvider chain."""

from __future__ import annotations

import unittest

from undermind.config import Config, PrimaryConfig
from undermind.providers.base import PrimaryProvider, ProviderError
from undermind.providers.fallback import (
    FallbackProvider,
    _derive_fallback_config,
    build_primary_chain,
)


class _Stub(PrimaryProvider):
    def __init__(self, name: str, fail: bool = False, reply: str = "") -> None:
        self.name = name
        self.fail = fail
        self.reply = reply or f"ok-{name}"
        self.calls: list[str] = []

    def execute(self, branch: str, context: str | None = None) -> str:
        self.calls.append(branch)
        if self.fail:
            raise ProviderError(f"{self.name} is down")
        return self.reply


class FallbackProviderTests(unittest.TestCase):
    def test_first_provider_answers(self) -> None:
        chain = FallbackProvider([_Stub("a"), _Stub("b")])
        self.assertEqual(chain.execute("hi"), "ok-a")
        self.assertEqual(chain.calls if hasattr(chain, "calls") else [], [])

    def test_falls_through_on_failure(self) -> None:
        primary = _Stub("primary", fail=True)
        fallback = _Stub("fallback")
        chain = FallbackProvider([primary, fallback])
        self.assertEqual(chain.execute("hi"), "ok-fallback")
        self.assertEqual(primary.calls, ["hi"])
        self.assertEqual(fallback.calls, ["hi"])

    def test_raises_when_chain_exhausted(self) -> None:
        chain = FallbackProvider([_Stub("a", fail=True), _Stub("b", fail=True)])
        with self.assertRaises(ProviderError):
            chain.execute("hi")

    def test_empty_chain_rejected(self) -> None:
        with self.assertRaises(ProviderError):
            FallbackProvider([])

    def test_context_forwarded(self) -> None:
        stub = _Stub("a")
        chain = FallbackProvider([stub])
        chain.execute("hi", context="sys")
        self.assertEqual(stub.calls, ["hi"])

    def test_chain_summary(self) -> None:
        chain = FallbackProvider([_Stub("a"), _Stub("b")])
        self.assertEqual(chain.chain_summary, "_Stub -> _Stub")


class BuildPrimaryChainTests(unittest.TestCase):
    def _config(self, **primary_kwargs) -> Config:
        config = Config()
        config.primary = PrimaryConfig(kind="ollama", model="big:latest", **primary_kwargs)
        return config

    def test_no_fallback_returns_plain_provider(self) -> None:
        provider, desc = build_primary_chain(self._config())
        self.assertNotIsInstance(provider, FallbackProvider)
        self.assertEqual(desc, "ollama:big:latest")

    def test_fallback_config_derived(self) -> None:
        config = self._config(
            fallback_kind="openai_compat",
            fallback_base_url="http://localhost:13305",
            fallback_model="Bonsai-4B-Q1_0",
        )
        fb = _derive_fallback_config(config, "openai_compat")
        self.assertEqual(fb.kind, "openai_compat")
        self.assertEqual(fb.base_url, "http://localhost:13305")
        self.assertEqual(fb.model, "Bonsai-4B-Q1_0")
        # system prompt and other fields carry over
        self.assertEqual(fb.system_prompt, config.primary.system_prompt)

    def test_fallback_kind_without_fields_uses_primary_base(self) -> None:
        config = self._config(fallback_kind="ollama", fallback_model="small:latest")
        fb = _derive_fallback_config(config, "ollama")
        self.assertEqual(fb.base_url, config.primary.base_url)
        self.assertEqual(fb.model, "small:latest")

    def test_chain_built_with_fallback(self) -> None:
        config = self._config(
            fallback_kind="openai_compat",
            fallback_base_url="http://localhost:13305",
            fallback_model="Bonsai-4B-Q1_0",
        )
        provider, desc = build_primary_chain(config)
        self.assertIsInstance(provider, FallbackProvider)
        self.assertIn("->", desc)
        self.assertIn("Bonsai-4B-Q1_0", desc)

    def test_derive_tolerates_bare_primary_config(self) -> None:
        # Defensive: a bare PrimaryConfig (no wrapping Config) must not crash.
        fb = _derive_fallback_config(
            PrimaryConfig(kind="ollama", model="big:latest", fallback_kind="ollama"),
            "ollama",
        )
        self.assertEqual(fb.kind, "ollama")


from undermind.providers.fallback import FallbackProvider


class _Ok:
    def __init__(self, tag=""):
        self.tag = tag

    def execute(self, branch, context=None):
        return f"ok:{self.tag}"


class _Boom:
    def execute(self, branch, context=None):
        raise ProviderError("down")


class TestServedAttribution(unittest.TestCase):
    def test_last_served_primary(self):
        chain = FallbackProvider([_Ok("p1"), _Ok("p2")])
        self.assertEqual(chain.execute("x"), "ok:p1")
        self.assertEqual(chain.last_served, 0)
        self.assertIsNotNone(chain.last_latency_ms)

    def test_last_served_fallback(self):
        chain = FallbackProvider([_Boom(), _Ok("p2")])
        self.assertEqual(chain.execute("x"), "ok:p2")
        self.assertEqual(chain.last_served, 1)
        self.assertIsNotNone(chain.last_latency_ms)

    def test_last_served_none_on_total_failure(self):
        chain = FallbackProvider([_Boom(), _Boom()])
        with self.assertRaises(ProviderError):
            chain.execute("x")
        self.assertIsNone(chain.last_served)
        self.assertIsNone(chain.last_latency_ms)


if __name__ == "__main__":
    unittest.main()
