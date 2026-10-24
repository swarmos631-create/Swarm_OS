"""Provider-independent local model runtime.

Providers:
* ``sklearn``  — artifacts produced by the SwarmOS training pipeline (always available once trained).
* ``ollama``   — optional local Ollama server (loopback only unless explicitly allowed).
* ``llamacpp`` — optional local llama.cpp ``llama-server`` (loopback only unless explicitly allowed).
* ``rules``    — deterministic fallback for supported tasks.

There are deliberately **no hosted LLM adapters** (OpenAI, Anthropic, Gemini, …). Nothing in this module can
route a request off-host unless an operator sets SWARMOS_ALLOW_REMOTE_MODEL_HOSTS for a self-hosted server.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from swarmos.config import Settings, get_settings
from swarmos.db import session_scope
from swarmos.models import ModelVersion

log = logging.getLogger("swarmos.ai")
T = TypeVar("T", bound=BaseModel)


class ModelUnavailable(Exception):
    pass


@dataclass
class LoadedModel:
    name: str
    version: str
    algorithm: str
    estimator: Any
    feature_spec: dict[str, Any]
    metrics: dict[str, Any]

    @property
    def label(self) -> str:
        return f"model:{self.name}@{self.version}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SklearnArtifacts:
    """Loads the *promoted* version of a trained model, verifying the artifact checksum."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[str, tuple[float, LoadedModel | None, str | None]] = {}
        self._lock = threading.Lock()

    def load(self, name: str, ttl_s: float = 30.0) -> LoadedModel:
        with self._lock:
            cached = self._cache.get(name)
            if cached and time.monotonic() - cached[0] < ttl_s:
                if cached[1] is None:
                    raise ModelUnavailable(cached[2] or f"No promoted model '{name}'")
                return cached[1]
        try:
            model = self._load_uncached(name)
            err = None
        except ModelUnavailable as exc:
            model, err = None, str(exc)
        with self._lock:
            self._cache[name] = (time.monotonic(), model, err)
        if model is None:
            raise ModelUnavailable(err or name)
        return model

    def _load_uncached(self, name: str) -> LoadedModel:
        with session_scope() as db:
            mv = db.scalars(
                select(ModelVersion).where(ModelVersion.model_name == name, ModelVersion.status == "promoted")
            ).first()
            if mv is None:
                raise ModelUnavailable(f"No promoted version of '{name}'. Train and promote it in the Model Lab.")
            from swarmos.ml.pipeline import artifact_bytes, training_available

            if not training_available():
                raise ModelUnavailable(f"scikit-learn is not installed in this deployment; '{name}' uses its deterministic fallback")
            import joblib

            data = artifact_bytes(mv, self.settings)
            if data is None:
                raise ModelUnavailable(f"Artifact for {name}@{mv.version} is missing or fails its checksum; refusing to load")
            # Produced by our own training pipeline and checksum-verified against the registry before unpickling.
            bundle = joblib.load(io.BytesIO(data))
            return LoadedModel(
                name=name, version=mv.version, algorithm=mv.algorithm, estimator=bundle["estimator"],
                feature_spec=bundle.get("feature_spec", {}), metrics=mv.metrics,
            )

    def invalidate(self, name: str | None = None) -> None:
        with self._lock:
            if name:
                self._cache.pop(name, None)
            else:
                self._cache.clear()


class LocalLLM:
    """Client for a local inference server (Ollama native API or llama.cpp's server)."""

    def __init__(self, provider: str, base_url: str, model: str, settings: Settings) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.settings = settings
        self._sem = threading.BoundedSemaphore(settings.model_max_concurrency)

    def health(self) -> dict[str, Any]:
        try:
            if self.provider == "ollama":
                r = httpx.get(f"{self.base_url}/api/tags", timeout=3)
                r.raise_for_status()
                names = [m.get("name") for m in r.json().get("models", [])]
                return {"available": self.model in names, "models": names,
                        "detail": None if self.model in names else f"Model '{self.model}' is not pulled"}
            r = httpx.get(f"{self.base_url}/health", timeout=3)
            return {"available": r.status_code == 200, "models": [], "detail": None if r.status_code == 200 else r.text[:200]}
        except httpx.HTTPError as exc:
            return {"available": False, "models": [], "detail": f"Unreachable: {type(exc).__name__}"}

    def generate(self, prompt: str, *, system: str, max_tokens: int = 400, json_schema: dict[str, Any] | None = None) -> str:
        if not self._sem.acquire(timeout=self.settings.model_request_timeout_seconds):
            raise ModelUnavailable("Local model is busy (concurrency limit reached)")
        try:
            timeout = self.settings.model_request_timeout_seconds
            if self.provider == "ollama":
                body: dict[str, Any] = {
                    "model": self.model, "prompt": prompt, "system": system, "stream": False,
                    "options": {"num_predict": max_tokens, "temperature": 0.2},
                }
                if json_schema:
                    body["format"] = json_schema
                r = httpx.post(f"{self.base_url}/api/generate", json=body, timeout=timeout)
                r.raise_for_status()
                return str(r.json().get("response", ""))
            body = {"prompt": f"{system}\n\n{prompt}", "n_predict": max_tokens, "temperature": 0.2}
            if json_schema:
                body["json_schema"] = json_schema
            r = httpx.post(f"{self.base_url}/completion", json=body, timeout=timeout)
            r.raise_for_status()
            return str(r.json().get("content", ""))
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"{self.provider} request failed: {type(exc).__name__}") from exc
        finally:
            self._sem.release()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.provider != "ollama":
            raise ModelUnavailable("Embeddings are only wired for Ollama")
        try:
            r = httpx.post(f"{self.base_url}/api/embed", json={"model": self.model, "input": texts},
                           timeout=self.settings.model_request_timeout_seconds)
            r.raise_for_status()
            return list(r.json()["embeddings"])
        except (httpx.HTTPError, KeyError) as exc:
            raise ModelUnavailable(f"Embedding request failed: {type(exc).__name__}") from exc


