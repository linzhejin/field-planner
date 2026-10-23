"""Bounded DeepSeek requests shared by the workbench and evaluation tools."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx


class ModelBudgetExceeded(TimeoutError):
    """The total interaction deadline passed, including late responses."""


@dataclass(frozen=True)
class ModelHTTPResult:
    response: httpx.Response
    elapsed_ms: int
    request_id: str | None


async def post_model_json_async(
    url: str,
    *,
    api_key: str,
    payload: dict[str, Any],
    deadline: float,
    transport: httpx.AsyncBaseTransport | None = None,
    monotonic=time.perf_counter,
) -> ModelHTTPResult:
    started = monotonic()
    remaining = deadline - started
    if remaining <= 0:
        raise ModelBudgetExceeded("模型请求前已超过总时间预算")
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=remaining,
            follow_redirects=True,
        ) as client:
            async with asyncio.timeout(remaining):
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
    except TimeoutError as error:
        raise ModelBudgetExceeded("模型请求超过总时间预算") from error
    if monotonic() >= deadline:
        raise ModelBudgetExceeded("模型响应到达时已超过总时间预算")
    return ModelHTTPResult(
        response=response,
        elapsed_ms=max(0, int((monotonic() - started) * 1000)),
        request_id=response.headers.get("x-request-id"),
    )


def post_model_json(
    url: str,
    *,
    api_key: str,
    payload: dict[str, Any],
    deadline: float,
    transport: httpx.AsyncBaseTransport | None = None,
    monotonic=time.perf_counter,
) -> ModelHTTPResult:
    """Synchronous compatibility wrapper; no detached worker survives cancellation."""
    return asyncio.run(
        post_model_json_async(
            url,
            api_key=api_key,
            payload=payload,
            deadline=deadline,
            transport=transport,
            monotonic=monotonic,
        )
    )
