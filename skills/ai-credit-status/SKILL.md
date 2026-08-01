---
name: ai-credit-status
description: Shows whether Z.AI/GLM or DeepSeek is usable now, with peak-hour warning (SGT). Use when the user asks about AI credits, which provider to use, or peak hours.
---

Check AI provider status (Z.AI preferred, DeepSeek fallback). Crisp table output only (provider rows + Verdict):

```bash
python3 skills/ai-credit-status/scripts/ai_credit_status.py
```

Example output:

```
 AI Provider Status - Sat 01 Aug 2026 12:46 SGT
 Z.AI/GLM     🔴 OFF-PEAK (0.5x)      | rate limited (5h window)
 DeepSeek     🟢 OFF-PEAK (1x)        | 16.34 USD (fallback)
 Verdict: Z.AI rate limited (5h window) - fallback: DeepSeek has 16.34 USD. Z.AI resets in 1h 51m (Sat 14:38 SGT).
```

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