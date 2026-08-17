#!/usr/bin/env python3
"""AI status: Z.AI/GLM preferred, DeepSeek fallback. Peak hours hardcoded (SGT).

Also reports GLM Coding Plan quotas (5-hour, weekly, MCP) from docs.z.ai:
- Plan limits: Lite/Pro/Max/Team (individual credits-based plans, Jul 2026).
- Credits = (in*mult + cached*mult + out*mult) / 10_000; MCP calls * 1.2.
- GLM-4.7 = 1x all day; GLM-5.2/5-Turbo = 3x peak / 1x off-peak; off-peak 0.5x.

Z.AI exposes NO public usage API (console data requires a logged-in web session).
Usage is therefore seeded from ~/.balarc_zai keys (what the website shows):
  export GLM_PLAN=lite            # lite | pro | max | team-standard | team-premium
  export ZAI_5H_USED=1180         # credits used in current 5h window  (website: 5 Hours Quota)
  export ZAI_WEEKLY_USED=5900     # credits used this week             (website: Weekly Quota)
  export ZAI_MCP_USED=127         # MCP calls used this week           (website: MCP Quota)
  export ZAI_QUOTA_RESET_5H="2026-08-01 20:00"   # optional reset timestamps (SGT)
  export ZAI_QUOTA_RESET_WEEKLY="2026-08-03 00:00"
Local probe events (in ~/.zai_quota_state.json) are only used as a fallback
when the seed keys are absent.
"""
from __future__ import annotations
import argparse, json, os, re, shlex, sys, urllib.error, urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")
DS_BAL = "https://api.deepseek.com/user/balance"
ZAI_CHAT = "https://api.z.ai/api/anthropic/v1/messages"
ZAI_MODELS = "https://api.z.ai/api/paas/v4/models"
# Hardcoded peak hours (SGT/UTC+8)
DS_PEAK = ((9, 12), (14, 18))     # DeepSeek daily peak 09-12 & 14-18 (Beijing/SGT, UTC+8); time-of-day billing since 2026-08-17, off-peak = 50% of peak
ZAI_PEAK = ((14, 18),)            # Z.AI Mon-Fri: 14-18, 1.0x (0.5x off-peak)
EXH = ("insufficient balance", "no resource package")
RATE = ("usage limit reached", "rate limit", "ratelimit", "too many")
RESET_RE = re.compile(r"limit will reset at\s*:?\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)", re.I)

# --- GLM Coding Plan quotas (credits-based; per docs.z.ai devpack overview/teamplan) ---
PLAN_QUOTAS = {
    "lite":          {"5h": 2_000, "weekly": 10_000},
    "pro":           {"5h": 12_000, "weekly": 60_000},
    "max":           {"5h": 28_000, "weekly": 140_000},
    "team-standard": {"5h": 15_000, "weekly": 66_000},
    "team-premium":  {"5h": 35_000, "weekly": 155_000},
}
# Credit multiplier per model (docs: teamplan "Credit Calculation")
MODEL_MULT = {
    "glm-4.7":     {"in": 4.6, "cache": 1.2, "out": 16.0},
    "glm-5-turbo": {"in": 5.7, "cache": 1.5, "out": 21.0},
    "glm-5":       {"in": 6.9, "cache": 1.7, "out": 24.0},
    "glm-5.2":     {"in": 6.9, "cache": 1.7, "out": 24.0},
}
MODEL_PEAK_BOOST = {"glm-5": 3.0, "glm-5.2": 3.0, "glm-5-turbo": 3.0}  # 3x during peak; GLM-4.7 stays 1x
MCP_MULT = 1.2            # web search / web reader / zread: credits = calls * 1.2
OFF_PEAK_DISCOUNT = 0.5   # model usage charged at 50% off-peak
STATE_PATH = Path.home() / ".zai_quota_state.json"


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
    except (TimeoutError, OSError) as e:
        return None, {"_error": str(e)}


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


