"""Thin, typed wrapper over the TinyFish REST APIs used by RoadLoop.

Endpoints (docs.tinyfish.ai, checked Oct 2026):
  Agent    POST {agent}/v1/automation/run | run-async | run-sse,  GET {agent}/v1/runs/{id},
           POST {agent}/v1/runs/{id}/cancel
  Search   GET  https://api.search.tinyfish.ai?query=...
  Fetch    POST https://api.fetch.tinyfish.ai   {"urls": [...], "format": "markdown"}
  Monitor  POST/GET/PATCH/DELETE {agent}/v1/monitors[/{id}],  POST {agent}/v1/monitors/{id}/runs
Auth: X-API-Key on every call.

Gotcha from the docs: a run can be COMPLETED (infrastructure worked) while the goal
failed; then result == {"status": "failure", "reason": ...}. agent_result() turns
that into AgentGoalFailed so callers can't miss it.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator

import requests

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


class TinyFishError(Exception):
    def __init__(self, message: str, *, http_status: int | None = None, code: str | None = None,
                 category: str | None = None, retry_after: float | None = None, payload: Any = None):
        super().__init__(message)
        self.http_status, self.code, self.category = http_status, code, category
        self.retry_after, self.payload = retry_after, payload

    @property
    def retryable(self) -> bool:
        return self.http_status in (429, 500, 502, 503, 504) or self.category == "SYSTEM_FAILURE"


class AgentGoalFailed(TinyFishError):
    """Run finished but the agent could not achieve the goal."""


class TinyFish:
    def __init__(self, api_key: str, *, agent_base: str = "https://agent.tinyfish.ai",
                 search_base: str = "https://api.search.tinyfish.ai",
                 fetch_base: str = "https://api.fetch.tinyfish.ai",
                 timeout: float = 60.0, max_retries: int = 3, session: requests.Session | None = None):
        self.agent_base = agent_base.rstrip("/")
        self.search_base = search_base.rstrip("/")
        self.fetch_base = fetch_base.rstrip("/")
        self.timeout, self.max_retries = timeout, max_retries
        self.s = session or requests.Session()
        self.s.headers.update({"X-API-Key": api_key, "Accept": "application/json"})

    # ------------------------------------------------------------- plumbing
    def _request(self, method: str, url: str, **kw) -> Any:
        kw.setdefault("timeout", self.timeout)
        attempt = 0
        while True:
            try:
                r = self.s.request(method, url, **kw)
            except requests.RequestException as e:
                err = TinyFishError(f"network error: {e}", http_status=None, category="SYSTEM_FAILURE")
            else:
                if r.status_code < 400:
                    return r.json() if r.content else None
                err = self._error_from(r)
            attempt += 1
            if not err.retryable or attempt > self.max_retries:
                raise err
            time.sleep(err.retry_after if err.retry_after else min(2 ** attempt, 20))

    @staticmethod
    def _error_from(r: requests.Response) -> TinyFishError:
        try:
            body = r.json()
        except ValueError:
            body = {"message": r.text[:300]}
        e = body.get("error", body) if isinstance(body, dict) else {}
        if not isinstance(e, dict):
            e = {"message": str(e)}
        ra = e.get("retry_after") or r.headers.get("Retry-After")
        return TinyFishError(e.get("message") or f"HTTP {r.status_code}", http_status=r.status_code,
                             code=e.get("code"), category=e.get("category"),
                             retry_after=float(ra) if ra else None, payload=body)

    # ---------------------------------------------------------------- agent
    @staticmethod
    def _run_body(url: str, goal: str, output_schema: dict | None, webhook_url: str | None,
                  browser_profile: str | None, **extra) -> dict:
        body: dict[str, Any] = {"url": url, "goal": goal}
        if output_schema:
            body["output_schema"] = output_schema
        if webhook_url:
            body["webhook_url"] = webhook_url  # must be https:// on the real API
        if browser_profile:
            body["browser_profile"] = browser_profile  # "lite" | "stealth"
        body.update({k: v for k, v in extra.items() if v is not None})
        return body

    def run(self, url: str, goal: str, output_schema: dict | None = None, *,
            browser_profile: str | None = None, **extra) -> dict:
        """Blocking run. Fine for Search-style lookups; use run_async for filings."""
        body = self._run_body(url, goal, output_schema, None, browser_profile, **extra)
        return self._request("POST", f"{self.agent_base}/v1/automation/run", json=body, timeout=900)

    def run_async(self, url: str, goal: str, output_schema: dict | None = None, *,
                  webhook_url: str | None = None, browser_profile: str | None = None, **extra) -> str:
        body = self._run_body(url, goal, output_schema, webhook_url, browser_profile, **extra)
        data = self._request("POST", f"{self.agent_base}/v1/automation/run-async", json=body)
        if not data or not data.get("run_id"):
            raise TinyFishError("run-async returned no run_id", payload=data)
        return data["run_id"]

    def stream(self, url: str, goal: str, output_schema: dict | None = None, **extra) -> Iterator[dict]:
        """Yield SSE events: STARTED, STREAMING_URL, PROGRESS, HEARTBEAT, COMPLETE.
        No resume on disconnect: fall back to get_run(run_id)."""
        body = self._run_body(url, goal, output_schema, None, extra.pop("browser_profile", None), **extra)
        with self.s.post(f"{self.agent_base}/v1/automation/run-sse", json=body, stream=True,
                         headers={"Accept": "text/event-stream"}, timeout=(10, 900)) as r:
            if r.status_code >= 400:
                raise self._error_from(r)
            for line in r.iter_lines(decode_unicode=True):
                if line and line.startswith("data:"):
                    try:
                        yield json.loads(line[5:].strip())
                    except ValueError:
                        continue

    def get_run(self, run_id: str) -> dict:
        return self._request("GET", f"{self.agent_base}/v1/runs/{run_id}")

    def cancel_run(self, run_id: str) -> dict:
        return self._request("POST", f"{self.agent_base}/v1/runs/{run_id}/cancel")

    def wait_for_run(self, run_id: str, *, timeout_s: float = 900, poll_s: float = 5) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            run = self.get_run(run_id)
            if run.get("status") in TERMINAL:
                return run
            if time.monotonic() > deadline:
                raise TinyFishError(f"run {run_id} still {run.get('status')} after {timeout_s}s", code="TIMEOUT")
            time.sleep(poll_s)

    @staticmethod
    def agent_result(run: dict) -> dict:
        """Return the structured result or raise. Handles every documented shape."""
        status = run.get("status")
        if status != "COMPLETED":
            e = run.get("error") or {}
            raise TinyFishError(e.get("message") or f"run {status}", code=e.get("code"),
                                category=e.get("category"), payload=run)
        result = run.get("result")
        if isinstance(result, dict) and result.get("status") == "failure" and "reason" in result:
            raise AgentGoalFailed(result.get("reason", "agent could not complete goal"),
                                  category="AGENT_FAILURE", payload=run)
        if isinstance(result, dict) and set(result) == {"result"}:
            result = result["result"]
        if not isinstance(result, dict):
            return {"value": result}
        return result

    # --------------------------------------------------------- search/fetch
    def search(self, query: str, *, location: str | None = None, language: str | None = None,
               domain_type: str | None = None, recency_minutes: int | None = None,
               purpose: str | None = None) -> list[dict]:
        params = {k: v for k, v in dict(query=query, location=location, language=language,
                                        domain_type=domain_type, recency_minutes=recency_minutes,
                                        purpose=purpose).items() if v is not None}
        data = self._request("GET", self.search_base, params=params)
        return (data or {}).get("results", [])

    def fetch(self, urls: list[str], *, format: str = "markdown", ttl: int | None = 0,
              include_selectors: list[str] | None = None, purpose: str | None = None) -> dict:
        if not 1 <= len(urls) <= 10:
            raise ValueError("Fetch takes 1-10 URLs per request")
        body: dict[str, Any] = {"urls": urls, "format": format}
        if ttl is not None:
            body["ttl"] = ttl
        if include_selectors:
            body["include_selectors"] = include_selectors
        if purpose:
            body["purpose"] = purpose
        return self._request("POST", self.fetch_base, json=body)

    # -------------------------------------------------------------- monitors
    def create_page_monitor(self, name: str, url: str, schedule_cron: str, *, purpose: str | None = None,
                            webhook_url: str | None = None, format: str = "markdown") -> dict:
        body = {"type": "fetch", "name": name[:100], "config": {"url": url, "format": format},
                "schedule_cron": schedule_cron}
        if purpose:
            body["purpose"] = purpose
        if webhook_url:
            body["webhook_url"] = webhook_url
        return self._request("POST", f"{self.agent_base}/v1/monitors", json=body)

    def create_topic_monitor(self, name: str, query: str, schedule_cron: str, *, purpose: str | None = None,
                             webhook_url: str | None = None, recency_minutes: int | None = None,
                             result_limit: int = 10) -> dict:
        cfg: dict[str, Any] = {"query": query[:2000], "result_limit": result_limit}
        if recency_minutes:
            cfg["recency_minutes"] = recency_minutes
        body = {"type": "search", "name": name[:100], "config": cfg, "schedule_cron": schedule_cron}
        if purpose:
            body["purpose"] = purpose
        if webhook_url:
            body["webhook_url"] = webhook_url
        return self._request("POST", f"{self.agent_base}/v1/monitors", json=body)

    def list_monitors(self) -> list[dict]:
        return (self._request("GET", f"{self.agent_base}/v1/monitors") or {}).get("monitors", [])

    def set_monitor_status(self, monitor_id: str, status: str) -> dict:
        # Docs: don't combine `status` with other fields in the same PATCH.
        if status not in ("active", "paused"):
            raise ValueError(status)
        return self._request("PATCH", f"{self.agent_base}/v1/monitors/{monitor_id}", json={"status": status})

    def delete_monitor(self, monitor_id: str) -> dict:
        return self._request("DELETE", f"{self.agent_base}/v1/monitors/{monitor_id}")

    def run_monitor_now(self, monitor_id: str) -> dict:
        # Note: run-now results do NOT fire webhooks; the caller must process the returned run.
        return self._request("POST", f"{self.agent_base}/v1/monitors/{monitor_id}/runs")


def from_settings(settings) -> TinyFish:
    return TinyFish(settings.tinyfish_api_key, agent_base=settings.agent_base,
                    search_base=settings.search_base, fetch_base=settings.fetch_base)
