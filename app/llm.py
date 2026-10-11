"""The one wrapper for every model call (Tech Review 00 §4).

    parsed, run = llm.call("extract", "extract.v1", ExtractionResult, instructions, [text_part(...), image_part(...)],
                           doc_id="D-0003")

Rules:
- Output is schema-checked: OpenAI Responses API `responses.parse(text_format=<pydantic model>)`. Schemas must be
  strict-compatible: no free-form `dict`/`Any` fields; use nested models, lists and Optional instead.
- Effort and max output tokens per job come from config.JOBS; model from config.MODELS.
- Results are cached under STATE_DIR/cache/<job>/<input_hash>.json. Only real calls write the cache.
  `use_cache=False` (Re-run) always calls the model.
- An incomplete or refused response raises LLMError and records a failed Run.
- Every call appends a Run record (model, versions, input hash, latency, token usage) to STATE_DIR/runs.jsonl.
"""
from __future__ import annotations

import base64
import json
import time
from threading import BoundedSemaphore
from typing import Any, Optional, TypeVar

from pydantic import BaseModel, Field

from . import config
from .util import hash_obj, iso

T = TypeVar("T", bound=BaseModel)
_sem = BoundedSemaphore(config.LLM_CONCURRENCY)
_client = None


class LLMError(RuntimeError):
    pass


class Run(BaseModel):
    """One model call, appended to STATE_DIR/runs.jsonl."""
    run_id: str
    job: str
    doc_id: Optional[str] = None
    model: str
    prompt_version: str
    schema_version: str
    input_hash: str
    started_at: str
    duration_s: float = 0.0
    cached: bool = False
    status: str = "ok"
    error: Optional[str] = None
    usage: dict = Field(default_factory=dict)


_runs_lock = BoundedSemaphore(1)


def _next_run_id() -> str:
    f = config.STATE_DIR / "runs.jsonl"
    n = sum(1 for _ in f.open()) if f.exists() else 0
    return f"RUN-{n + 1:05d}"


def _record(run: Run) -> None:
    with _runs_lock:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        run.run_id = run.run_id or _next_run_id()
        with (config.STATE_DIR / "runs.jsonl").open("a") as f:
            f.write(run.model_dump_json() + "\n")


def client():
    global _client
    if _client is None:
        from openai import OpenAI
        key = config.openai_api_key()
        if not key:
            raise LLMError("No OpenAI API key: set OPENAI_API_KEY or ~/.config/aerchain/openai.env")
        _client = OpenAI(api_key=key, timeout=config.LLM_TIMEOUT_S, max_retries=config.LLM_RETRIES)
    return _client


def text_part(text: str) -> dict:
    return {"type": "input_text", "text": text}


def image_part(data: bytes, mime: str = "image/jpeg", detail: str = "auto") -> dict:
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "input_image", "image_url": f"data:{mime};base64,{b64}", "detail": detail}


def schema_version(schema: type[BaseModel]) -> str:
    return hash_obj(schema.model_json_schema())[:8]


def call(job: str, prompt_version: str, schema: type[T], instructions: str, content: list[dict], *,
         doc_id: Optional[str] = None, model: Optional[str] = None, effort: Optional[str] = None,
         max_output_tokens: Optional[int] = None, use_cache: bool = True) -> tuple[T, Run]:
    model = model or config.MODELS.get(job, config.MODEL)
    d_effort, d_max = config.JOBS.get(job, ("medium", 8000))
    effort = effort or d_effort
    max_output_tokens = max_output_tokens or d_max
    sv = schema_version(schema)
    input_hash = hash_obj({"job": job, "prompt_version": prompt_version, "schema": sv, "model": model,
                           "effort": effort, "instructions": instructions, "content": content})
    cache_path = config.STATE_DIR / "cache" / job / f"{input_hash}.json"
    run = Run(run_id="", job=job, doc_id=doc_id, model=model, prompt_version=prompt_version,
              schema_version=sv, input_hash=input_hash, started_at=iso(config.real_now()))

    if use_cache and cache_path.exists():
        data = json.loads(cache_path.read_text())
        parsed = schema.model_validate(data["parsed"])
        run.cached, run.usage, run.duration_s = True, data.get("usage", {}), data.get("duration_s", 0.0)
        _record(run)
        return parsed, run

    t0 = time.time()
    try:
        with _sem:
            t0 = time.time()  # measure model time, not time queued for a slot
            resp = client().responses.parse(
                model=model, instructions=instructions,
                input=[{"role": "user", "content": content}],
                text_format=schema, reasoning={"effort": effort}, max_output_tokens=max_output_tokens)
    except LLMError:
        raise
    except Exception as e:  # network, auth, schema conversion, 4xx/5xx
        run.status, run.error, run.duration_s = "failed", f"{type(e).__name__}: {str(e)[:300]}", time.time() - t0
        _record(run)
        raise LLMError(run.error) from e

    run.duration_s = round(time.time() - t0, 2)
    u = getattr(resp, "usage", None)
    run.usage = {
        "input_tokens": getattr(u, "input_tokens", None),
        "output_tokens": getattr(u, "output_tokens", None),
        "reasoning_tokens": getattr(getattr(u, "output_tokens_details", None), "reasoning_tokens", None),
    }
    parsed = getattr(resp, "output_parsed", None)
    if resp.status != "completed" or parsed is None:
        detail = getattr(resp, "incomplete_details", None)
        refusal = _refusal_text(resp)
        run.status = "failed"
        run.error = f"status={resp.status}" + (f" incomplete={detail}" if detail else "") + (f" refusal={refusal}" if refusal else "")
        _record(run)
        raise LLMError(run.error)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        "parsed": parsed.model_dump(mode="json"), "usage": run.usage, "model": model,
        "duration_s": run.duration_s, "created_at": run.started_at, "response_id": getattr(resp, "id", None),
        "prompt_version": prompt_version, "schema_version": sv, "job": job,
    }, ensure_ascii=False))
    _record(run)
    return parsed, run


def _refusal_text(resp: Any) -> Optional[str]:
    for item in getattr(resp, "output", []) or []:
        for c in getattr(item, "content", []) or []:
            if getattr(c, "type", "") == "refusal":
                return getattr(c, "refusal", "refused")
    return None
