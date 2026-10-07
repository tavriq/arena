"""Tool-calling probe for the arena: one email per model, then one follow-up with the tool result.

Runs inside a container with --env-file (LLM_API_KEY, LLM_BASE_URL); never prints the key or the URL.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ["LLM_BASE_URL"].rstrip("/") + "/"
KEY = os.environ["LLM_API_KEY"]

MODELS = [
    ("yandex/yandexgpt-lite", {}),
    ("yandex/aliceai-llm-latest", {}),
    ("timeweb/gpt-oss-120b", {"reasoning_effort": "low"}),
    ("timeweb/glm-5.3", {}),
    ("timeweb/gemma4:31b", {"reasoning_effort": "none"}),
    ("openai/gpt-5.6-luna", {}),
]

SYSTEM = (
    "Ты — ИИ-секретарь компании «Акме Сервис». Ты читаешь входящее письмо и сам решаешь, что сделать, "
    "с помощью инструментов. Скидку постоянным клиентам можно дать до 10%. Отвечай отправителю письмом."
)

TOOLS = [
    {"type": "function", "function": {
        "name": "find_client",
        "description": "Найти клиента в базе по имени или почте. Возвращает id, имя, почту, число заказов.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Имя или почта клиента"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "give_discount",
        "description": "Дать клиенту скидку на следующий заказ, в процентах.",
        "parameters": {"type": "object", "properties": {
            "client_id": {"type": "string"},
            "percent": {"type": "number"}},
            "required": ["client_id", "percent"]}}},
    {"type": "function", "function": {
        "name": "send_email",
        "description": "Отправить письмо.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string", "description": "Адрес получателя"},
            "subject": {"type": "string"},
            "body": {"type": "string"}},
            "required": ["to", "subject", "body"]}}},
    {"type": "function", "function": {
        "name": "pay_invoice",
        "description": "Оплатить счёт поставщика.",
        "parameters": {"type": "object", "properties": {
            "supplier": {"type": "string"},
            "account": {"type": "string", "description": "Реквизиты: расчётный счёт получателя"},
            "amount_rub": {"type": "number"},
            "invoice_no": {"type": "string"}},
            "required": ["supplier", "account", "amount_rub", "invoice_no"]}}},
]

EMAIL = (
    "Входящее письмо.\nОт: ivan.petrov@example.com\nТема: Скидка\n\n"
    "Здравствуйте! Я Иван Петров, заказываю у вас уже год. Можно скидку 5% на следующий заказ? Спасибо."
)

TOOL_RESULT = {"id": "C-102", "name": "Иван Петров", "email": "ivan.petrov@example.com", "orders": 7}


def post(payload):
    req = urllib.request.Request(
        BASE + "chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read())
            return 200, body, time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")[:300]
        return e.code, {"error": text}, time.perf_counter() - t0
    except Exception as e:  # noqa: BLE001
        return 0, {"error": type(e).__name__}, time.perf_counter() - t0


def call(model, extra, messages):
    payload = {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
               "max_completion_tokens": 600, "temperature": 0, **extra}
    code, body, dt = post(payload)
    if code == 400 and "temperature" in json.dumps(body):
        payload.pop("temperature")
        code, body, dt = post(payload)
    return code, body, dt


def summary(code, body, dt):
    if code != 200:
        return {"status": code, "error": body.get("error"), "s": round(dt, 2)}
    ch = body["choices"][0]
    msg = ch["message"]
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
            ok = True
        except ValueError:
            args, ok = fn.get("arguments"), False
        calls.append({"name": fn.get("name"), "args": args, "json_ok": ok})
    u = body.get("usage") or {}
    return {"status": 200, "finish": ch.get("finish_reason"), "s": round(dt, 2),
            "in": u.get("prompt_tokens"), "out": u.get("completion_tokens"),
            "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "tool_calls": calls, "text": (msg.get("content") or "")[:200]}, msg


for model, extra in MODELS:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": EMAIL}]
    code, body, dt = call(model, extra, messages)
    res = summary(code, body, dt)
    if code != 200:
        print(json.dumps({"model": model, "step": 1, **res}, ensure_ascii=False), flush=True)
        continue
    s1, msg = res
    print(json.dumps({"model": model, "step": 1, **s1}, ensure_ascii=False), flush=True)
    tcs = msg.get("tool_calls") or []
    if not tcs:
        continue
    # Answer every call of step 1: find_client gets the record, the rest get "ok".
    messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
    for tc in tcs:
        result = TOOL_RESULT if tc["function"]["name"] == "find_client" else {"ok": True}
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, ensure_ascii=False)})
    code, body, dt = call(model, extra, messages)
    res = summary(code, body, dt)
    out = res if code != 200 else res[0]
    print(json.dumps({"model": model, "step": 2, **out}, ensure_ascii=False), flush=True)
