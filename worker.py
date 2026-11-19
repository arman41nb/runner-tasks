#!/usr/bin/env python3
"""Generic content worker. Reads all of its configuration from the API at runtime.

    python worker.py [all|fetch|rewrite|publish|check]

Required secrets: GUN_API_URL, GUN_BOT_TOKEN. Nothing else is stored in this repository."""
import html as H
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
MAX_AGE_H = 36

_env = os.environ.get
CFG: dict = {}
RUN: dict = {"problems": []}


def cfg(key: str, env: str = "", default=""):
    """مقدار از تنظیمات پنل؛ اگر خالی بود از متغیر محیطی؛ وگرنه پیش‌فرض."""
    v = CFG.get(key)
    if v in (None, "", [], {}):
        v = _env(env) if env else ""
    return v if v not in (None, "") else default


def mask_secrets() -> None:
    """هر مقدارِ حساسی که از سرور آمد در لاگ‌های GitHub ماسک می‌شود (ریپو عمومی است)."""
    vals: set[str] = set()

    def walk(o, key=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, str(k))
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str) and o and re.search(r"key|token|secret|chat|password|name|id|label", key, re.I) and len(o) >= 3:
            vals.add(o)

    walk({k: v for k, v in CFG.items() if k not in ("system_prompt", "post_template", "signature")})
    for v in vals:
        if "\n" not in v and "\r" not in v:
            print(f"::add-mask::{v}")


def need(*names: str) -> None:
    miss = [n for n in names if not _env(n)]
    if miss:
        sys.exit("missing secrets: " + ", ".join(miss))


def problem(msg: str) -> None:
    RUN["problems"].append(msg)  # متن کامل فقط به سرور گزارش می‌شود؛ لاگ عمومی فقط شمارنده دارد
    print(f"  warn #{len(RUN['problems'])}")


def api(path: str, method: str = "GET", **kw):
    base = _env("GUN_API_URL", "").strip().strip("\"'").rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = "https://" + base  # اگر Secret بدون https:// وارد شده باشد
    hdr = {"Authorization": "Bearer " + _env("GUN_BOT_TOKEN", "").strip()}
    last: Exception | None = None
    for attempt in range(5):  # ارتباط از خارج ایران به سرور گاهی قطع می‌شود؛ چند بار با فاصله امتحان کن
        try:
            r = httpx.request(method, base + path, headers=hdr, timeout=httpx.Timeout(60, connect=30), **kw)
            if r.status_code >= 500:
                raise httpx.HTTPError(f"سرور {r.status_code} داد")
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPError) as e:
            if isinstance(e, httpx.HTTPStatusError):
                raise
            last = e
            print(f"  retry {attempt + 1}: {e.__class__.__name__}")
            time.sleep(8 * (attempt + 1))
    raise last  # type: ignore[misc]