class ModelRuntime:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.artifacts = SklearnArtifacts(self.settings)
        self.llms: list[LocalLLM] = []
        if self.settings.ollama_url:
            self.llms.append(LocalLLM("ollama", self.settings.ollama_url, self.settings.ollama_model, self.settings))
        if self.settings.llamacpp_url:
            self.llms.append(LocalLLM("llamacpp", self.settings.llamacpp_url, "default", self.settings))
        self._llm_health: tuple[float, LocalLLM | None] = (0.0, None)

    # ---------------------------------------------------------------- classical models
    def load(self, name: str) -> LoadedModel:
        return self.artifacts.load(name)

    # ---------------------------------------------------------------- local LLM
    def _active_llm(self) -> LocalLLM | None:
        ts, llm = self._llm_health
        if time.monotonic() - ts < 60:
            return llm
        chosen = next((m for m in self.llms if m.health()["available"]), None)
        self._llm_health = (time.monotonic(), chosen)
        return chosen

    def generate_text(self, prompt: str, *, system: str, max_tokens: int = 400) -> tuple[str, str] | None:
        """Return (text, provider_label) or None when no local LLM is enabled/available."""
        if not self.settings.llm_narratives_enabled:
            return None
        llm = self._active_llm()
        if llm is None:
            return None
        try:
            return llm.generate(prompt, system=system, max_tokens=max_tokens), f"{llm.provider}:{llm.model}"
        except ModelUnavailable as exc:
            log.warning("llm_generate_failed", extra={"error": str(exc)})
            return None

    def generate_structured(self, prompt: str, schema: type[T], *, system: str) -> tuple[T, str] | None:
        if not self.settings.llm_narratives_enabled:
            return None
        llm = self._active_llm()
        if llm is None:
            return None
        try:
            raw = llm.generate(prompt, system=system, json_schema=schema.model_json_schema())
            return schema.model_validate(json.loads(raw)), f"{llm.provider}:{llm.model}"
        except (ModelUnavailable, ValidationError, json.JSONDecodeError) as exc:
            log.warning("llm_structured_failed", extra={"error": type(exc).__name__})
            return None

    def health(self) -> dict[str, Any]:
        providers: list[dict[str, Any]] = [
            {"id": "rules", "kind": "deterministic", "available": True, "endpoint": None,
             "detail": "Deterministic rules and templates; always available."},
        ]
        with session_scope() as db:
            promoted = db.scalars(select(ModelVersion).where(ModelVersion.status == "promoted")).all()
            providers.append({
                "id": "sklearn", "kind": "trained-artifacts", "available": bool(promoted), "endpoint": None,
                "detail": f"{len(promoted)} promoted model(s)" if promoted else "No promoted models yet",
                "models": [f"{m.model_name}@{m.version}" for m in promoted],
            })
        for name, url in (("ollama", self.settings.ollama_url), ("llamacpp", self.settings.llamacpp_url)):
            llm = next((m for m in self.llms if m.provider == name), None)
            if llm is None:
                providers.append({"id": name, "kind": "local-llm", "available": False, "endpoint": None,
                                  "detail": f"Not configured (set SWARMOS_{name.upper()}_URL)"})
            else:
                h = llm.health()
                providers.append({"id": name, "kind": "local-llm", "endpoint": url, **h})
        return {
            "providers": providers,
            "llm_narratives_enabled": self.settings.llm_narratives_enabled,
            "external_llm_providers": "none (not implemented by design)",
        }


_runtime: ModelRuntime | None = None


def get_runtime() -> ModelRuntime:
    global _runtime
    if _runtime is None:
        _runtime = ModelRuntime()
    return _runtime


def reset_runtime() -> None:
    global _runtime
    _runtime = None
