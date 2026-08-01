#!/usr/bin/env python3
"""AI status: Z.AI/GLM preferred, DeepSeek fallback. Peak hours hardcoded (SGT)."""
from __future__ import annotations
import argparse, json, re, shlex, sys, urllib.error, urllib.request
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")
DS_BAL = "https://api.deepseek.com/user/balance"
ZAI_CHAT = "https://api.z.ai/api/anthropic/v1/messages"
ZAI_MODELS = "https://api.z.ai/api/paas/v4/models"
# Hardcoded peak hours (SGT/UTC+8)
DS_PEAK = ((9, 12), (14, 18))     # DeepSeek daily: 09-12 & 14-18, 2x price
ZAI_PEAK = ((14, 18),)            # Z.AI Mon-Fri: 14-18, 1.0x (0.5x off-peak)
EXH = ("insufficient balance", "no resource package")
RATE = ("usage limit reached", "rate limit", "ratelimit", "too many")
RESET_RE = re.compile(r"limit will reset at\s*:?\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)", re.I)


def load_cfg(path: Path) -> dict[str, str]:
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().strip("'\"")
        if not k:
            continue
        try:
            p = shlex.split(v)
            out[k] = p[0] if p else ""
        except ValueError:
            out[k] = v.strip().strip("'\"")
    return out


def http(url: str, key: str | None = None, payload: dict | None = None):
    h = {"Authorization": f"Bearer {key}"} if key else {}
    data = None
    if payload is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, {"_raw": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"_raw": body}
    except urllib.error.URLError as e:
        return None, {"_error": str(e.reason)}


def ds_balance(key: str) -> dict:
    st, data = http(DS_BAL, key)
    if st != 200 or not isinstance(data, dict) or "balance_infos" not in data:
        return {"ok": False}
    total, cur = 0.0, "USD"
    for i in data.get("balance_infos", []):
        try:
            total += float(i.get("total_balance", 0.0))
        except ValueError:
            continue
        cur = i.get("currency", cur)
    return {"ok": True, "balance": round(total, 2), "currency": cur}


def parse_reset(msg: str):
    m = RESET_RE.search(msg)
    if not m:
        return None
    ts = m.group(1).replace("T", " ")
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(ts, f).replace(tzinfo=SGT)
        except ValueError:
            continue
    return None


def candidates(c: dict[str, str]) -> list[str]:
    seen = []
    for k in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        raw = (c.get(k) or "").strip()
        m = re.sub(r"\[[^\]]*\]", "", raw).strip() if raw else ""
        if m and m not in seen:
            seen.append(m)
    if "glm-4.7" not in seen:
        seen.append("glm-4.7")
    return seen


def live_models(key: str) -> list[str]:
    st, data = http(ZAI_MODELS, key)
    if st != 200 or not isinstance(data, dict):
        return []
    return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]


def probe(key: str, model: str) -> dict:
    p = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
    st, data = http(ZAI_CHAT, key, p)
    if st == 200:
        return {"usable": True, "model": model}
    msg = ""
    if isinstance(data, dict):
        err = data.get("error", {})
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
    elif isinstance(data, str):
        msg = data
    low = msg.lower()
    if st == 429:
        if any(x in low for x in EXH):
            return {"usable": False, "reason": "quota used up (no balance)", "model": model}
        rt = parse_reset(msg)
        if rt or any(x in low for x in RATE):
            return {"usable": False, "reason": "rate limited (5h window)", "reset_at": rt, "model": model}
    return {"usable": False, "reason": f"status {st}", "model": model}


def zai_status(key: str, c: dict[str, str]) -> dict:
    for m in candidates(c) + live_models(key):
        r = probe(key, m)
        if not r.get("skip"):
            return r
    return {"usable": False, "reason": "no probe model"}


def in_peak(now: datetime, windows, weekday_only: bool) -> bool:
    if weekday_only and now.weekday() >= 5:
        return False
    t = now.time()
    return any(time(s, 0) <= t < time(e, 0) for s, e in windows)


def countdown(target, now) -> str:
    m = int((target - now).total_seconds() // 60)
    d, rem = divmod(m, 1440)
    h, mn = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {mn}m"
    if h:
        return f"{h}h {mn}m"
    return f"{mn}m"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-probe", action="store_true")
    a = ap.parse_args()

    dsc = load_cfg(Path.home() / ".balarc_deepseek")
    zc = load_cfg(Path.home() / ".balarc_zai")
    dk = dsc.get("DEEPSEEK_TOKEN") or dsc.get("ANTHROPIC_AUTH_TOKEN")
    zk = zc.get("ANTHROPIC_AUTH_TOKEN")

    now = datetime.now(SGT)
    dsp = in_peak(now, DS_PEAK, False)
    zp = in_peak(now, ZAI_PEAK, True)

    ds_ok, ds_cred = False, "no key"
    if dk:
        r = ds_balance(dk)
        if r["ok"]:
            ds_ok = True
            ds_cred = f"{r['balance']:.2f} {r['currency']}"
        else:
            ds_cred = "query failed"

    z = {"usable": False, "reason": "no key"}
    zm, zr = None, None
    if zk and not a.no_probe:
        z = zai_status(zk, zc)
        zm = z.get("model")
        zr = z.get("reset_at")

    z_peak_warn = "⚠️ DURING Z.AI PEAK HOURS (14:00-18:00 Mon-Fri, 1.0x)" if zp else ""
    ds_peak_warn = "⚠️ DURING DEEPSEEK PEAK (09-12 & 14-18 SGT, 2x price)" if dsp else ""

    if z.get("usable"):
        verdict = "Use Z.AI/GLM (preferred)." + (f" {z_peak_warn}" if zp else "")
    else:
        reason = z.get("reason", "unavailable")
        fallback = f"DeepSeek has {ds_cred}" if ds_ok else "DeepSeek also unavailable"
        verdict = f"Z.AI {reason} - fallback: {fallback}."
        if ds_peak_warn:
            verdict += f" {ds_peak_warn}"
        if zr:
            verdict += f" Z.AI resets in {countdown(zr, now)} ({zr:%a %H:%M} SGT)."

    if a.json:
        print(json.dumps({
            "checked_at": now.isoformat(timespec="seconds"),
            "zai": {"preferred": True, "usable": z.get("usable"), "reason": z.get("reason"),
                    "peak": zp, "peak_warning": z_peak_warn, "probe_model": zm,
                    "reset_at": zr.isoformat(timespec="seconds") if zr else None},
            "deepseek": {"fallback": True, "peak": dsp, "peak_warning": ds_peak_warn,
                         "credits_ok": ds_ok, "credits": ds_cred},
            "verdict": verdict,
        }, ensure_ascii=False, indent=2))
        return 0

    bar = "=" * 60
    print(bar)
    print(f" AI Provider Status - {now:%a %d %b %Y %H:%M} SGT")
    print(bar)
    good = "usable (preferred)" if z.get("usable") else z.get("reason")
    print(f" Z.AI/GLM     {'🟢' if z.get('usable') else '🔴'} {'PEAK (1.0x)' if zp else 'OFF-PEAK (0.5x)':<20} | {good}")
    print(f" DeepSeek     {'🟢' if ds_ok else '🔴'} {'PEAK (2x)' if dsp else 'OFF-PEAK (1x)':<20} | {ds_cred} (fallback)")
    print(bar)
    print(f" Verdict: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())