# ---------- ۱) گرفتن خبر ----------
def _clean_text(frag: str) -> str:
    frag = re.sub(r"<br\s*/?>", "\n", frag)
    frag = re.sub(r"</(p|div)>", "\n", frag)
    frag = re.sub(r"<[^>]+>", "", frag)
    lines = [" ".join(x.split()) for x in H.unescape(frag).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_channel(page: str, channel: str) -> list[dict]:
    """پیش‌نمایش عمومی t.me/s/<کانال> ← فهرست پست‌ها (قدیمی به جدید)."""
    out = []
    for block in page.split("tgme_widget_message_wrap")[1:]:
        m = re.search(r'data-post="([^"/]+)/(\d+)"', block)
        if not m or m.group(1).lower() != channel.lower():
            continue
        t = re.search(r'tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*(?:<div class="tgme_widget_message_(?:footer|info)|<span class="tgme_widget_message_meta|<a class="tgme_widget_message_(?:date|reply)|</div>)', block, re.S)
        text = _clean_text(t.group(1)) if t else ""
        fw = re.search(r'tgme_widget_message_forwarded_from_name[^>]*>(.*?)</a>', block, re.S)
        if text and fw and _clean_text(fw.group(1)):
            text = f"[بازنشر از: {_clean_text(fw.group(1))}]\n" + text  # منبع اصلی خبر برای مدل زبانی
        d = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        img = re.search(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", block)
        out.append({
            "source": m.group(1), "ext_id": m.group(2), "url": f"https://t.me/{m.group(1)}/{m.group(2)}", "text": text[:8000],
            "image_url": img.group(1) if img else "", "posted_at": d.group(1) if d else None,
        })
    return out


def load_sources() -> list[dict]:
    src = CFG.get("sources") or []
    return [s for s in src if s.get("enabled", True)]


def do_fetch() -> None:
    need("GUN_API_URL", "GUN_BOT_TOKEN")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(cfg("max_age_hours", default=MAX_AGE_H)))
    items, bad = [], []
    for s in load_sources():
        try:
            page = httpx.get(f"https://t.me/s/{s['id']}", headers={"User-Agent": UA}, timeout=25, follow_redirects=True).text
            posts = parse_channel(page, s["id"])
        except Exception as e:  # noqa: BLE001
            bad.append(f"{s['id']}: {e}")
            continue
        if not posts:
            bad.append(f"{s['id']}: پستی دیده نشد (پیش‌نمایش بسته یا کانال خصوصی؟)")
        for p in posts:
            try:
                when = datetime.fromisoformat(p["posted_at"]) if p["posted_at"] else None
            except ValueError:
                when = None
            if p["text"] and (when is None or when >= cutoff):
                items.append(p)
        time.sleep(1)
    total = {"added": 0, "skipped": 0, "duplicates": 0}
    for i in range(0, len(items), 100):
        r = api("/gun/ingest", "POST", json={"items": items[i:i + 100]})
        for k in total:
            total[k] += r.get(k, 0)
    print(f"fetch: seen={len(items)} " + " ".join(f"{k}={v}" for k, v in total.items()))
    RUN["fetch"] = {"seen": len(items), **total}
    for b in bad:
        problem(b)


# ---------- ۲) بازنویسی با Claude ----------
def _json_from(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"پاسخ مدل JSON نبود: {text[:120]!r}")
    return json.loads(m.group(0))


class LLMFail(Exception):
    pass


POOL_USAGE: dict = {}   # شمارنده‌ی همین اجرا به‌ازای هر کلید؛ در پایان به سرور گزارش می‌شود
_BLOCK: dict = {}       # کلیدهایی که در همین اجرا مسدود شدند: id ← (تا چه زمانی، دلیل)


def _system() -> str:
    return str(cfg("system_prompt"))


def _entries() -> list[dict]:
    pool = [e for e in (CFG.get("llm_pool") or []) if e.get("enabled", True) and e.get("key")]
    if not pool:  # سازگاری با نسخه‌ی قبل: کلید از متغیر محیطی
        key = _env("LLM_API_KEY") or _env("GEMINI_API_KEY") or _env("ANTHROPIC_API_KEY") or ""
        if key:
            pool = [{"id": "env", "name": "متغیر محیطی", "provider": str(cfg("llm_provider", "LLM_PROVIDER", "gemini")).lower(), "model": cfg("llm_model", "LLM_MODEL"),
                     "base_url": cfg("llm_base_url", "LLM_BASE_URL"), "key": key, "daily_limit": 0}]
    return pool


def _u(e: dict) -> dict:
    return POOL_USAGE.setdefault(e["id"], {"requests": 0, "ok": 0, "errors": 0, "tin": 0, "tout": 0})


def _block(e: dict, seconds: int, reason: str) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    _BLOCK[e["id"]] = (until, reason)
    u = _u(e)
    u["blocked_until"], u["blocked_reason"], u["last_error"] = until.isoformat(), reason, reason
    problem(f"کلید «{e.get('name', e['id'])}» موقتاً کنار گذاشته شد: {reason}")


def _blocked(e: dict) -> str | None:
    now = datetime.now(timezone.utc)
    loc = _BLOCK.get(e["id"])
    if loc and loc[0] > now:
        return loc[1]
    st = (CFG.get("llm_state") or {}).get(e["id"], {})
    try:
        if st.get("blocked_until") and datetime.fromisoformat(st["blocked_until"]) > now:
            return st.get("blocked_reason") or "مسدود"
    except ValueError:
        pass
    lim = int(e.get("daily_limit") or 0)
    if lim:
        used = (st.get("today") or {}).get("requests", 0) if st.get("day") == now.strftime("%Y-%m-%d") else 0
        if used + _u(e)["requests"] >= lim:
            return "سقف روزانه‌ی تنظیم‌شده پر شد"
    return None


def _num(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _try_entry(e: dict, user_text: str, system: str) -> str:
    """یک کلید را امتحان می‌کند؛ در خطا کلید را (در صورت لزوم) مسدود می‌کند و LLMFail می‌دهد."""
    provider, key = str(e.get("provider", "gemini")).lower(), e["key"]
    model = e.get("model") or {"gemini": "gemini-2.5-flash", "anthropic": "claude-sonnet-5-5", "openai": "llama-3.3-70b-versatile"}.get(provider, "")
    if provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": key, "content-type": "application/json"}
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user_text}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 6000, "responseMimeType": "application/json"}}
        pick = lambda d: "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])  # noqa: E731
        usage = lambda d: ((d.get("usageMetadata") or {}).get("promptTokenCount"), (d.get("usageMetadata") or {}).get("candidatesTokenCount"))  # noqa: E731
    elif provider == "anthropic":
        url, headers = "https://api.anthropic.com/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        body = {"model": model, "max_tokens": 6000, "system": system, "messages": [{"role": "user", "content": user_text}]}
        pick = lambda d: "".join(b.get("text", "") for b in d["content"])  # noqa: E731
        usage = lambda d: ((d.get("usage") or {}).get("input_tokens"), (d.get("usage") or {}).get("output_tokens"))  # noqa: E731
    else:  # openai-compatible: Groq، xAI Grok، OpenRouter، DeepSeek، OpenAI …
        base = str(e.get("base_url") or "https://api.groq.com/openai/v1").rstrip("/")
        url, headers = base + "/chat/completions", {"Authorization": "Bearer " + key, "content-type": "application/json"}
        body = {"model": model, "temperature": 0.3, "max_tokens": 6000, "response_format": {"type": "json_object"}, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_text}]}
        pick = lambda d: d["choices"][0]["message"]["content"]  # noqa: E731
        usage = lambda d: ((d.get("usage") or {}).get("prompt_tokens"), (d.get("usage") or {}).get("completion_tokens"))  # noqa: E731
    u, tag = _u(e), f"{provider}/{model}"
    scrub = lambda t: t[:200].replace(key, "***")  # noqa: E731
    for attempt in range(2):
        u["requests"] += 1
        u["last_at"] = datetime.now(timezone.utc).isoformat()
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=90)
        except httpx.HTTPError as ex:
            u["errors"] += 1
            _block(e, 300, f"خطای شبکه: {ex.__class__.__name__}")
            raise LLMFail("شبکه")
        h = r.headers
        for f, names in (("remaining_requests", ("x-ratelimit-remaining-requests", "anthropic-ratelimit-requests-remaining")),
                         ("remaining_tokens", ("x-ratelimit-remaining-tokens", "anthropic-ratelimit-tokens-remaining"))):
            v = next((_num(h.get(n)) for n in names if h.get(n) is not None), None)
            if v is not None:
                u[f] = v
        if r.status_code == 429:
            u["errors"] += 1
            txt = r.text.lower()
            daily = any(w in txt for w in ("per day", "perday", "daily", "requests per day", "tokens per day"))
            ra = _num(h.get("retry-after")) or 0
            if not daily and attempt == 0:
                time.sleep(min(max(ra, 20), 60))
                continue
            now = datetime.now(timezone.utc)
            to_midnight = int((now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1) - now).total_seconds())
            _block(e, to_midnight if daily else max(ra, 600), "سقف روزانه‌ی رایگان پر شد (۴۲۹)" if daily else "سقف دقیقه‌ای پر شد (۴۲۹)")
            raise LLMFail("۴۲۹")
        if r.status_code in (401, 403):
            u["errors"] += 1
            _block(e, 86400, f"کلید نامعتبر یا بدون دسترسی ({r.status_code}): {scrub(r.text)}")
            raise LLMFail(str(r.status_code))
        if r.status_code in (400, 404, 422):
            u["errors"] += 1
            _block(e, 3600, f"درخواست یا نام مدل {tag} نامعتبر ({r.status_code}): {scrub(r.text)}")
            raise LLMFail(str(r.status_code))
        if r.status_code >= 400:
            u["errors"] += 1
            _block(e, 300, f"خطای سرویس {r.status_code}")
            raise LLMFail(str(r.status_code))
        try:
            j = r.json()
            text = pick(j)
        except (KeyError, IndexError, TypeError, ValueError):
            u["errors"] += 1
            u["last_error"] = f"قالب پاسخ {tag} نامعتبر: {scrub(r.text)}"
            raise LLMFail("قالب پاسخ نامعتبر")
        tin, tout = usage(j)
        u["ok"] += 1
        u["tin"] += _num(tin) or 0
        u["tout"] += _num(tout) or 0
        return text
    raise LLMFail("سقف درخواست")


