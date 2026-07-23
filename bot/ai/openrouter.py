# -*- coding: utf-8 -*-
"""Клиент OpenRouter (OpenAI-совместимый `chat/completions`).

Перенос из telewin практически как есть — там он обкатан на боевом трафике.
Три вещи, которые здесь не косметика:

* **Ретраи только на временных ошибках.** 429 и 5xx — временные, повторяем
  с растущей паузой. Остальные 4xx — ошибка запроса (кривой ключ, кривое тело),
  повтор бесполезен и просто сожжёт время клиента.
* **`tool_choice="required"`** — не украшение, а предохранитель агента: им
  форсируется поиск, когда модель заявила отсутствие товара, не заглянув в прайс.
* **Ключ спрашивается лениво**, в момент вызова. Иначе ETL и замер качества,
  которым модель не нужна, падали бы на старте из-за пустого `OPENROUTER_API_KEY`.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from ..config import OpenRouterConfig, OshibkaKonfiga
from ..logger import log_vyzov_ii

TAYMAUT_S = 60.0
POPYTOK = 3
TEMPERATURA = 0.2   # продавец называет цены из прайса, фантазия тут во вред


class OshibkaOpenRouter(Exception):
    """Сбой обращения к модели. `povtorit` — временный ли он."""

    def __init__(self, soobshchenie: str, povtorit: bool):
        super().__init__(soobshchenie)
        self.povtorit = povtorit


def _klyuch(cfg: OpenRouterConfig) -> str:
    if not cfg.api_key:
        raise OshibkaKonfiga(
            "❌ Не задан OPENROUTER_API_KEY — без него ИИ-слой работать не может.\n"
            "   Ключ берётся на openrouter.ai, кладётся в .env (образец — .env.example).\n"
            "   Баланс: GET https://openrouter.ai/api/v1/credits с этим же ключом."
        )
    return cfg.api_key


async def chat(cfg: OpenRouterConfig, messages: list[dict],
               tools: list[dict] | None = None, tool_choice: str = "auto") -> dict:
    """Один вызов модели → сообщение ассистента `{content, tool_calls}`."""
    telo: dict = {"model": cfg.model, "messages": messages, "temperature": TEMPERATURA}
    if tools:
        telo["tools"] = tools
        telo["tool_choice"] = tool_choice

    zagolovki = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_klyuch(cfg)}",
        # Необязательные заголовки OpenRouter: по ним видно, чей трафик, в статистике ключа.
        "HTTP-Referer": "https://saunamart.ru",
        "X-Title": "SBAvito",
    }

    poslednyaya: Exception | None = None
    async with httpx.AsyncClient(timeout=TAYMAUT_S) as client:
        for popytka in range(1, POPYTOK + 1):
            try:
                t0 = time.perf_counter()
                otvet = await client.post(f"{cfg.base_url}/chat/completions",
                                          json=telo, headers=zagolovki)
                ms = int((time.perf_counter() - t0) * 1000)
                tekst = otvet.text
                if otvet.status_code == 429 or otvet.status_code >= 500:
                    raise OshibkaOpenRouter(
                        f"OpenRouter {otvet.status_code} (временная): {tekst[:200]}",
                        povtorit=True)
                if otvet.status_code >= 400:
                    raise OshibkaOpenRouter(
                        f"OpenRouter {otvet.status_code}: {tekst[:500]}", povtorit=False)
                dannye = otvet.json()
                soobshchenie = (dannye.get("choices") or [{}])[0].get("message")
                if not soobshchenie:
                    raise OshibkaOpenRouter(
                        f"OpenRouter вернул ответ без сообщения: {tekst[:300]}", povtorit=True)
                log_vyzov_ii(cfg.model, ms, dannye.get("usage"),
                             messages=messages, otvet=soobshchenie)
                return {"content": soobshchenie.get("content"),
                        "tool_calls": soobshchenie.get("tool_calls")}
            except OshibkaOpenRouter as e:
                poslednyaya = e
                if not e.povtorit or popytka == POPYTOK:
                    raise
                await asyncio.sleep(0.8 * popytka)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                poslednyaya = e
                if popytka == POPYTOK:
                    raise
                await asyncio.sleep(0.8 * popytka)
    assert poslednyaya is not None
    raise poslednyaya