def parse_ts(s: str):
    """Parse 'YYYY-MM-DD HH:MM(:SS)' (+ optional tz) into an aware SGT datetime."""
    if not s:
        return None
    s = s.strip()
    dt = None
    # Already has a tz offset / Z
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    if dt is None:
        for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                dt = datetime.strptime(s, f)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    # Ensure tz-aware; naive input defaults to SGT
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SGT)
    return dt


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
        u = data.get("usage", {}) if isinstance(data, dict) else {}
        return {"usable": True, "model": model,
                "usage": {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
                          "cache": u.get("cache_read_input_tokens", 0)},
                "tools": u.get("server_tool_use", {}) if isinstance(u, dict) else {}}
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


# --- Quota local fallback (only used when seed keys are absent) ---

def _age(e: dict, now: datetime):
    try:
        return now - datetime.fromisoformat(e.get("ts", ""))
    except (ValueError, TypeError):
        return None


def pct(used: float, limit: float) -> float:
    return round(min(100.0, used / limit * 100), 1) if limit else 0.0


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"events": []}


def save_state(st: dict, now: datetime) -> None:
    st["events"] = [e for e in st.get("events", [])
                    if (d := _age(e, now)) is not None and d <= timedelta(days=8)]
    try:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def credits_for(model: str, usage: dict, peak: bool) -> float:
    m = MODEL_MULT.get(model) or MODEL_MULT["glm-4.7"]
    cr = (usage.get("in", 0) * m["in"] + usage.get("cache", 0) * m["cache"]
          + usage.get("out", 0) * m["out"]) / 10_000.0
    if MODEL_PEAK_BOOST.get(model) and peak:
        cr *= MODEL_PEAK_BOOST[model]
    if not peak:
        cr *= OFF_PEAK_DISCOUNT
    return cr


def record_event(st: dict, res: dict, peak: bool, now: datetime) -> None:
    if not res.get("usable") or not res.get("model"):
        return
    u = res.get("usage") or {}
    calls = sum(int(v) for v in (res.get("tools") or {}).values()
                if isinstance(v, (int, float)))
    st.setdefault("events", []).append({
        "ts": now.isoformat(timespec="seconds"),
        "model": res["model"],
        "in": u.get("in", 0), "out": u.get("out", 0), "cache": u.get("cache", 0),
        "mcp_calls": calls,
        "credits": round(credits_for(res["model"], u, peak), 4),
    })


def quota_usage(st: dict, now: datetime) -> dict:
    h5 = wk = 0.0
    for e in st.get("events", []):
        d = _age(e, now)
        if d is None or d.total_seconds() < 0:
            continue
        if d <= timedelta(hours=5):
            h5 += e.get("credits", 0.0)
        if d <= timedelta(days=7):
            wk += e.get("credits", 0.0)
    return {"5h": h5, "weekly": wk}


def mcp_usage(st: dict, now: datetime) -> int:
    total = 0
    for e in st.get("events", []):
        d = _age(e, now)
        if d is None or d.total_seconds() < 0 or d > timedelta(days=7):
            continue
        total += int(e.get("mcp_calls", 0))
    return total


