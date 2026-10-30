"""Bounded Gitee multimodal embedding and independent visual-caption requests."""

from __future__ import annotations

import base64
import io
import json
import math
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from PIL import Image

from .settings import CaptionConfig, IndexConfig

CAPTION_PROMPT = "Describe this scientific image for retrieval. State visible modules, arrows, labels, axes, legends, trends and layout. Use the supplied source caption only as context. Do not invent experimental results or unseen details. Return concise factual prose, preserving exact technical labels."


def normalize(vector: list[float], dimensions: int) -> list[float]:
    if len(vector) != dimensions or any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
        for x in vector
    ):
        raise ValueError("Embedding response has invalid dimensions or non-finite values")
    norm = math.sqrt(sum(x * x for x in vector))
    if norm <= 0:
        raise ValueError("Embedding response contains a zero vector")
    return [float(x / norm) for x in vector]


def image_data(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Image must be an existing file no larger than 20 MiB")
    with Image.open(path) as original:
        if original.width * original.height > 40_000_000:
            raise ValueError("Image exceeds the 40 megapixel limit")
        image = original.convert("RGB")
        image.thumbnail((1600, 1600))
        if min(image.size) < 32:
            image = image.resize((max(32, image.width), max(32, image.height)))
        stream = io.BytesIO()
        image.save(stream, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode()


class InferenceClient:
    def __init__(
        self,
        index: IndexConfig,
        caption: CaptionConfig,
        env: Mapping[str, str] | None = None,
        check: Callable[[], None] | None = None,
    ):
        self.index, self.caption = index, caption
        self.env = os.environ if env is None else env
        self.check = check or (lambda: None)

    def ready(self, caption: bool = False) -> bool:
        config = self.caption if caption else self.index
        return bool(self.env.get(config.api_key_env))

    def joint_ready(self) -> bool:
        return bool(self.index.joint_base_url and self.env.get(self.index.joint_key_env))

    def _post(
        self, config: IndexConfig | CaptionConfig, route: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        key = self.env.get(config.api_key_env)
        if not key:
            raise ValueError(f"Set {config.api_key_env} in the server environment")
        encoded = json.dumps(body).encode()
        for attempt in range(3):
            self.check()
            request = urllib.request.Request(
                config.base_url.rstrip("/") + route,
                data=encoded,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=config.timeout) as response:
                    content = response.read(32 * 1024 * 1024 + 1)
                    if len(content) > 32 * 1024 * 1024:
                        raise ValueError("Inference response exceeds 32 MiB")
                    payload = json.loads(content)
                    if not isinstance(payload, dict):
                        raise ValueError("Invalid inference response")
                    self.check()
                    return payload
            except urllib.error.HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                hint = (
                    " Verify that this endpoint supports native image+caption samples."
                    if route == "/embeddings" and exc.code == 400
                    else ""
                )
                raise ValueError(f"Inference service returned HTTP {exc.code}.{hint}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 2:
                    raise ValueError("Inference service is unreachable or timed out") from exc
                time.sleep(attempt + 1)
        raise ValueError("Inference request failed")

    def embed(self, inputs: list[dict[str, str]]) -> list[list[float]]:
        if not inputs:
            return []
        payload = self._post(
            self.index,
            "/embeddings",
            {
                "model": self.index.model,
                "input": inputs,
                "dimensions": self.index.dimensions,
                "encoding_format": "float",
            },
        )
        model = str(payload.get("model", "")).split("/")[-1].lower()
        if model != self.index.model.lower():
            raise ValueError("Embedding service returned a different model")
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(inputs):
            raise ValueError("Embedding response count does not match inputs")
        ordered = sorted(data, key=lambda x: x.get("index", -1))
        if [x.get("index") for x in ordered] != list(range(len(inputs))):
            raise ValueError("Embedding response indices are invalid")
        return [normalize(item.get("embedding", []), self.index.dimensions) for item in ordered]

    def describe(self, image: str, caption: str, context: str) -> str:
        payload = self._post(
            self.caption,
            "/chat/completions",
            {
                "model": self.caption.model,
                "messages": [
                    {"role": "system", "content": CAPTION_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image}},
                            {
                                "type": "text",
                                "text": "Original caption: "
                                + caption[:3000]
                                + "\nAssociated source text: "
                                + context[:5000],
                            },
                        ],
                    },
                ],
                "max_tokens": self.caption.max_tokens,
                "temperature": 0.1,
                "stream": False,
                **(
                    {"thinking": {"type": "disabled"}}
                    if "deepseek" in self.caption.model.lower()
                    else {"enable_thinking": False}
                ),
            },
        )
        try:
            description = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ValueError("Caption service returned no description") from None
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Caption service returned an empty description")
        return description.strip()[:12000]

    def combined(
        self, image: str, text: str, visual: list[float], textual: list[float]
    ) -> list[float]:
        if not self.joint_ready():
            raise ValueError(
                "Configure joint_base_url and its API key for native Qwen3-VL-Embedding-8B image+caption encoding"
            )
        return self.native_query(text, image)

    def native_query(self, text: str = "", image: str = "") -> list[float]:
        if not self.joint_ready():
            raise ValueError(f"Configure {self.index.joint_key_env} for native fusion")
        config = replace(
            self.index, base_url=self.index.joint_base_url, api_key_env=self.index.joint_key_env
        )
        if self.index.joint_protocol == "dashscope":
            contents = []
            if text:
                contents.append({"text": text})
            if image:
                contents.append({"image": image})
            payload = self._post(
                config,
                "/services/embeddings/multimodal-embedding/multimodal-embedding",
                {
                    "model": self.index.joint_model,
                    "input": {"contents": contents},
                    "parameters": {"dimension": self.index.joint_dimensions, "enable_fusion": True},
                },
            )
            try:
                embeddings = payload["output"]["embeddings"]
                if len(embeddings) != 1:
                    raise ValueError("Native fusion returned multiple independent embeddings")
                if text and image and embeddings[0].get("type") not in {"fusion", "fused"}:
                    raise ValueError("Native service did not confirm a fused image/text output")
                vector = embeddings[0]["embedding"]
            except (KeyError, TypeError, IndexError) as exc:
                raise ValueError("Invalid DashScope native fusion response") from exc
            return normalize(vector, self.index.joint_dimensions)
        payload = self._post(
            config,
            "/embeddings",
            {
                "model": self.index.joint_model,
                "input": [
                    {**({"text": text} if text else {}), **({"image": image} if image else {})}
                ],
                "dimensions": self.index.joint_dimensions,
                "encoding_format": "float",
            },
        )
        if str(payload.get("model", "")).split("/")[-1].lower() != self.index.joint_model.lower():
            raise ValueError("Native embedding service returned a different model")
        try:
            rows = payload["data"]
            if len(rows) != 1:
                raise ValueError("Native fusion response must contain one sample")
            vector = rows[0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Invalid native fusion response") from exc
        return normalize(vector, self.index.joint_dimensions)
