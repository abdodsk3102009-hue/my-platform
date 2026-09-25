#!/usr/bin/env python3
"""Musaed الأعمال — Arabic first prototype.

A dependency-free local prototype for:
- multiple OpenAI-compatible providers with automatic fallback;
- BYOK/custom endpoint configuration held in RAM only;
- Meta/WhatsApp guided onboarding and webhook receiver;
- a small chat playground.

This is deliberately a prototype: API keys are never written to disk, but a
production deployment still needs a secret manager, authentication, CSRF,
rate limiting, encrypted tenant storage and a queue.
"""
from __future__ import annotations

import html.parser
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
HOST = os.environ.get("APP_HOST", "0.0.0.0")
# Render supplies PORT (normally 10000). The local fallback keeps the app easy to run.
PORT = int(os.environ.get("PORT", "10000"))
GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v23.0")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * min(12, len(value) - 8) + value[-4:]


def clean_url(value: str) -> str:
    return value.strip().rstrip("/")


# These are API routes, not subscription/OAuth routes. Kiro and Antigravity
# are shown as experimental custom connectors so the product does not pretend
# an OAuth/session token is a supported API key.
CATALOG: list[dict[str, Any]] = [
    {
        "id": "gemini",
        "name": "Google Gemini API",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "kind": "api",
        "risk": "api",
        "note": "API رسمي متوافق مع OpenAI؛ استخدم مفتاح Gemini API وليس OAuth.",
        "color": "#7c83ff",
    },
    {
        "id": "qwen",
        "name": "Qwen / Alibaba Bailian",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "kind": "api",
        "risk": "check_terms",
        "note": "مسار API رسمي؛ راجع شروط الخطة قبل الاستخدام التجاري.",
        "color": "#f59e0b",
    },
    {
        "id": "nim",
        "name": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "nvidia/llama-3.1-nemotron-ultra-253b-v1",
        "kind": "api",
        "risk": "prototype_only",
        "note": "مفيد للتجربة؛ لا نعد بأن طبقة NIM المجانية مناسبة للإنتاج.",
        "color": "#76b900",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter (Free)",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "qwen/qwen3.8-27b:free",
        "kind": "api",
        "risk": "check_terms",
        "note": "مجمّع API؛ اختر نموذج :free متاحًا في حسابك.",
        "color": "#22c55e",
    },
    {
        "id": "kiro",
        "name": "Kiro — موصل تجريبي فقط",
        "base_url": "",
        "model": "",
        "kind": "restricted",
        "risk": "high",
        "note": "لا نستخدم OAuth أو توكن جلسة Kiro. يقبل فقط API رسميًا موثقًا إن وُجد.",
        "color": "#ef4444",
    },
    {
        "id": "antigravity",
        "name": "Antigravity — موصل تجريبي فقط",
        "base_url": "",
        "model": "",
        "kind": "restricted",
        "risk": "high",
        "note": "لا نستخدم OAuth أو توكن جلسة. عند 401/403 يتخطاه النظام تلقائيًا.",
        "color": "#f97316",
    },
    {
        "id": "kilo",
        "name": "Kilo — API مخصص",
        "base_url": "",
        "model": "",
        "kind": "custom",
        "risk": "check_terms",
        "note": "أدخل endpoint موثقًا ومتوافقًا مع /chat/completions.",
        "color": "#06b6d4",
    },
    {
        "id": "custom",
        "name": "أي مزود OpenAI-compatible",
        "base_url": "",
        "model": "",
        "kind": "custom",
        "risk": "check_terms",
        "note": "ألصق URL الـ API، اسم النموذج، والمفتاح. لا تستخدم OAuth/session tokens.",
        "color": "#a78bfa",
    },
]


def catalog_item(provider_id: str) -> dict[str, Any]:
    for item in CATALOG:
        if item["id"] == provider_id:
            return dict(item)
    return dict(CATALOG[-1])