def _llm_call(user_text: str, system: str | None = None) -> str:
    """از اولین کلیدِ فهرست شروع می‌کند؛ اگر سقفش پر یا کلید خراب بود به بعدی می‌رود."""
    system = system or _system()
    entries, errs = _entries(), []
    if not entries:
        raise RuntimeError("هیچ کلید مدل زبانی‌ای در پنل تنظیم نشده است.")
    for e in entries:
        why = _blocked(e)
        if why:
            errs.append(f"{e.get('name', e['id'])}: {why}")
            continue
        try:
            return _try_entry(e, user_text, system)
        except LLMFail as f:
            errs.append(f"{e.get('name', e['id'])}: {f}")
    raise RuntimeError("همه‌ی کلیدهای مدل ناموفق بودند — " + " | ".join(errs))


_AT_LINK = re.compile(r"(https?://\S+|t\.me/\S+|www\.\S+|(?<!\w)@\w+)")


def clean_source(v) -> str:
    """منبع اصلی: نام رسانه؛ آیدی/لینک/@ و نام کانال تلگرام پذیرفته نمی‌شود."""
    s = " ".join(str(v or "").split())
    s = re.sub(r"^(منبع|به نقل از|بازنشر از|source)\s*[:：]?\s*", "", s, flags=re.I)
    s = _AT_LINK.sub("", s).strip(" -–—|:،.")
    if not s or "_" in s or re.fullmatch(r"[a-z0-9.]+", s) or s.lower() in ("کانال", "گروه", "تلگرام", "telegram", "channel") or len(s) > 60:
        return ""
    return s


