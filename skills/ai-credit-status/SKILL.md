---
name: ai-credit-status
description: Shows whether Z.AI/GLM or DeepSeek is usable now, with peak-hour warning (SGT) and GLM Coding Plan quota usage (5h / weekly / MCP). Use when the user asks about AI credits, which provider to use, peak hours, or quota remaining.
---

Check AI provider status (Z.AI preferred, DeepSeek fallback). Crisp table output only (provider rows + quota rows + Verdict):

```bash
python3 skills/ai-credit-status/scripts/ai_credit_status.py
```

Example output:

```
 AI Provider Status - Sat 01 Aug 2026 17:50 SGT
 Z.AI/GLM     🔴 OFF-PEAK (0.5x)      | rate limited (5h window)
   ↳ Quota 5h:  2,000.0/2,000 cr (100.0%) · resets in 1h 41m [website]
   ↳ Weekly:    5,900.0/10,000 cr (59.0%) · 7d rolling [website]
   ↳ MCP:       127 calls (~152.4 cr · 1.2 cr/call)  [website]
 DeepSeek     🟢 PEAK (2x)            | 16.24 USD (fallback)
 Verdict: Z.AI rate limited (5h window) - fallback: DeepSeek has 16.24 USD. Z.AI resets in 1h 41m (Sat 19:31 SGT).
```

If Z.AI is rate limited: `Z.AI rate limited (5h window)` and 5h quota shows 100% + reset countdown.
Plan defaults to `lite`; override with `GLM_PLAN` env or config when needed.

Full JSON (all details, including probe model, reset time, peak warnings) when machine-readable output is needed:

```bash
python3 skills/ai-credit-status/scripts/ai_credit_status.py --json
```

## Rule

1. Z.AI/GLM always preferred - status confirmed live by a 1-token test message (model from ~/.balarc_zai, suffix stripped, then live /models list).
2. If Z.AI rate limited / out of balance / error -> DeepSeek fallback.
3. Peak hours hardcoded (SGT) shown as warning when active:
   - DeepSeek: PEAK 09-12 & 14-18 daily (2x) / OFF (1x)
   - Z.AI/GLM: PEAK 14-18 Mon-Fri (1.0x) / OFF (0.5x) + rolling 5h limit

On 5h limit Z.AI returns "limit will reset at <ts>"; script parses and shows countdown.

Use --no-probe to skip the test message.

## Quotas (GLM Coding Plan)

Z.AI exposes **no public usage API** — the quota numbers on z.ai console require a logged-in web session. So usage is **seeded from ~/.balarc_zai** by pasting what the website shows (z.ai/manage-apikey/coding-plan/personal/my-plan):

```bash
export GLM_PLAN=lite            # lite | pro | max | team-standard | team-premium (default lite)
export ZAI_5H_USED=1180         # credits used in current 5h window  (website: "5 Hours Quota")
export ZAI_WEEKLY_USED=5900     # credits used this week             (website: "Weekly Quota")
export ZAI_MCP_USED=127         # MCP calls used this week           (website: "MCP Quota")
export ZAI_QUOTA_RESET_5H="2026-08-01 20:00"    # optional reset timestamps (SGT)
export ZAI_QUOTA_RESET_WEEKLY="2026-08-03 00:00"
```

If a seed key is absent, local probe tracking (~/.zai_quota_state.json, 5h/7d rolling windows) is used as fallback. The live probe is authoritative for the 5h status: when Z.AI returns 429, the 5h row shows 100% and uses the server's `reset_at`.

Plan limits (credits, per docs.z.ai devpack/overview + teamplan, Jul 2026):

| Plan          | 5h    | Weekly |
| ------------- | ----- | ------ |
| Lite          | 2,000 | 10,000 |
| Pro           | 12,000| 60,000 |
| Max           | 28,000| 140,000|
| Team Standard | 15,000| 66,000 |
| Team Premium  | 35,000| 155,000|

- Model credits = (in×mult + cached×mult + out×mult) / 10,000
- MCP credits = calls × 1.2 (Web Search / Web Reader / Zread)
- GLM-4.7 = 1× all day; GLM-5.x = 3× peak / 1× off-peak; off-peak model usage 0.5×
- The `[website]`/`[local]` suffix on each quota row shows the data source.