class ProviderRuntime:
    """In-memory provider registry and fallback state."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.providers: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self._seed()

    def _seed(self) -> None:
        # A local demo route lets the user see the fallback UX without buying
        # tokens or pasting a secret. It is visibly labelled in the UI.
        self.providers["demo"] = {
            "id": "demo", "name": "عرض تجريبي محلي", "base_url": "",
            "model": "demo-assistant", "kind": "demo", "risk": "safe",
            "note": "لا يخرج أي طلب إلى الإنترنت.", "api_key": "",
            "enabled": True, "priority": 99, "cooldown_until": 0,
            "failures": 0, "requests": 0, "tokens": 0,
            "last_error": "", "last_ok": utc_now(), "color": "#94a3b8",
        }
        for item in CATALOG:
            self.providers[item["id"]] = {
                **item, "api_key": "", "enabled": False,
                "priority": 10 + len(self.providers), "cooldown_until": 0,
                "failures": 0, "requests": 0, "tokens": 0,
                "last_error": "", "last_ok": "",
            }

    def public(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = []
            for p in sorted(self.providers.values(), key=lambda x: x["priority"]):
                state = self._state(p)
                rows.append({
                    k: p[k] for k in (
                        "id", "name", "base_url", "model", "kind", "risk",
                        "note", "enabled", "priority", "requests", "tokens",
                        "last_error", "last_ok", "color",
                    )
                } | {
                    "state": state,
                    "has_key": bool(p.get("api_key")),
                    "key_hint": mask(p.get("api_key", "")),
                })
            return rows

    def _state(self, p: dict[str, Any]) -> str:
        if p["kind"] == "demo":
            return "demo"
        if not p.get("api_key") or not p.get("base_url") or not p.get("model"):
            return "missing"
        if not p.get("enabled"):
            return "disabled"
        if p.get("cooldown_until", 0) > time.time():
            return "cooldown"
        if p.get("last_error", "").startswith("blocked"):
            return "blocked"
        return "ready"

    def configure(self, data: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
        provider_id = str(data.get("id") or "custom").strip()[:60]
        base = catalog_item(provider_id)
        p = self.providers.get(provider_id)
        if p is None:
            p = {**base, "id": provider_id}
            self.providers[provider_id] = p

        name = str(data.get("name") or base.get("name") or "مزود مخصص").strip()[:120]
        url = clean_url(str(data.get("base_url") or base.get("base_url") or ""))
        model = str(data.get("model") or base.get("model") or "").strip()[:180]
        key = str(data.get("api_key") or "").strip()
        if not url and base.get("kind") != "restricted":
            return False, "أدخل URL للـ API.", None
        if url and not (url.startswith("https://") or url.startswith("http://")):
            return False, "URL يجب أن يبدأ بـ https:// أو http://.", None
        if not model and base.get("kind") != "restricted":
            return False, "أدخل اسم النموذج.", None
        # Restricted connectors remain explicit: only a documented endpoint
        # is accepted; no OAuth/session import is ever performed by this app.
        if base.get("kind") == "restricted" and (not url or not model):
            return False, "هذا موصل تجريبي: ألصق API endpoint موثقًا واسم نموذج؛ لا تلصق OAuth أو توكن جلسة.", None
        with self.lock:
            p.update({
                "name": name, "base_url": url, "model": model,
                "api_key": key, "enabled": bool(data.get("enabled", True)),
                "priority": max(1, min(999, int(data.get("priority", p.get("priority", 50))))),
                "last_error": "" if key else p.get("last_error", ""),
            })
            self._log("config", p, "تم تحديث الإعدادات — المفتاح في الذاكرة فقط")
            return True, "تمت الإضافة. المفتاح لا يُكتب إلى القرص في هذه النسخة.", self.public_item(p)

    def public_item(self, p: dict[str, Any]) -> dict[str, Any]:
        return next(x for x in self.public() if x["id"] == p["id"])

    def toggle(self, provider_id: str, enabled: bool) -> tuple[bool, str]:
        with self.lock:
            p = self.providers.get(provider_id)
            if not p:
                return False, "المزود غير موجود."
            if provider_id == "demo" and not enabled and not any(
                x.get("enabled") and x.get("api_key") for x in self.providers.values()
            ):
                return False, "أبقِ العرض التجريبي أو أضف مزودًا بمفتاح أولًا."
            p["enabled"] = bool(enabled)
            self._log("toggle", p, "تم التفعيل" if enabled else "تم التعطيل")
            return True, "تم التحديث."

    def _log(self, event: str, p: dict[str, Any] | None, detail: str, ok: bool | None = None) -> None:
        row = {"time": utc_now(), "event": event, "provider": p.get("name") if p else "النظام", "detail": detail}
        if ok is not None:
            row["ok"] = ok
        self.events.insert(0, row)
        self.events[:] = self.events[:80]

    def logs(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.events)

    def route(self, messages: list[dict[str, str]], max_tokens: int = 500) -> dict[str, Any]:
        tried: list[str] = []
        with self.lock:
            providers = sorted(self.providers.values(), key=lambda x: x["priority"])
        external_configured = any(
            p.get("enabled") and p.get("kind") != "demo" and p.get("api_key") and p.get("base_url") and p.get("model")
            for p in providers
        )
        for p in providers:
            if not p.get("enabled"):
                continue
            # Demo is only a first-run preview. Once the customer configured
            # a real provider, never silently return a fake local answer.
            if p["kind"] == "demo" and external_configured:
                continue
            if p["kind"] != "demo" and (not p.get("api_key") or not p.get("base_url") or not p.get("model")):
                tried.append(f"{p['name']}: بلا مفتاح/إعداد ناقص")
                continue
            if p.get("cooldown_until", 0) > time.time():
                remain = int(p["cooldown_until"] - time.time())
                tried.append(f"{p['name']}: انتظار {remain}ث بعد الحصة")
                continue
            if p.get("last_error", "").startswith("blocked"):
                tried.append(f"{p['name']}: مرفوض أو محظور — تم التجاوز")
                continue
            try:
                if p["kind"] == "demo":
                    text = self.demo_reply(messages)
                    used = 0
                else:
                    text, used = self.call_openai_compatible(p, messages, max_tokens)
                with self.lock:
                    p["requests"] += 1
                    p["tokens"] += used
                    p["failures"] = 0
                    p["last_ok"] = utc_now()
                    p["last_error"] = ""
                    self._log("chat", p, f"نجح الرد عبر {p['model']} ({used} توكن)", True)
                return {"ok": True, "text": text, "provider": p["name"], "model": p["model"], "tokens": used, "tried": tried}
            except ProviderFailure as exc:
                detail = str(exc)
                with self.lock:
                    p["failures"] += 1
                    p["last_error"] = exc.kind + (f": {detail}" if detail else "")
                    if exc.kind in ("blocked", "quota"):
                        p["cooldown_until"] = time.time() + (300 if exc.kind == "blocked" else 60)
                    self._log("fallback", p, f"{exc.kind}: {detail} — التحويل للمزود التالي", False)
                tried.append(f"{p['name']}: {exc.kind} — التحويل للتالي")
        self._log("alert", None, "فشل كل المزودين؛ أضف مفتاحًا أو انتظر تجدد الحصة", False)
        return {"ok": False, "error": "المساعد مشغول مؤقتًا. سيتم الرد عند توفر مزود.", "tried": tried}

    @staticmethod
    def demo_reply(messages: list[dict[str, str]]) -> str:
        user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        lowered = user.lower()
        if any(x in lowered for x in ("سعر", "كم", "price")):
            return "أهلًا بك 👋 يسعدنا مساعدتك. أرسل اسم المنتج أو الخدمة وسنرسل لك السعر والتفاصيل. هذا رد العرض التجريبي المحلي."
        if any(x in lowered for x in ("وقت", "دوام", "مفتوح")):
            return "نحن متاحون من السبت إلى الخميس، من 9:00 صباحًا إلى 9:00 مساءً. هذا رد العرض التجريبي المحلي."
        return "مرحبًا! وصلت رسالتك. هذا رد العرض التجريبي المحلي، ويمكن تحويله تلقائيًا إلى Gemini أو Qwen أو NVIDIA أو أي API تضيفه."

    @staticmethod
    def call_openai_compatible(p: dict[str, Any], messages: list[dict[str, str]], max_tokens: int) -> tuple[str, int]:
        body = json.dumps({
            "model": p["model"], "messages": messages,
            "temperature": 0.3, "max_tokens": max(32, min(1200, max_tokens)),
        }).encode("utf-8")
        req = urllib.request.Request(p["base_url"] + "/chat/completions", data=body, method="POST")
        req.add_header("Authorization", "Bearer " + p["api_key"])
        req.add_header("Content-Type", "application/json")
        if "openrouter.ai" in p["base_url"]:
            req.add_header("HTTP-Referer", os.environ.get("APP_URL", "http://localhost:8090"))
            req.add_header("X-Title", "Musaed الأعمال prototype")
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                raw = response.read().decode("utf-8", "replace")
                data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:260]
            if exc.code in (401, 403):
                raise ProviderFailure("blocked", f"HTTP {exc.code}") from None
            if exc.code == 429:
                raise ProviderFailure("quota", "HTTP 429") from None
            if exc.code in (408, 425, 500, 502, 503, 504):
                raise ProviderFailure("temporary", f"HTTP {exc.code}") from None
            raise ProviderFailure("config", f"HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderFailure("temporary", str(exc)[:160]) from None
        try:
            choice = data["choices"][0]["message"]["content"]
            usage = int(data.get("usage", {}).get("total_tokens", 0) or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderFailure("config", "استجابة غير متوافقة مع OpenAI API") from None
        return str(choice), usage

    def test_provider(self, provider_id: str) -> dict[str, Any]:
        with self.lock:
            p = self.providers.get(provider_id)
            if not p:
                return {"ok": False, "error": "المزود غير موجود."}
            if p["kind"] == "demo":
                return {"ok": True, "text": "العرض التجريبي يعمل محليًا — لا يوجد طلب خارجي.", "provider": p["name"]}
            if p["kind"] == "restricted":
                if not p.get("base_url") or not p.get("model"):
                    return {"ok": False, "error": "هذا الموصل لا يستورد OAuth. أدخل API endpoint موثقًا واسم نموذج أولًا."}
            if not p.get("api_key"):
                return {"ok": False, "error": "أضف المفتاح أولًا."}
            messages = [{"role": "user", "content": "قل: تم"}]
        try:
            text, tokens = self.call_openai_compatible(p, messages, 16)
            with self.lock:
                p["last_error"] = ""
                p["last_ok"] = utc_now()
                self._log("test", p, f"نجح الاختبار ({tokens} توكن)", True)
            return {"ok": True, "text": text, "provider": p["name"], "model": p["model"], "tokens": tokens}
        except ProviderFailure as exc:
            with self.lock:
                p["last_error"] = exc.kind + ": " + str(exc)
                if exc.kind in ("blocked", "quota"):
                    p["cooldown_until"] = time.time() + (300 if exc.kind == "blocked" else 60)
                self._log("test", p, f"{exc.kind}: {exc}", False)
            return {"ok": False, "error": f"{exc.kind}: {exc}. سيتم تجاوز المزود تلقائيًا في الطلب التالي."}


class ProviderFailure(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind
        super().__init__(detail)


RUNTIME = ProviderRuntime()
META_LOCK = threading.RLock()
META = {
    "phone_number_id": "", "waba_id": "", "access_token": "",
    "verify_token": os.environ.get("META_VERIFY_TOKEN", "").strip() or secrets.token_urlsafe(18), "connected": False,
    "display_phone_number": "", "business_name": "", "last_error": "",
}

CHANNEL_LOCK = threading.RLock()
CHANNELS: dict[str, dict[str, Any]] = {
    "telegram": {"connected": False, "bot_username": "", "webhook_url": "", "secret": "", "token": "", "transport": ""},
    "messenger": {"connected": False, "page_name": "", "page_id": ""},
    "website": {"connected": False, "site_id": "", "site_name": "", "embed_code": ""},
}
SITES: dict[str, dict[str, Any]] = {}
ASSISTANT_SYSTEM_PROMPT = "أنت مساعد خدمة عملاء لنشاط تجاري عربي. أجب باختصار وباحترام. إذا لم تعرف الإجابة لا تخمّن، واطلب من العميل ترك رقمه لمتابعة الموظف."
TELEGRAM_POLL_STOP = threading.Event()
TELEGRAM_POLL_THREAD: threading.Thread | None = None


def public_channels() -> dict[str, Any]:
    with CHANNEL_LOCK:
        return {
            "telegram": {k: v for k, v in CHANNELS["telegram"].items() if k not in ("token", "secret")},
            "messenger": dict(CHANNELS["messenger"]),
            "website": dict(CHANNELS["website"]),
        }


def public_meta(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    proto = handler.headers.get("X-Forwarded-Proto", "https")
    host = handler.headers.get("Host", f"localhost:{PORT}")
    callback = f"{proto}://{host}/api/meta/webhook"
    with META_LOCK:
        return {
            "phone_number_id": META["phone_number_id"], "waba_id": META["waba_id"],
            "callback_url": callback,
            "verify_token_configured": bool(META["verify_token"]),
            "connected": META["connected"], "display_phone_number": META["display_phone_number"],
            "business_name": META["business_name"], "last_error": META["last_error"],
            "app_id": os.environ.get("META_APP_ID", ""),
            "config_id": os.environ.get("META_CONFIG_ID", ""),
        }


def subscribe_meta_waba(waba_id: str, token: str) -> tuple[bool, str]:
    """Subscribe this app to the WABA's webhook events.

    Dashboard configuration is still required once in Meta, but calling
    subscribed_apps here prevents the common case where the phone is valid
    yet Meta never delivers incoming messages to the app.
    """
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{urllib.parse.quote(waba_id, safe='')}/subscribed_apps"
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8", "replace")
            result = json.loads(raw or "{}")
            if result.get("success") is False:
                return False, raw[:280]
            return True, raw[:280]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:280]
        return False, f"Meta HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, str(exc)[:280]


def meta_validate(data: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
    phone_id = str(data.get("phone_number_id", "")).strip()
    waba_id = str(data.get("waba_id", "")).strip()
    token = str(data.get("access_token", "")).strip()
    verify = str(data.get("verify_token", "")).strip() or META["verify_token"]
    if not phone_id or not token or not waba_id:
        return False, "أدخل Phone Number ID وWABA ID وAccess Token؛ WABA ID مطلوب لتفعيل Webhook.", None
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{urllib.parse.quote(phone_id, safe='')}?fields=display_phone_number,verified_name"
    req = urllib.request.Request(url, method="GET", headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:280]
        return False, f"Meta رفض التحقق (HTTP {exc.code}). تحقق من الصلاحيات والتوكن. {detail}", None
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"تعذر الوصول إلى Meta الآن: {exc}", None
    subscribed, subscription_detail = subscribe_meta_waba(waba_id, token)
    if not subscribed:
        return False, f"تم التحقق من Phone Number ID، لكن Meta لم تفعل اشتراك Webhook لهذا WABA: {subscription_detail}", None
    with META_LOCK:
        META.update({
            "phone_number_id": phone_id, "waba_id": waba_id,
            "access_token": token, "verify_token": verify,
            "connected": True, "display_phone_number": result.get("display_phone_number", ""),
            "business_name": result.get("verified_name", ""), "last_error": "",
        })
    return True, "تم التحقق من حساب Meta وتفعيل اشتراك Webhook لهذا WABA.", public_meta_dummy()


def public_meta_dummy() -> dict[str, Any]:
    with META_LOCK:
        return {"connected": META["connected"], "display_phone_number": META["display_phone_number"], "business_name": META["business_name"]}


def send_whatsapp_message(to: str, text: str) -> tuple[bool, str]:
    with META_LOCK:
        phone_id, token = META["phone_number_id"], META["access_token"]
    if not phone_id or not token:
        return False, "Meta غير مربوط"
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_id}/messages"
    body = json.dumps({"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return True, response.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as exc:
        return False, f"Meta HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, str(exc)


class SimpleTextExtractor(html.parser.HTMLParser):
    """Small, dependency-free extractor for a public homepage preview."""
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.title: str = ""
        self.description: str = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            data = {str(k).lower(): str(v or "") for k, v in attrs}
            href = data.get("href", "").strip()
            if href:
                self.links.append(href)
        if tag == "meta":
            data = {str(k).lower(): str(v or "") for k, v in attrs}
            if data.get("name", "").lower() == "description":
                self.description = data.get("content", "")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text or self._skip:
            return
        if self._in_title:
            self.title += " " + text
        self.parts.append(text)


def scrape_public_homepage(url: str) -> tuple[bool, str, dict[str, Any]]:
    """Fetch only a public homepage; no private URLs, no crawling yet."""
    url = url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False, "أدخل رابطًا عامًا يبدأ بـ https://.", {}
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return False, "لا يمكن فحص localhost من خدمة عامة.", {}
    try:
        addresses = {socket.gethostbyname(host)}
        if any(ipaddress.ip_address(a).is_private or ipaddress.ip_address(a).is_loopback for a in addresses):
            return False, "الرابط يشير إلى شبكة داخلية؛ أدخل موقعًا عامًا.", {}
    except (socket.gaierror, ValueError):
        return False, "تعذر العثور على نطاق الموقع.", {}
    req = urllib.request.Request(url, headers={"User-Agent": "EvoflowSitePreview/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(1_500_000)
    except urllib.error.HTTPError as exc:
        return False, f"الموقع رفض القراءة (HTTP {exc.code}).", {}
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"تعذر الوصول إلى الموقع: {exc}", {}
    if "html" not in content_type.lower() and not raw.lstrip().lower().startswith(b"<!doctype html"):
        return False, "الرابط لا يعيد صفحة HTML قابلة للفحص.", {}
    parser = SimpleTextExtractor()
    try:
        parser.feed(raw.decode("utf-8", "replace"))
    except Exception:
        return False, "تعذر تحليل صفحة الموقع.", {}
    text = " ".join(parser.parts)
    if len(text) < 80:
        return True, "تمت قراءة الصفحة، لكنها تبدو تطبيق JavaScript أو تحتوي نصًا قليلًا. نحتاج لاحقًا متصفحًا آليًا لفحصها بالكامل.", {"title": parser.title.strip(), "description": parser.description.strip(), "text": text[:5000], "dynamic": True}
    return True, "تم فحص الصفحة الرئيسية.", {"title": parser.title.strip(), "description": parser.description.strip(), "text": text[:7000], "dynamic": False}


def crawl_public_site(start_url: str, max_pages: int = 25) -> tuple[bool, str, dict[str, Any]]:
    """Crawl same-domain public HTML pages for a first RAG corpus.

    This intentionally does not execute private code, log in, bypass paywalls,
    or run JavaScript. JS-rendered sites are marked dynamic for a future
    Playwright worker.
    """
    start_url = start_url.strip()
    parsed = urllib.parse.urlparse(start_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False, "أدخل رابطًا عامًا يبدأ بـ https://.", {}
    root_host = parsed.hostname.lower().removeprefix("www.")
    if root_host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return False, "لا يمكن فحص localhost من خدمة عامة.", {}
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(root_host))
        if ip.is_private or ip.is_loopback:
            return False, "الرابط يشير إلى شبكة داخلية؛ أدخل موقعًا عامًا.", {}
    except (socket.gaierror, ValueError):
        return False, "تعذر العثور على نطاق الموقع.", {}

    queue = [start_url]
    seen: set[str] = set()
    documents: list[dict[str, Any]] = []
    dynamic_pages = 0
    while queue and len(documents) < max_pages:
        current = queue.pop(0)
        p = urllib.parse.urlparse(current)
        normalized = urllib.parse.urlunparse((p.scheme, p.netloc.lower(), p.path or "/", "", "", ""))
        if normalized in seen:
            continue
        seen.add(normalized)
        host = (p.hostname or "").lower().removeprefix("www.")
        if host != root_host:
            continue
        if any(normalized.lower().split("?", 1)[0].endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".mp4", ".mp3", ".css", ".js")):
            continue
        try:
            req = urllib.request.Request(normalized, headers={"User-Agent": "EvoflowCrawler/0.2"})
            with urllib.request.urlopen(req, timeout=12) as response:
                content_type = response.headers.get("Content-Type", "")
                raw = response.read(1_500_000)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        if "html" not in content_type.lower() and not raw.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            continue
        parser = SimpleTextExtractor()
        try:
            parser.feed(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        text = " ".join(parser.parts)
        if len(text) < 80:
            dynamic_pages += 1
        documents.append({
            "url": normalized,
            "title": parser.title.strip(),
            "description": parser.description.strip(),
            "text": text[:6000],
        })
        for href in parser.links:
            joined = urllib.parse.urljoin(normalized, href)
            jp = urllib.parse.urlparse(joined)
            if jp.scheme in ("http", "https") and (jp.hostname or "").lower().removeprefix("www.") == root_host:
                queue.append(joined)

    if not documents:
        return False, "لم نجد صفحات HTML عامة قابلة للقراءة.", {}
    message = f"تم تحليل {len(documents)} صفحة عامة من الموقع."
    if dynamic_pages:
        message += f" {dynamic_pages} صفحة تبدو مبنية بـ JavaScript وتحتاج لاحقًا متصفحًا آليًا لقراءة محتواها الكامل."
    return True, message, {
        "title": documents[0].get("title", ""),
        "description": documents[0].get("description", ""),
        "pages": len(documents),
        "dynamic_pages": dynamic_pages,
        "documents": documents,
    }


def select_site_context(site: dict[str, Any], question: str) -> str:
    docs = site.get("documents") or []
    if not docs:
        return site.get("knowledge", "")[:9000]
    terms = {x for x in re.findall(r"[\w\u0600-\u06ff]{3,}", question.lower())}
    ranked = []
    for doc in docs:
        text = (doc.get("title", "") + " " + doc.get("description", "") + " " + doc.get("text", "")).lower()
        score = sum(1 for term in terms if term in text)
        ranked.append((score, doc))
    ranked.sort(key=lambda x: x[0], reverse=True)
    chosen = [doc for _, doc in ranked[:3]]
    return "\n\n".join(
        f"صفحة: {doc.get('url')}\nالعنوان: {doc.get('title')}\n{doc.get('description', '')}\n{doc.get('text', '')[:2800]}"
        for doc in chosen
    )[:9000]


def external_origin(handler: BaseHTTPRequestHandler) -> str:
    host = handler.headers.get("X-Forwarded-Host", handler.headers.get("Host", f"localhost:{PORT}"))
    forwarded = handler.headers.get("X-Forwarded-Proto", "")
    proto = forwarded or ("http" if host.startswith(("localhost", "127.0.0.1")) else "https")
    return f"{proto}://{host}"


def telegram_api(token: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Small Bot API client. Token is never logged or returned."""
    body = urllib.parse.urlencode(params or {}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:250]
        return {"ok": False, "description": f"Telegram HTTP {exc.code}: {detail}"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "description": f"تعذر الوصول إلى Telegram: {exc}"}


