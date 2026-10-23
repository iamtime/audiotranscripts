"""LLM providers for Beautify and chat when Claude Code is not used ("api" engine).

Every provider is spoken to over the OpenAI-compatible chat-completions protocol
(the openai SDK with a base URL): OpenAI, Gemini, Anthropic and OpenRouter offer
such an endpoint, and so do local servers (Ollama, LM Studio, vLLM) -- "custom".

API keys are kept out of settings.json, in .webui/secrets.json (mode 600);
the usual environment variables (GEMINI_API_KEY, OPENAI_API_KEY, ...) work too.
"""

import os

import sessions as S

PROVIDERS = {
    "gemini": {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "model": "gemini-3.5-flash", "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
               "key_url": "https://aistudio.google.com/apikey"},
    "openai": {"name": "OpenAI (ChatGPT)", "base_url": "https://api.openai.com/v1",
               "model": "gpt-5", "env": ("OPENAI_API_KEY",),
               "key_url": "https://platform.openai.com/api-keys"},
    "anthropic": {"name": "Anthropic API (Claude)", "base_url": "https://api.anthropic.com/v1/",
                  "model": "claude-sonnet-5-5", "env": ("ANTHROPIC_API_KEY",),
                  "key_url": "https://console.anthropic.com/settings/keys"},
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                   "model": "google/gemini-2.5-flash", "env": ("OPENROUTER_API_KEY",),
                   "key_url": "https://openrouter.ai/keys"},
    "custom": {"name": "Other (OpenAI-compatible)", "base_url": "", "model": "", "env": (),
               "key_url": ""},
}
SECRETS = S.WEBUI_DIR / "secrets.json"
MAX_OUTPUT = 32000   # Anthropic requires max_tokens; a full annotated transcript is long


# ── Keys ─────────────────────────────────────────────────────────────────────

def _secrets() -> dict:
    return S._read_json(SECRETS, {"api_keys": {}})


def stored_key(provider: str) -> str:
    return _secrets()["api_keys"].get(provider, "")


def env_key(provider: str) -> str:
    for name in PROVIDERS.get(provider, {}).get("env", ()):
        if os.environ.get(name):
            return os.environ[name]
    return ""


def api_key(provider: str) -> str:
    return stored_key(provider) or env_key(provider)


def set_key(provider: str, key: str | None):
    data = _secrets()
    if key:
        data["api_keys"][provider] = key.strip()
    else:
        data["api_keys"].pop(provider, None)
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    S._write_json(SECRETS, data)
    os.chmod(SECRETS, 0o600)


def mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else ("•" * len(key))


# ── Configuration ────────────────────────────────────────────────────────────

def config(settings: dict | None = None) -> dict:
    """The effective provider settings: provider, base_url, model, key."""
    s = settings or S.load_settings()
    provider = s.get("api_provider") or "gemini"
    p = PROVIDERS.get(provider, PROVIDERS["custom"])
    return {"provider": provider, "base_url": (s.get("api_base_url") or "").strip() or p["base_url"],
            "model": (s.get("api_model") or "").strip() or p["model"], "key": api_key(provider)}


def problem(cfg: dict) -> str:
    """Why this configuration cannot work yet ('' when it can)."""
    if not cfg["base_url"]:
        return "Set the API host (base URL)"
    if not cfg["model"]:
        return "Choose a model"
    if not cfg["key"] and cfg["provider"] != "custom":   # local servers often need no key
        return f"Add the {PROVIDERS[cfg['provider']]['name']} API key"
    return ""


def status(settings: dict, claude_found: bool) -> dict:
    """What the UI needs to know: is Beautify/chat usable, and with what."""
    engine = settings.get("beautify_engine") or "claude"
    if engine == "api":
        cfg = config(settings)
        why = problem(cfg)
        label = f"{PROVIDERS.get(cfg['provider'], PROVIDERS['custom'])['name']} · {cfg['model'] or '?'}"
    else:
        why = "" if claude_found else "Claude Code (the claude CLI) is not installed"
        label = "Claude Code"
    return {"engine": engine, "ready": not why, "problem": why, "label": label,
            "setup_done": bool(settings.get("ai_setup_done"))}


def public(settings: dict) -> dict:
    """Provider list and saved values for the setup dialog (keys masked)."""
    out = {}
    for pid, p in PROVIDERS.items():
        k = stored_key(pid)
        out[pid] = {"name": p["name"], "base_url": p["base_url"], "model": p["model"], "key_url": p["key_url"],
                    "key": mask(k) if k else "", "env_key": bool(env_key(pid))}
    return {"providers": out, "provider": settings.get("api_provider") or "gemini",
            "model": settings.get("api_model") or "", "base_url": settings.get("api_base_url") or ""}


# ── Calls ────────────────────────────────────────────────────────────────────

def client(cfg: dict, timeout: float = 900):
    from openai import OpenAI
    return OpenAI(api_key=cfg["key"] or "none", base_url=cfg["base_url"], timeout=timeout, max_retries=1)


def _kwargs(cfg: dict) -> dict:
    return {"max_tokens": MAX_OUTPUT} if cfg["provider"] == "anthropic" else {}


def stream(cfg: dict, messages: list[dict], on_delta=None) -> str:
    """Run a chat completion, streaming; returns the whole reply."""
    why = problem(cfg)
    if why:
        raise RuntimeError(why)
    parts = []
    resp = client(cfg).chat.completions.create(model=cfg["model"], messages=messages, stream=True, **_kwargs(cfg))
    for chunk in resp:
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta.content or ""
        if d:
            parts.append(d)
            if on_delta:
                on_delta(d)
    text = "".join(parts)
    if not text.strip():
        raise RuntimeError("The model returned an empty answer")
    return text


def test(cfg: dict) -> dict:
    """Check the key/host: list the models, then (if a model is set) ask for one word."""
    if not cfg["base_url"]:
        return {"ok": False, "error": "Set the API host (base URL)"}
    c = client(cfg, timeout=40)
    models = []
    try:
        models = sorted(m.id.removeprefix("models/") for m in c.models.list())
    except Exception as e:
        listing_error = explain(e)
    else:
        listing_error = ""
    if cfg["model"]:
        try:
            r = c.chat.completions.create(model=cfg["model"], messages=[{"role": "user", "content": "Reply with: OK"}],
                                          **({"max_tokens": 10} if cfg["provider"] == "anthropic" else {}))
            reply = (r.choices[0].message.content or "").strip()
            return {"ok": True, "models": models, "reply": reply[:40]}
        except Exception as e:
            return {"ok": False, "models": models, "error": explain(e)}
    if listing_error:
        return {"ok": False, "error": listing_error}
    return {"ok": True, "models": models}


def explain(e: Exception) -> str:
    name = type(e).__name__
    msg = str(e)
    low = msg.lower()
    if name in ("AuthenticationError", "PermissionDeniedError") or " 401" in msg or (
            "api key" in low and any(w in low for w in ("valid", "invalid", "incorrect", "expired"))) or "x-api-key" in low:
        return "The API key was rejected"
    if name == "NotFoundError" or " 404" in msg:
        return f"Not found (wrong model name or host): {msg[:200]}"
    if name in ("APIConnectionError", "APITimeoutError"):
        return "Could not reach the API host (internet connection, or the base URL)"
    if name == "RateLimitError" or " 429" in msg:
        return f"Rate limit or quota exceeded: {msg[:200]}"
    return f"{name}: {msg[:300]}"