def fnum(s: str):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-probe", action="store_true")
    a = ap.parse_args()

    dsc = load_cfg(Path.home() / ".balarc_deepseek")
    zc = load_cfg(Path.home() / ".balarc_zai")
    dk = dsc.get("DEEPSEEK_TOKEN") or dsc.get("ANTHROPIC_AUTH_TOKEN")
    zk = zc.get("ANTHROPIC_AUTH_TOKEN")
    plan = (zc.get("GLM_PLAN") or os.environ.get("GLM_PLAN") or "lite").strip().lower().replace(" ", "-")
    if plan in ("standard", "team", "team-standard-seat"):
        plan = "team-standard"
    if plan in ("premium", "team-premium-seat"):
        plan = "team-premium"

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

    z = {"usable": False, "reason": "probe skipped" if a.no_probe else "no key"}
    zm, zr = None, None
    if zk and not a.no_probe:
        z = zai_status(zk, zc)
        zm = z.get("model")
        zr = z.get("reset_at")

    pq = PLAN_QUOTAS.get(plan)

    # --- Quota usage: seeded from config (real website numbers) preferred over local tracking ---
    st = load_state()
    if z.get("usable") and z.get("usage"):
        record_event(st, z, zp, now)
        save_state(st, now)
    local = quota_usage(st, now)
    local_mcp = mcp_usage(st, now)

    def cfgval(*keys) -> str:
        for k in keys:
            v = (zc.get(k) or os.environ.get(k) or "").strip()
            if v:
                return v
        return ""

    u5 = fnum(cfgval("ZAI_5H_USED"))
    uw = fnum(cfgval("ZAI_WEEKLY_USED"))
    umc = fnum(cfgval("ZAI_MCP_USED"))
    r5 = parse_ts(cfgval("ZAI_QUOTA_RESET_5H"))
    rw = parse_ts(cfgval("ZAI_QUOTA_RESET_WEEKLY"))
    rmc = parse_ts(cfgval("ZAI_QUOTA_RESET_MCP"))

    seeded5 = u5 is not None
    seededw = uw is not None
    seededmcp = umc is not None
    u5 = u5 if seeded5 else local["5h"]
    uw = uw if seededw else local["weekly"]
    umc = umc if seededmcp else float(local_mcp)
    # Z.AI probe 429 with reset_at is the authoritative 5h limit
    rate_limited = (not z.get("usable")) and z.get("reason", "").startswith("rate limited")
    if rate_limited and pq:
        u5 = pq["5h"]  # window exhausted -> 100%
        seeded5 = True

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
        zquota = None
        if pq:
            zquota = {
                "5h": {"used": round(u5, 2), "limit": pq["5h"], "pct": pct(u5, pq["5h"]),
                       "source": "config (website)" if seeded5 else "local tracking",
                       "reset_at": (zr or r5).isoformat(timespec="seconds") if (zr or r5) else None},
                "weekly": {"used": round(uw, 2), "limit": pq["weekly"], "pct": pct(uw, pq["weekly"]),
                           "source": "config (website)" if seededw else "local tracking",
                           "reset_at": rw.isoformat(timespec="seconds") if rw else None},
                "mcp": {"calls": int(umc),
                        "source": "config (website)" if seededmcp else "local tracking",
                        "credit_per_call": MCP_MULT,
                        "reset_at": rmc.isoformat(timespec="seconds") if rmc else None},
            }
        print(json.dumps({
            "checked_at": now.isoformat(timespec="seconds"),
            "zai": {"preferred": True, "usable": z.get("usable"), "reason": z.get("reason"),
                    "peak": zp, "peak_warning": z_peak_warn, "probe_model": zm,
                    "reset_at": zr.isoformat(timespec="seconds") if zr else None,
                    "plan": plan or None, "quotas": zquota},
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
    if pq:
        rst_target = zr or r5  # probe 429 reset_at is authoritative; else config
        rst = f"· resets in {countdown(rst_target, now)}" if rst_target else "· 5h rolling"
        src = "website" if seeded5 else "local"
        print(f"   ↳ Quota 5h:  {u5:,.1f}/{pq['5h']:,} cr ({pct(u5, pq['5h']):.1f}%) {rst} [{src}]")
        rwt = f"· resets in {countdown(rw, now)}" if rw else "· 7d rolling"
        src = "website" if seededw else "local"
        print(f"   ↳ Weekly:    {uw:,.1f}/{pq['weekly']:,} cr ({pct(uw, pq['weekly']):.1f}%) {rwt} [{src}]")
        rmt = f"· resets in {countdown(rmc, now)}" if rmc else ""
        src = "website" if seededmcp else "local"
        mcp_cred = umc * MCP_MULT
        print(f"   ↳ MCP:       {umc:,.0f} calls (~{mcp_cred:,.1f} cr · {MCP_MULT} cr/call) {rmt} [{src}]")
        if not (seeded5 and seededw and seededmcp):
            missing = []
            if not seeded5:
                missing.append("ZAI_5H_USED")
            if not seededw:
                missing.append("ZAI_WEEKLY_USED")
            if not seededmcp:
                missing.append("ZAI_MCP_USED")
            print(f"   ↳ Hint:      add {' '.join(missing)} to ~/.balarc_zai to show real website usage (pasted from z.ai console)")
    elif plan:
        print(f"   ↳ Quotas: unknown plan '{plan}' (try lite | pro | max | team-standard | team-premium)")
    print(f" DeepSeek     {'🟢' if ds_ok else '🔴'} {'PEAK (2x)' if dsp else 'OFF-PEAK (1x)':<20} | {ds_cred} (fallback)")
    print(bar)
    print(f" Verdict: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())