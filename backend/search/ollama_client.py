"""Minimal client for the local Ollama server - loopback only, local models only.

Two guards keep text on this machine: the host must be 127.0.0.1/localhost/::1 (an OLLAMA_HOST
pointing elsewhere is refused), and model names containing "cloud" (Ollama's hosted models)
are refused.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from urllib.parse import urlparse

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen3:14b")  # ~85% on a 12 GB GPU; 30b-a3b for harder questions


class OllamaError(RuntimeError):
    pass


def base_url() -> str:
    raw = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    url = raw if "://" in raw else f"http://{raw}"
    host = urlparse(url).hostname or ""
    if host not in LOOPBACK:
        raise OllamaError(f"OLLAMA_HOST={raw!r} is not a loopback address - refusing to send text there.")
    return url.rstrip("/")


def _check_model(model: str) -> str:
    if "cloud" in model.lower():
        raise OllamaError(f"Model {model!r} looks like an Ollama cloud model - refusing (local models only).")
    return model


def _post(path: str, payload: dict, timeout: float = 600, stream: bool = False):
    req = urllib.request.Request(base_url() + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise OllamaError(f"Ollama {path}: {e.code} {e.read().decode(errors='replace')[:300]}") from None
    except urllib.error.URLError as e:
        raise OllamaError(f"Ollama not reachable at {base_url()} ({e.reason}). Is Ollama running?") from None
    if stream:
        return resp
    with resp:
        return json.loads(resp.read())


def available_models() -> set[str]:
    try:
        with urllib.request.urlopen(base_url() + "/api/tags", timeout=5) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError):
        return set()
    names = set()
    for m in data.get("models", []):
        names.add(m["name"])
        if m["name"].endswith(":latest"):
            names.add(m["name"].removesuffix(":latest"))
    return names


def require(model: str) -> None:
    _check_model(model)
    if model not in available_models():
        raise OllamaError(f"Model {model!r} is not installed. Run: ollama pull {model}")


def embed(texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
    data = _post("/api/embed", {"model": _check_model(model), "input": texts, "keep_alive": "10m"})
    vectors = data.get("embeddings") or []
    if len(vectors) != len(texts):
        raise OllamaError(f"Expected {len(texts)} embeddings, got {len(vectors)}")
    return vectors


def chat_stream(messages: list[dict], model: str = CHAT_MODEL, num_ctx: int = 16384,
                temperature: float = 0.2) -> Iterator[tuple[str, str]]:
    """Yield ("thinking" | "answer", text) pieces.

    Thinking is requested explicitly: for qwen3 that is the only reliable way to keep the reasoning
    out of the answer (with think=false it leaks into the content). Models without thinking support
    are retried with think=false.
    """
    for think in (True, False):
        payload = {"model": _check_model(model), "messages": messages, "stream": True, "think": think,
                   "keep_alive": "10m", "options": {"num_ctx": num_ctx, "temperature": temperature}}
        try:
            with _post("/api/chat", payload, stream=True) as resp:
                for line in resp:
                    if not line.strip():
                        continue
                    part = json.loads(line)
                    if "error" in part:
                        raise OllamaError(part["error"])
                    msg = part.get("message", {})
                    if t := msg.get("thinking"):
                        yield "thinking", t
                    if t := msg.get("content"):
                        yield "answer", t
                    if part.get("done"):
                        # Ollama's own timings (nanoseconds) and token counts, as a JSON string
                        keys = ("load_duration", "prompt_eval_count", "prompt_eval_duration",
                                "eval_count", "eval_duration", "total_duration")
                        yield "stats", json.dumps({k: part.get(k) for k in keys})
                        return
        except OllamaError as e:
            if think and "think" in str(e).lower():
                continue  # model has no thinking support
            raise