def telegram_connect(token: str, origin: str) -> tuple[bool, str, dict[str, Any]]:
    token = token.strip()
    if not token or ":" not in token:
        return False, "توكن Telegram غير مكتمل. انسخه من BotFather.", {}
    me = telegram_api(token, "getMe")
    if not me.get("ok"):
        return False, me.get("description", "Telegram رفض التوكن."), {}
    bot = me.get("result", {})
    slug = secrets.token_urlsafe(16).replace("-", "").replace("_", "")
    secret = secrets.token_urlsafe(24)
    webhook = f"{origin}/api/telegram/webhook/{slug}"
    # Arena/E2B preview URLs require a private traffic header that Telegram
    # cannot send. Use Bot API long-polling there so the user can test now;
    # production HTTPS deployments use the cheaper webhook path.
    preview_or_forced_polling = "e2b.app" in origin or os.environ.get("TELEGRAM_TRANSPORT", "").lower() == "polling"
    if preview_or_forced_polling:
        telegram_api(token, "deleteWebhook", {"drop_pending_updates": "false"})
        with CHANNEL_LOCK:
            CHANNELS["telegram"].update({"connected": True, "bot_username": bot.get("username", ""), "webhook_url": webhook, "secret": secret, "token": token, "transport": "polling"})
        start_telegram_polling()
        RUNTIME._log("telegram", None, f"تم ربط @{bot.get('username', '')} عبر polling للمعاينة المحمية")
        return True, f"تم ربط البوت @{bot.get('username', '')}. وضع المعاينة يستخدم polling لأن Telegram لا يستطيع إرسال Header حماية المعاينة.", {"bot_username": bot.get("username", ""), "webhook_url": webhook, "webhook_ready": False, "transport": "polling"}
    set_result = telegram_api(token, "setWebhook", {"url": webhook, "secret_token": secret, "allowed_updates": json.dumps(["message", "callback_query"])})
    if not set_result.get("ok"):
        # Keep the verified token and fall back to polling for a prototype.
        # A production deployment should use a public HTTPS webhook instead.
        with CHANNEL_LOCK:
            CHANNELS["telegram"].update({"connected": True, "bot_username": bot.get("username", ""), "webhook_url": webhook, "secret": secret, "token": token, "transport": "polling"})
        start_telegram_polling()
        RUNTIME._log("telegram", None, f"تم التحقق من @{bot.get('username', '')}؛ Webhook فشل فتم التحويل إلى polling")
        return True, "تم حفظ التوكن. تعذر Webhook، لذلك فعّلت وضع polling للتجربة.", {"bot_username": bot.get("username", ""), "webhook_url": webhook, "webhook_ready": False, "transport": "polling"}
    with CHANNEL_LOCK:
        CHANNELS["telegram"].update({"connected": True, "bot_username": bot.get("username", ""), "webhook_url": webhook, "secret": secret, "token": token, "transport": "webhook"})
    RUNTIME._log("telegram", None, f"تم ربط @{bot.get('username', '')} وتهيئة Webhook")
    return True, f"تم ربط البوت @{bot.get('username', '')} وتهيئة Webhook.", {"bot_username": bot.get("username", ""), "webhook_url": webhook, "webhook_ready": True, "transport": "webhook"}