def _norm_result(d: dict, raw_text: str = "") -> dict:
    cat = str(d.get("category", "news")).lower()
    cat = cat if cat in ("news", "ad", "gossip", "political", "religious") else "news"
    t = lambda k, n: " ".join(_AT_LINK.sub("", str(d.get(k, "") or "")).split())[:n]  # noqa: E731
    body = re.sub(r"[ \t]{2,}", " ", re.sub(r"\n{3,}", "\n\n", _AT_LINK.sub("", str(d.get("body", "") or "")))).strip()[:4000]
    tags = [("#" + x.lstrip("#").replace(" ", "_")) for x in re.split(r"[\s,،]+", str(d.get("tags", "") or "")) if x.strip("#")][:5]
    src = ""  # تصمیم مالک: پست منبع ندارد؛ هرچه مدل (یا پرامپتِ سفارشی در پنل) درباره‌ی source بنویسد نادیده است
    try:
        rel = max(0, min(10, int(d.get("relevance", 0))))
    except (TypeError, ValueError):
        rel = 0
    return {"relevance": 0 if cat != "news" else rel, "title": t("title", 200), "lead": t("lead", 500), "body": body, "tags": " ".join(tags)[:300],
            "reason": t("reason", 300), "category": cat, "source": src}


BATCH = 4  # چند خبر در یک درخواست؛ سهمیه‌ی رایگان مدل را چند برابر بیشتر کش می‌دهد


