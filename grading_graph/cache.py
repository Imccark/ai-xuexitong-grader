from __future__ import annotations

from pathlib import Path
from typing import Any

from grading_graph.store import atomic_write_json, canonical_hash, file_sha256
from grading_graph.nodes.image_quality import RECTIFICATION_VERSION


class JsonResponseCache:
    """Small, content-addressed cache for deterministic provider responses."""

    def __init__(self, root: Path | str, *, preprocess_version: str = RECTIFICATION_VERSION) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.preprocess_version = str(preprocess_version)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(
        *,
        prompt: str,
        schema: dict[str, Any],
        image_ref: str | list[str] | tuple[str, ...] | None = None,
        model: str = "",
        preprocess_version: str = "",
    ) -> str:
        raw_refs = [] if image_ref is None else ([image_ref] if isinstance(image_ref, str) else list(image_ref))
        image_fingerprints: list[str] = []
        for raw_ref in raw_refs:
            image_path = Path(str(raw_ref))
            image_fingerprints.append(
                f"sha256:{file_sha256(image_path)}" if image_path.is_file() else str(raw_ref)
            )
        return canonical_hash(
            {
                "prompt": prompt,
                "schema": schema,
                "image_refs": image_fingerprints,
                "model": model,
                "preprocess_version": preprocess_version,
            }
        )

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.root / f"{key}.json"
        if not path.is_file():
            self.misses += 1
            return None
        import json

        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"corrupt provider cache entry: {path}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"provider cache entry must be an object: {path}")
        self.hits += 1
        return value

    def put(self, key: str, value: dict[str, Any]) -> None:
        atomic_write_json(self.root / f"{key}.json", value)


class CachedJsonProvider:
    def __init__(self, provider: Any, cache: JsonResponseCache, *, model: str | None = None) -> None:
        self.provider = provider
        self.cache = cache
        self.model = str(getattr(provider, "model", "") if model is None else model)

    def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        image_ref: str | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        key = self.cache.key(
            prompt=prompt,
            schema=schema,
            image_ref=image_ref,
            model=self.model,
            preprocess_version=self.cache.preprocess_version,
        )
        cached = self.cache.get(key)
        if cached is not None:
            normalized = self._normalize_compat(cached, schema)
            if normalized is not None:
                return normalized
        if image_ref is None:
            value = self.provider.complete_json(prompt, schema)
        else:
            value = self.provider.complete_json(prompt, schema, image_ref=image_ref)
        if not isinstance(value, dict):
            raise ValueError("cached JSON provider response must be an object")
        self.cache.put(key, value)
        return self._normalize_compat(value, schema)

    @staticmethod
    def _normalize_compat(value: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any] | None:
        """Apply the same narrow Qwen transcriber shape compatibility on cache hits."""
        properties = schema.get("properties", {}) or {}
        if "spans" not in properties and "symbol_candidates" not in properties:
            return value
        if "symbol_candidates" in properties:
            if "symbol_candidates" in value:
                return value
            if isinstance(value, list):
                return {"symbol_candidates": value}
            if "symbols" in value:
                return {**value, "symbol_candidates": value.get("symbols", [])}
            return None
        if "spans" in value:
            return value
        if "content" in value:
            nested = value.get("content")
            if isinstance(nested, str):
                import json

                try:
                    nested = json.loads(nested)
                except ValueError:
                    nested = None
            if isinstance(nested, dict):
                return CachedJsonProvider._normalize_compat(nested, schema)
        if "lines" in value:
            return {"spans": value.get("lines", [])}
        for alias in ("transcription", "transcriptions", "ocr", "results"):
            if alias in value:
                aliased = value.get(alias, [])
                if isinstance(aliased, dict):
                    aliased = [aliased]
                elif isinstance(aliased, str):
                    aliased = [{"text": aliased}]
                return {"spans": aliased}
        if "span_id" in value:
            return {"spans": [value]}
        # A cache entry from another graph node (for example page
        # ``regions``) is not a valid transcriber response.  Treat it as a
        # miss so the provider can produce a fresh response instead of
        # surfacing a misleading TranscriptionProviderError.
        return None