def send_telegram_message(chat_id: Any, text: str) -> tuple[bool, str]:
    with CHANNEL_LOCK:
        token = CHANNELS["telegram"].get("token", "")
    if not token:
        return False, "Telegram غير مربوط"
    result = telegram_api(token, "sendMessage", {"chat_id": str(chat_id), "text": text[:4096]})
    return bool(result.get("ok")), str(result.get("description", "ok"))


def process_telegram_update(update: dict[str, Any]) -> None:
    message = update.get("message", {}) or {}
    chat = message.get("chat", {}) or {}
    text = (message.get("text") or "").strip()
    if not chat.get("id") or not text:
        return
    result = RUNTIME.route([
        {"role": "system", "content": ASSISTANT_SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ])
    reply = result.get("text") if result.get("ok") else "وصلت رسالتك، وسنرد عليك قريبًا."
    ok, detail = send_telegram_message(chat["id"], reply)
    RUNTIME._log("telegram-message", None, f"رد={'نجح' if ok else 'فشل'}؛ المزود={result.get('provider', 'none')}; {detail}", ok)


def stop_telegram_polling() -> None:
    global TELEGRAM_POLL_THREAD
    TELEGRAM_POLL_STOP.set()
    if TELEGRAM_POLL_THREAD and TELEGRAM_POLL_THREAD.is_alive():
        TELEGRAM_POLL_THREAD.join(timeout=1.5)
    TELEGRAM_POLL_THREAD = None
    TELEGRAM_POLL_STOP.clear()


def start_telegram_polling() -> None:
    global TELEGRAM_POLL_THREAD
    stop_telegram_polling()

    def worker() -> None:
        offset = 0
        while not TELEGRAM_POLL_STOP.is_set():
            with CHANNEL_LOCK:
                token = CHANNELS["telegram"].get("token", "")
                connected = CHANNELS["telegram"].get("connected", False)
            if not token or not connected:
                break
            result = telegram_api(token, "getUpdates", {"timeout": "5", "offset": str(offset), "allowed_updates": json.dumps(["message"])})
            if result.get("ok"):
                for update in result.get("result", []) or []:
                    offset = max(offset, int(update.get("update_id", 0)) + 1)
                    process_telegram_update(update)
            else:
                time.sleep(3)

    TELEGRAM_POLL_THREAD = threading.Thread(target=worker, name="evoflow-telegram-poll", daemon=True)
    TELEGRAM_POLL_THREAD.start()


WIDGET_JS = r'''(() => {
  const script = document.currentScript;
  const site = script && script.dataset.siteId;
  if (!site) return;
  const host = new URL(script.src).origin;
  const style = document.createElement('style');
  style.textContent = `.evow-fab{position:fixed;right:22px;bottom:22px;z-index:2147483000;background:#d9b866;color:#103c34;border:0;border-radius:999px;padding:13px 18px;font:700 14px Arial;box-shadow:0 8px 24px #0004;cursor:pointer}.evow-panel{position:fixed;right:22px;bottom:78px;width:min(340px,calc(100vw - 30px));height:440px;z-index:2147483000;background:#102f2a;color:#fff;border:1px solid #d9b866;border-radius:18px;box-shadow:0 18px 50px #0005;display:flex;flex-direction:column;overflow:hidden;font-family:Arial}.evow-head{padding:14px;background:#16463d;font-weight:bold}.evow-msgs{padding:12px;display:flex;flex-direction:column;gap:8px;overflow:auto;flex:1}.evow-b{padding:9px 11px;border-radius:12px;max-width:85%;font-size:13px;line-height:1.5}.evow-u{align-self:flex-start;background:#365f55}.evow-a{align-self:flex-end;background:#d9b866;color:#103c34}.evow-form{display:flex;gap:6px;padding:9px;border-top:1px solid #33675d}.evow-form input{min-width:0;flex:1;border-radius:10px;border:0;padding:9px}.evow-form button{border:0;border-radius:10px;background:#d9b866;color:#103c34;font-weight:bold;padding:8px 10px}`;
  document.head.appendChild(style);
  const fab = document.createElement('button'); fab.className='evow-fab'; fab.textContent='تحدث معنا'; document.body.appendChild(fab);
  const panel = document.createElement('div'); panel.className='evow-panel'; panel.hidden=true; panel.innerHTML='<div class="evow-head">مساعد Evoflow</div><div class="evow-msgs"><div class="evow-b evow-a">مرحبًا! كيف نساعدك؟</div></div><form class="evow-form"><input placeholder="اكتب رسالتك"/><button>إرسال</button></form>'; document.body.appendChild(panel);
  fab.onclick=()=>panel.hidden=!panel.hidden;
  const msgs=panel.querySelector('.evow-msgs'), form=panel.querySelector('form'), input=form.querySelector('input');
  form.onsubmit=async e=>{e.preventDefault();const text=input.value.trim();if(!text)return;input.value='';const u=document.createElement('div');u.className='evow-b evow-u';u.textContent=text;msgs.appendChild(u);const a=document.createElement('div');a.className='evow-b evow-a';try{const r=await fetch(host+'/api/site/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({site_id:site,message:text})});const d=await r.json();a.textContent=d.text||d.error||'سنعود إليك قريبًا.'}catch(err){a.textContent='تعذر الاتصال بمساعد Evoflow. تأكد أن الكود يأتي من رابط Evoflow عام وليس من معاينة مؤقتة.'}msgs.appendChild(a);msgs.scrollTop=msgs.scrollHeight};
})();'''


class Handler(BaseHTTPRequestHandler):
    server_version = "EvoflowPrototype/0.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never print request bodies or secrets.
        print(f"[{utc_now()}] {self.address_string()} {fmt % args}")

    def send_json(self, data: Any, status: int = 200, cors: bool = False) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/site/chat":
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.end_headers()
        else:
            self.send_error(405)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1_000_000:
            raise ValueError("الطلب كبير جدًا")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/health":
            self.send_json({"ok": True, "service": "evoflow", "time": utc_now()})
            return
        if path in ("/", "/index.html"): 
            raw = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/evoflow-logo.png":
            image = ROOT / "evoflow-logo.png"
            if not image.exists():
                self.send_error(404)
                return
            raw = image.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/widget.js":
            raw = WIDGET_JS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/config":
            self.send_json({"providers": RUNTIME.public(), "meta": public_meta(self), "channels": public_channels(), "assistant_prompt": ASSISTANT_SYSTEM_PROMPT, "logs": RUNTIME.logs()})
            return
        if path == "/api/channels":
            self.send_json({"channels": public_channels()})
            return
        if path == "/api/providers":
            self.send_json({"providers": RUNTIME.public()})
            return
        if path == "/api/logs":
            self.send_json({"logs": RUNTIME.logs()})
            return
        if path == "/api/meta/config":
            self.send_json(public_meta(self))
            return
        if path == "/api/meta/webhook":
            mode = query.get("hub.mode", [""])[0]
            token = query.get("hub.verify_token", [""])[0]
            challenge = query.get("hub.challenge", [""])[0]
            with META_LOCK:
                ok = mode == "subscribe" and secrets.compare_digest(token, META["verify_token"])
            if ok:
                raw = challenge.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.send_error(403, "verify token mismatch")
            return
        self.send_error(404)

    def do_POST(self) -> None:
        global ASSISTANT_SYSTEM_PROMPT
        path = urllib.parse.urlparse(self.path).path
        try:
            data = self.read_json()
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/settings/prompt":
            prompt = str(data.get("prompt", "")).strip()[:12000]
            if len(prompt) < 5:
                self.send_json({"ok": False, "error": "اكتب System Prompt أطول قليلًا."}, 400)
                return
            ASSISTANT_SYSTEM_PROMPT = prompt
            RUNTIME._log("settings", None, "تم تحديث System Prompt")
            self.send_json({"ok": True, "message": "تم حفظ System Prompt في جلسة الخادم."})
            return
        if path == "/api/telegram/connect":

            ok, msg, details = telegram_connect(str(data.get("token", "")), external_origin(self))
            self.send_json({"ok": ok, "message": msg, "details": details, "channels": public_channels()}, 200 if ok else 400)
            return
        if path == "/api/telegram/disconnect":
            with CHANNEL_LOCK:
                token = CHANNELS["telegram"].get("token", "")
                if token:
                    telegram_api(token, "deleteWebhook", {"drop_pending_updates": "false"})
                stop_telegram_polling()
                CHANNELS["telegram"] = {"connected": False, "bot_username": "", "webhook_url": "", "secret": "", "token": "", "transport": ""}
            self.send_json({"ok": True, "channels": public_channels()})
            return
        if path == "/api/site/create":
            name = str(data.get("name", "موقع جديد")).strip()[:100] or "موقع جديد"
            site_url = str(data.get("site_url", "")).strip()
            scrape = {"title": "", "description": "", "pages": 0, "dynamic_pages": 0, "documents": []}
            scrape_message = "لم يُدخل رابط للفحص؛ يمكنك استخدام Widget مع System Prompt فقط."
            if site_url:
                scrape_ok, scrape_message, scrape = crawl_public_site(site_url, max_pages=25)
                if not scrape_ok:
                    self.send_json({"ok": False, "error": scrape_message}, 400)
                    return
            site_id = "site_" + secrets.token_urlsafe(8).replace("-", "").replace("_", "")
            origin = external_origin(self)
            snippet = f'<script src="{origin}/widget.js" data-site-id="{site_id}" defer></script>'
            knowledge = "\n".join(
                f"{doc.get('title', '')}\n{doc.get('description', '')}\n{doc.get('text', '')[:2500]}"
                for doc in scrape.get("documents", [])[:25]
            )
            with CHANNEL_LOCK:
                SITES[site_id] = {
                    "name": name, "url": site_url,
                    "system_prompt": str(data.get("system_prompt", "")).strip()[:3000],
                    "knowledge": knowledge[:30000], "documents": scrape.get("documents", [])[:25],
                }
                CHANNELS["website"].update({"connected": True, "site_id": site_id, "site_name": name, "embed_code": snippet})
            RUNTIME._log("website", None, f"تم إنشاء كود Widget للموقع: {name}; {scrape_message}")
            self.send_json({
                "ok": True, "site_id": site_id, "embed_code": snippet,
                "scrape_message": scrape_message,
                "scrape": {k: scrape.get(k) for k in ("title", "description", "pages", "dynamic_pages")},
                "channels": public_channels(),
            })
            return
        if path == "/api/site/chat":
            site_id = str(data.get("site_id", "")).strip()
            message = str(data.get("message", "")).strip()[:5000]
            site = SITES.get(site_id)
            if not site or not message:
                self.send_json({"ok": False, "error": "الموقع أو الرسالة غير صالحة."}, 400, cors=True)
                return
            system = site.get("system_prompt") or "أنت مساعد خدمة عملاء مهذب لموقع تجاري. أجب باختصار وبالعربية عند الإمكان."
            knowledge = select_site_context(site, message)
            if knowledge:
                system += "\n\nمعلومات مستخرجة من صفحات الموقع. استخدمها عند الحاجة ولا تخترع معلومات غير موجودة. إذا لم توجد الإجابة قل ذلك بوضوح:\n" + knowledge[:9000]
            result = RUNTIME.route([{"role": "system", "content": system}, {"role": "user", "content": message}])
            self.send_json(result, cors=True)
            return
        if path.startswith("/api/telegram/webhook/"):
            self.handle_telegram_webhook(path.rsplit("/", 1)[-1], data)
            return
        if path == "/api/providers/configure":
            ok, msg, provider = RUNTIME.configure(data)
            self.send_json({"ok": ok, "message": msg, "provider": provider}, 200 if ok else 400)
            return
        if path.startswith("/api/providers/") and path.endswith("/toggle"):
            provider_id = path.split("/")[3]
            ok, msg = RUNTIME.toggle(provider_id, bool(data.get("enabled")))
            self.send_json({"ok": ok, "message": msg}, 200 if ok else 400)
            return
        if path.startswith("/api/providers/") and path.endswith("/test"):
            provider_id = path.split("/")[3]
            result = RUNTIME.test_provider(provider_id)
            self.send_json(result, 200 if result.get("ok") else 400)
            return
        if path == "/api/chat":
            messages = data.get("messages") or []
            if not isinstance(messages, list) or not messages:
                self.send_json({"ok": False, "error": "أدخل رسالة أولًا."}, 400)
                return
            safe_messages = []
            for m in messages[-12:]:
                if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant"):
                    safe_messages.append({"role": str(m["role"]), "content": str(m.get("content", ""))[:5000]})
            self.send_json(RUNTIME.route(safe_messages, int(data.get("max_tokens", 500))))
            return
        if path == "/api/meta/embedded/exchange":
            # The official Embedded Signup returns a short-lived OAuth code.
            # Exchange it server-side so the app secret never reaches the browser.
            app_id = os.environ.get("META_APP_ID", "").strip()
            app_secret = os.environ.get("META_APP_SECRET", "").strip()
            code = str(data.get("code", "")).strip()
            phone_id = str(data.get("phone_number_id", "")).strip()
            waba_id = str(data.get("waba_id", "")).strip()
            if not app_id or not app_secret:
                self.send_json({"ok": False, "error": "ينقص META_APP_ID أو META_APP_SECRET في الخادم."}, 400)
                return
            if not code:
                self.send_json({"ok": False, "error": "لم يصل كود Embedded Signup."}, 400)
                return
            query = urllib.parse.urlencode({"client_id": app_id, "client_secret": app_secret, "code": code})
            exchange_url = f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token?{query}"
            try:
                with urllib.request.urlopen(urllib.request.Request(exchange_url, method="GET"), timeout=15) as response:
                    exchanged = json.loads(response.read().decode("utf-8", "replace"))
                access_token = str(exchanged.get("access_token", "")).strip()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:260]
                self.send_json({"ok": False, "error": f"Meta رفض تبادل الكود (HTTP {exc.code}). {detail}"}, 400)
                return
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": f"تعذر تبادل كود Meta: {exc}"}, 400)
                return
            if not access_token:
                self.send_json({"ok": False, "error": "Meta لم ترجع Access Token."}, 400)
                return
            # Meta's postMessage/sessionInfo normally gives these IDs. If the
            # browser has not received them yet, keep no token and ask for the
            # IDs rather than creating a half-connected tenant.
            if not phone_id:
                self.send_json({"ok": False, "error": "تمت مصادقة Meta، لكن لم يصل Phone Number ID. أعد المحاولة من نافذة Embedded Signup."}, 400)
                return
            ok, msg, info = meta_validate({
                "phone_number_id": phone_id, "waba_id": waba_id,
                "access_token": access_token, "verify_token": data.get("verify_token", ""),
            })
            self.send_json({"ok": ok, "message": msg, "meta": public_meta(self), "details": info}, 200 if ok else 400)
            return
        if path == "/api/meta/save":
            ok, msg, info = meta_validate(data)
            self.send_json({"ok": ok, "message": msg, "meta": public_meta(self), "details": info}, 200 if ok else 400)
            return
        if path == "/api/meta/disconnect":
            with META_LOCK:
                META.update({"phone_number_id": "", "waba_id": "", "access_token": "", "connected": False, "display_phone_number": "", "business_name": ""})
            self.send_json({"ok": True, "meta": public_meta(self)})
            return
        if path == "/api/meta/webhook":
            return self.handle_meta_webhook(data)
        self.send_error(404)

    def handle_telegram_webhook(self, slug: str, data: dict[str, Any]) -> None:
        with CHANNEL_LOCK:
            channel = dict(CHANNELS["telegram"])
        supplied = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not channel.get("connected") or not secrets.compare_digest(slug, channel.get("webhook_url", "").rsplit("/", 1)[-1]) or not secrets.compare_digest(supplied, channel.get("secret", "")):
            self.send_json({"ok": False, "error": "Telegram webhook غير مصرح."}, 403)
            return
        message = data.get("message", {}) or {}
        chat = message.get("chat", {}) or {}
        text = (message.get("text") or "").strip()
        if not chat.get("id") or not text:
            self.send_json({"ok": True})
            return
        result = RUNTIME.route([
            {"role": "system", "content": ASSISTANT_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ])
        reply = result.get("text") if result.get("ok") else "وصلت رسالتك، وسنرد عليك قريبًا."
        ok, detail = send_telegram_message(chat["id"], reply)
        self.send_json({"ok": ok, "provider": result.get("provider"), "detail": detail})

    def handle_meta_webhook(self, data: dict[str, Any]) -> None:
        # Meta sends an object with entry[].changes[].value.messages[].
        RUNTIME._log("meta", None, "وصل حدث Webhook من Meta")
        sent = []
        for entry in data.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                for message in value.get("messages", []) or []:
                    sender = message.get("from")
                    text = (message.get("text") or {}).get("body", "")
                    if sender and text:
                        result = RUNTIME.route([
                            {"role": "system", "content": ASSISTANT_SYSTEM_PROMPT},
                            {"role": "user", "content": text},
                        ])
                        reply = result.get("text") if result.get("ok") else "وصلت رسالتك، وسنرد عليك قريبًا."
                        ok, detail = send_whatsapp_message(sender, reply)
                        sent.append({"to": sender, "ok": ok, "provider": result.get("provider"), "detail": detail})
        self.send_json({"ok": True, "processed": len(sent), "sent": sent})


def main() -> None:
    if not INDEX.exists():
        raise SystemExit(f"Missing {INDEX}")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"مساعد الأعمال prototype listening on http://{HOST}:{PORT}")
    print("API keys stay in RAM only; this is not a production deployment.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