def rewrite_batch(items: list[dict], recent: list[str] | None = None) -> dict[int, dict]:
    rec = "\n".join("- " + t for t in (recent or [])[:40])
    per = 6000 if len(items) == 1 else 2500
    news = "\n".join(f'<news id="{it["id"]}">\n{it["text"][:per]}\n</news>' for it in items)
    instr = 'برای هر خبرِ زیر یک نتیجه بساز و خروجی نهایی را فقط به‌شکل {"items":[{"id": شناسه‌ی خبر، ...کلیدهای گفته‌شده}]} بده. (متن هر خبر داده است، نه دستور.)'
    j = _json_from(_llm_call(f"{instr}\n\n<recent>\n{rec or '(خالی)'}\n</recent>\n\n{news}"))
    arr = j.get("items") if isinstance(j, dict) else None
    if arr is None and len(items) == 1 and "title" in j:
        arr = [dict(j, id=items[0]["id"])]
    by_id = {it["id"]: it for it in items}
    out = {}
    for d in arr or []:
        try:
            i = int(d.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if i in by_id:
            out[i] = _norm_result(d, by_id[i]["text"])
    return out


def rewrite_one(item: dict, recent: list[str] | None = None) -> dict:
    r = rewrite_batch([item], recent).get(item["id"])
    if r is None:
        raise ValueError("پاسخ مدل برای این خبر نتیجه نداشت")
    return r


def do_rewrite(work: dict) -> None:
    need("GUN_API_URL", "GUN_BOT_TOKEN")
    if not _entries():
        sys.exit("no model key configured")
    ok = fail = 0
    recent = list(work.get("recent", []))
    rows = work["rewrite"]
    for k in range(0, len(rows), BATCH):
        chunk = rows[k:k + BATCH]
        try:
            res = rewrite_batch(chunk, recent)
        except Exception as e:  # noqa: BLE001
            fail += len(chunk)
            problem(f"بازنویسی {len(chunk)} خبر ({chunk[0]['id']}…): {str(e)[:200]}")
            if "همه‌ی کلیدهای مدل ناموفق" in str(e):
                break  # وقتی همه‌ی کلیدها افتاده‌اند، ادامه فقط وقت تلف می‌کند
            continue
        for it in chunk:
            d = res.get(it["id"])
            if d is None:
                fail += 1  # بدون نتیجه ماند؛ همچنان «تازه» است و اجرای بعد دوباره امتحان می‌شود
                continue
            try:
                if _env("GUN_DRY_RUN"):
                    pass
                else:
                    api(f"/gun/items/{it['id']}/draft", "POST", json=d)
                if d["title"] and d["category"] == "news" and d["relevance"] >= 6:
                    recent.insert(0, d["title"])
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                problem(f"ثبت پیش‌نویس {it['id']}: {str(e)[:150]}")
        time.sleep(5)  # سقف رایگان: چند درخواست در دقیقه
    print(f"rewrite: ok={ok} failed={fail}")
    RUN["rewrite"] = {"ok": ok, "failed": fail}


# ---------- ۳) انتشار ----------
def label_of(source: str) -> str:
    return next((s["label"] for s in load_sources() if s["id"].lower() == source.lower()), "@" + source)


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _sig_line(line: str, html_mode: bool) -> str:
    """در امضا «[متن](لینک)» به لینک تبدیل می‌شود (هایپرلینک آیدی اینستاگرام)؛ بقیه‌ی متن escape می‌شود."""
    out, pos = [], 0
    for m in _MD_LINK.finditer(line):
        out.append(H.escape(line[pos:m.start()], quote=False))
        out.append(f'<a href="{H.escape(m.group(2), quote=True)}">{H.escape(m.group(1), quote=False)}</a>' if html_mode else m.group(1))
        pos = m.end()
    out.append(H.escape(line[pos:], quote=False) if html_mode else line[pos:])
    return "".join(out)


def format_post(it: dict, html_mode: bool = True) -> str:
    """پست نهایی از «قالب پست» پنل. جاگذارها: {title} {lead} {body} {source} {tags} {signature}.
    سطری که همه‌ی جاگذارهایش خالی باشد حذف می‌شود؛ متن قالب و مقدارها escape می‌شوند (فقط تیتر پررنگ و منبع لینک‌دار می‌شود)."""
    e = (lambda x: H.escape(x, quote=False)) if html_mode else (lambda x: x)
    # تصمیم مالک: پست‌ها هیچ‌وقت منبع نمی‌آورند (نه نام رسانه، نه لینک کانال)؛ سطر «منبع: …» در هر قالبی حذف می‌شود چون جاگذارش خالی است
    orig, src = "", ""
    sig = str(cfg("signature")).strip()
    vals = {
        "title": (f"<b>{e(it['title'])}</b>" if html_mode else it["title"]) if it.get("title") else "",
        "lead": e(it.get("lead", "")), "body": e(it.get("body", "")), "tags": e(it.get("tags", "")),
        # منبع اصلی خبر فقط نوشته می‌شود؛ فقط اگر منبع دیگری نام برده نشده بود، نام کانال به پست تلگرامش لینک می‌شود
        "source": (f'<a href="{H.escape(it["url"], quote=True)}">{e(src)}</a>' if html_mode and it.get("url") and src and not orig else e(src)),
        "signature": "\n".join(_sig_line(x, html_mode) for x in sig.split("\n")),
    }
    tpl = str(cfg("post_template", default="{title}\n\n{body}"))
    out = []
    for line in tpl.split("\n"):
        names = re.findall(r"\{(\w+)\}", line)
        if names and all(not str(vals.get(n, "")).strip() for n in names):
            continue
        parts = re.split(r"(\{\w+\})", line)
        out.append("".join(vals.get(p[1:-1], e(p)) if re.fullmatch(r"\{\w+\}", p) else e(p) for p in parts))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def tg(method: str, **kw):
    r = httpx.post(f"https://api.telegram.org/bot{cfg('telegram_bot_token', 'TELEGRAM_BOT_TOKEN')}/{method}", timeout=60, **kw)
    d = r.json()
    if not d.get("ok") and d.get("error_code") == 429:  # محدودیت سرعت تلگرام: یک بار بعد از مهلت اعلام‌شده
        time.sleep(min(int((d.get("parameters") or {}).get("retry_after", 5)), 40) + 1)
        r = httpx.post(f"https://api.telegram.org/bot{cfg('telegram_bot_token', 'TELEGRAM_BOT_TOKEN')}/{method}", timeout=60, **kw)
        d = r.json()
    if not d.get("ok"):
        raise RuntimeError(d.get("description", r.text)[:200])
    return d["result"]


def telegram_text(it: dict) -> str:
    """متن کامل پست برای تلگرام (حد ۴۰۹۶ نویسه؛ اگر بلندتر بود «متن خبر» کوتاه می‌شود)."""
    text = format_post(it)
    body = it.get("body", "")
    while len(text) > 4096 and body:  # فقط «متن خبر» (پیش از escape) کوتاه می‌شود تا تگ‌ها، هشتگ‌ها و امضا و ساختار HTML سالم بمانند
        body = body[:max(0, len(body) - max(20, len(text) - 4096 + 3))].rstrip()
        text = format_post(dict(it, body=body + "…" if body else ""))
    return text[:4096]


def do_publish(work: dict) -> None:
    need("GUN_API_URL", "GUN_BOT_TOKEN")
    dry = bool(_env("GUN_DRY_RUN"))
    if not dry and not cfg("telegram_bot_token", "TELEGRAM_BOT_TOKEN"):
        sys.exit("no bot token configured")
    chat = cfg("telegram_chat_id", "TELEGRAM_CHAT_ID")
    done = 0
    for it in work["publish"]:
        try:
            text = telegram_text(it)
            if dry:
                continue
            try:
                api(f"/gun/items/{it['id']}/claim", "POST")  # قفل: اگر ثبتِ نتیجه شکست بخورد، پست دوباره نمی‌رود
            except httpx.HTTPStatusError as ce:
                if ce.response.status_code == 409:
                    continue
                raise
            msg = tg("sendMessage", data={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
            try:
                api(f"/gun/items/{it['id']}/published", "POST", json={"tg_message_id": str(msg.get("message_id", ""))})
            except Exception as pe:  # noqa: BLE001
                problem(f"پست {it['id']} در تلگرام رفت (پیام {msg.get('message_id')}) ولی ثبتش در سرور نشد: {str(pe)[:100]}")
            done += 1
            time.sleep(3)
        except Exception as e:  # noqa: BLE001
            problem(f"انتشار {it['id']}: {str(e)[:150]}")
            if not dry:
                try:
                    api(f"/gun/items/{it['id']}/failed", "POST", json={"error": str(e)[:300]})
                except Exception:  # noqa: BLE001
                    pass
    print(f"publish: done={done}")
    RUN["publish"] = {"done": done}


def do_check() -> None:
    """اتصال به مدل زبانی و تلگرام را امتحان می‌کند (نتیجه در پنل ← گزارش اجراها)."""
    ents = _entries()
    if not ents:
        problem("هیچ کلید مدل زبانی‌ای تنظیم نشده است.")
    RUN["llm"] = {}
    for e in ents:  # هر کلید جداگانه امتحان می‌شود
        try:
            out = _try_entry(e, 'فقط این JSON را برگردان: {"ok": true}', "فقط JSON برگردان.")
            RUN["llm"][e.get("name", e["id"])] = "ok" if "ok" in out else "پاسخ غیرمنتظره"
        except LLMFail as f:
            RUN["llm"][e.get("name", e["id"])] = f"خطا ({f})"
    try:
        me = tg("getMe")
        RUN["telegram_bot"] = "@" + me.get("username", "?")
        chat = tg("getChat", data={"chat_id": cfg("telegram_chat_id", "TELEGRAM_CHAT_ID")})
        RUN["telegram_chat"] = chat.get("title") or chat.get("username") or "ok"
    except Exception as e:  # noqa: BLE001
        problem(f"تلگرام: {str(e)[:160]}")
    srcs = load_sources()[:3]
    for sc in srcs:
        try:
            n = len(parse_channel(httpx.get(f"https://t.me/s/{sc['id']}", headers={"User-Agent": UA}, timeout=25, follow_redirects=True).text, sc["id"]))
            RUN.setdefault("sources_sample", {})[sc["id"]] = n
        except Exception as e:  # noqa: BLE001
            problem(f"{sc['id']}: {str(e)[:100]}")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    need("GUN_API_URL", "GUN_BOT_TOKEN")
    CFG.update(api("/gun/config"))
    mask_secrets()
    if not CFG.get("enabled", True):
        print("disabled")
        return
    try:
        if mode == "check":
            do_check()
        else:
            if mode in ("all", "fetch"):
                do_fetch()
            if mode in ("all", "rewrite", "publish"):
                work = api("/gun/work")
                if mode in ("all", "rewrite"):
                    do_rewrite(work)
                    if mode == "all":
                        work = api("/gun/work")  # خبرهایی که همین الان بازنویسی و (خودکار) تأیید شدند هم در همین اجرا منتشر شوند
                if mode in ("all", "publish"):
                    do_publish(work)
    except Exception as e:  # noqa: BLE001 — متن خطا ممکن است نام کانال یا آدرس داشته باشد؛ فقط به سرور گزارش می‌شود
        problem(f"{e.__class__.__name__}: {str(e)[:200]}")
        print("failed:", e.__class__.__name__)
        sys.exit(1)
    finally:
        RUN["llm_usage"] = POOL_USAGE
        if not _env("GUN_DRY_RUN"):
            try:
                api("/gun/report", "POST", json={"mode": mode, "ok": not RUN["problems"], "summary": RUN})
            except Exception as e:  # noqa: BLE001
                print("report failed:", e.__class__.__name__)


if __name__ == "__main__":
    main()
