"""FastAPI app. Run: uvicorn --factory app.main:create_app --no-proxy-headers

``--no-proxy-headers`` matters: the client address (and X-Forwarded-For trust)
is resolved by app.netutil according to TRUSTED_PROXY. Contract: docs/api.md.
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agent import FILTER_MAX_TOKENS, MAX_MODEL_CALLS, MAX_TOKENS, AttemptError, ChatClient, run_attempt
from app.config import Settings
from app.llm import Gateway
from app.netutil import client_ip, rate_limit_key
from app.pricing import worst_attempt_rub
from app.store import Store
from app.world import VISITOR_EMAIL

log = logging.getLogger("demo_arena")
MAX_BODY_BYTES = 16 * 1024
BODY_LIMIT = 1500
SUBJECT_LIMIT = 120

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class AttackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: int = Field(..., ge=1, le=5)
    subject: str = Field(default="", max_length=SUBJECT_LIMIT)
    body: str = Field(..., min_length=1, max_length=BODY_LIMIT)


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, **extra}, status_code=status)


def _load_examples(settings: Settings) -> list[dict]:
    try:
        data = json.loads(settings.examples_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("examples", []) if isinstance(data, dict) else []


def create_app(settings: Settings | None = None, client: ChatClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.var_dir / "arena.sqlite3", settings.daily_budget_rub, settings.rate_limit_per_hour,
                  settings.retention_days)
    if client is None and settings.live:
        client = Gateway(settings.base_url, settings.api_key, settings.llm_timeout_s)
    examples = _load_examples(settings)
    worst = worst_attempt_rub(settings.model, settings.filter_model, MAX_MODEL_CALLS, MAX_TOKENS, FILTER_MAX_TOKENS)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def limits_and_headers(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            response = _error(413, "bad_request", "Слишком длинный запрос.")
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.exception_handler(RequestValidationError)
    async def bad_request(_request: Request, _exc: RequestValidationError):
        return _error(422, "bad_request",
                      f"Нужно письмо от 1 до {BODY_LIMIT} символов, тема до {SUBJECT_LIMIT}, уровень от 1 до 5.")

    @app.get("/api/arena/health")
    def health():
        return {"ok": True}

    @app.get("/api/arena/status")
    def status():
        spent = store.spent_today()
        return {"budget_rub_today": round(spent, 2), "budget_rub_limit": settings.daily_budget_rub,
                "live": client is not None and spent + worst <= settings.daily_budget_rub,
                "counters": store.counters()}

    @app.get("/api/arena/examples")
    def get_examples():
        return {"examples": examples}

    @app.post("/api/arena/attack")
    def attack(req: AttackRequest, request: Request):
        if client is None:
            return _error(503, "budget_exhausted", "Живой режим выключен. Записанные атаки работают.")
        ip = client_ip(request.client.host if request.client else None, request.headers.get("x-forwarded-for"),
                       settings.trusted_proxies)
        decision = store.reserve(rate_limit_key(ip), worst)
        if not decision.allowed:
            status_code = 429 if decision.code == "rate_limited" else 503
            extra = {"retry_after_s": decision.retry_after_s} if decision.retry_after_s else {}
            return _error(status_code, decision.code, decision.message, **extra)
        try:
            result = run_attempt(client, level=req.level, subject=req.subject.strip(), body=req.body,
                                 sender=VISITOR_EMAIL, model=settings.model, filter_model=settings.filter_model)
        except AttemptError as e:
            store.settle(decision.reservation_id, e.cost_rub)
            log.warning("model error: %s", e.detail)
            return _error(502, "model_error", "Модель не ответила. Попробуйте ещё раз через минуту.")
        except Exception:
            store.settle(decision.reservation_id, 0)
            raise
        store.settle(decision.reservation_id, result["cost_rub"])
        store.save_attempt(result, req.subject, req.body)
        counters = store.counters()[req.level - 1]
        result["counters"] = counters
        log.info("attempt level=%s verdict=%s held_by=%s cost=%.4f ms=%s", result["level"], result["verdict"],
                 result["held_by"], result["cost_rub"], result["ms"])
        return result

    return app
