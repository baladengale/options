## Goal

Fix the SCARCE-override flaw identified in this conversation: when CSP redeployment is blocked (deployment % over the 25% limit — your current 65.5% state), the engine still forces 50% booking even though the freed capital has nowhere to go. Make SCARCE **deployment-aware** so trend-extended targets (70%/85%) become available for qualifying CSPs when you literally cannot open another CSP.

**Scope (per your answers):** Focused fix, deployment-limit-only trigger. Does NOT consolidate the 3 duplicated `_capital_scarcity` implementations, does NOT wire the orphan `should_pause_csp`, does NOT touch `AdaptiveProfitCalculator`.

---

## Design principle: backward-compatible by default

The new `csp_paused` kwarg defaults to `False` everywhere. This means:
- All **3 existing SCARCE tests** (`test_profit_management.py:114-146`) stay green unchanged — they never pass the kwarg, so behavior is identical.
- Every call site that doesn't opt in keeps current behavior.
- Only `portfolio.py --health` and `oie_engine.py` compute and pass the real deployment %.

---

## Changes (6 files)

### 1. `config/rules.yaml` — add the toggle (OFF by default)
Under `profit_take:`, add one key:
```yaml
profit_take:
  # ... existing keys ...
  bypass_scarce_when_csp_paused: false   # default OFF — preserves current behavior
```
Default `false` means the flaw stays "as-designed" unless you explicitly turn it on, consistent with your repo convention that profit-management changes are PROPOSED until validated. You flip this to `true` when you want the deployment-aware bypass active.

### 2. `src/config.py` — accessor (one-line addition near line 306)
The `profit_take()` accessor at line 306 already does `self._data.get('profit_take', {}).get(key, default)` — it works as-is for the new key. **No code change needed** in config.py; the existing accessor reads the new YAML key automatically. (I verified this — `cfg.profit_take('bypass_scarce_when_csp_paused', False)` returns the value or the default.)

### 3. `src/analysis/profit_management.py` — the core logic (GATE 2 modification)
Two changes:

**(a) Add `csp_paused: bool = False` kwarg** to `decide_profit_target` signature (line 65-72):
```python
def decide_profit_target(
    strategy: str,
    profit_captured: float,
    dte: int,
    delta: float,
    trend_ctx: Optional[TrendContext] = None,
    capital_scarcity: Optional[str] = None,
    csp_paused: bool = False,        # NEW
) -> ProfitDecision:
```

**(b) Modify GATE 2** (line 114-118) to respect the bypass:
```python
# ── GATE 2: capital scarcity (overrides trend extension unless CSP paused) ──
bypass_enabled = bool(cfg.profit_take('bypass_scarce_when_csp_paused', False))
if scarcity == scarcity_override and not (bypass_enabled and csp_paused):
    return _close_at_base(...)
```
When `bypass_enabled=True` AND `csp_paused=True`, GATE 2 is skipped → control falls through to the CSP/CC trend-extension branches below, where trend 72 + IVR 85 unlocks 85% as designed.

Add `csp_paused` to the `ProfitDecision` dataclass reason strings where relevant so the audit trail explains *why* the bypass fired (e.g., "Capital SCARCE but CSP deployment paused (bypass enabled) — applying trend extension").

### 4. `src/scoring/holding_score.py` — pass-through (line 151-153, 174)
Add `csp_paused: bool = False` to `_score_option` signature and forward it:
```python
def _score_option(pos, current, profit_captured, pl, today, yf_client,
                  trend_ctx=None, capital_scarcity=None, orders=None,
                  csp_paused: bool = False):   # NEW
    ...
    pd = decide_profit_target(strategy, profit_captured, dte, delta,
                              trend_ctx, capital_scarcity, csp_paused=csp_paused)   # forward
```

### 5. `scripts/portfolio.py` — compute + inject (line 518-543)
At `_score_options` (the injection point with `pf` + `nlv` in scope), compute deployment % once and pass it through:
```python
def _score_options(pf, snap_map, yf_client, today, trend_map=None, nlv=None, portfolio=None,
                    orders=None):
    trend_map = trend_map or {}
    scarcity = _capital_scarcity(portfolio, nlv) if portfolio and nlv else None
    # NEW — compute CSP deployment status for the SCARCE bypass
    max_csp_pct = get_config().max_csp_deployed_pct   # 0.25
    csp_dep = (pf.csp_liability / nlv) if (nlv and nlv > 0) else 0.0
    csp_paused = csp_dep > max_csp_pct
    ...
    score, dec, _pd = _score_option(pos, current, profit_captured, pos.get('pl', 0), today,
                                    yf_client, trend_ctx=tctx, capital_scarcity=scarcity,
                                    orders=orders, csp_paused=csp_paused)   # NEW kwarg
```
This mirrors the existing `csp_deployment_pct = pf.csp_liability / nlv` one-liner already used at `portfolio.py:598` — same pattern, no new data plumbing.

### 6. `scripts/oie_engine.py` — paper engine parity (line 408)
Same one-line addition at the paper-engine call site so the paper portfolio exercises the same logic:
```python
pdec = decide_profit_target(
    strategy, profit_captured, dte, abs(pos.get('current_delta', 0) or 0),
    tctx, capital_scarcity=self._capital_scarcity(),
    csp_paused=self._csp_paused(),   # NEW — compute from paper DB state
)
```
Add a small `_csp_paused()` helper method to `OIEEngine` (mirrors the existing `_capital_scarcity()` at line 739): computes paper CSP liability / paper net liq, compares to `cfg.max_csp_deployed_pct`.

---

## New tests (`tests/test_profit_management.py`)

Add 3 tests (existing 3 SCARCE tests stay unchanged — they don't pass `csp_paused` so they assert current behavior):

1. **`test_scarce_bypass_when_csp_paused_and_flag_on`** — SCARCE + strong trend + `csp_paused=True` + bypass flag enabled → target 85% (trend extension applies). This is the regression test for the flaw.
2. **`test_scarce_bypass_disabled_when_flag_off`** — SCARCE + strong trend + `csp_paused=True` + bypass flag OFF (default) → target 50% (current behavior preserved). Guards against accidentally flipping the default.
3. **`test_scarce_still_applies_when_csp_not_paused`** — SCARCE + strong trend + `csp_paused=False` + bypass enabled → target 50%. Confirms the bypass only fires when redeployment is actually blocked.

These use the existing monkeypatch pattern (`test_targets_read_from_config` at line 183 shows how to stub `cfg.profit_take(...)`).

---

## What stays untouched

- ❌ The 3 duplicated `_capital_scarcity` implementations (portfolio.py, oie_engine.py, adaptive_profit.py) — out of scope per your "focused fix" choice
- ❌ `Config.should_pause_csp()` orphan code — out of scope (you chose deployment-limit only, not macro triggers)
- ❌ `AdaptiveProfitCalculator` class — untouched
- ❌ All existing SCARCE tests — unchanged (default-`False` kwarg preserves behavior)
- ❌ The spec doc `specs/profit-loss-management-spec.md` — already documents this as the capital-scarcity gate; the fix is the implementation of an already-proposed refinement, not a new spec

---

## Validation

After implementation:
1. `pytest tests/test_profit_management.py -v` — all 28 existing + 3 new tests pass
2. `pytest tests/test_holding_score.py tests/test_profit_target_optimization.py -v` — no regressions in the wrapper-layer tests
3. Manual end-to-end: with `bypass_scarce_when_csp_paused: true` in rules.yaml, run `python3 scripts/portfolio.py --health` and confirm GOOG 310P now shows the 85% target (trend 90 + IVR ~30) instead of 50%, while TSLA 310P (trend 25) still shows 50% (trend too weak to extend regardless of SCARCE)
4. `pytest tests/ --cov=src --cov-report=term --cov-fail-under=85` — coverage gate holds

---

## Activation path

Because `bypass_scarce_when_csp_paused` defaults to `false`, this commit lands **inert** — no behavior changes for anyone until you flip the YAML flag to `true`. That respects your repo convention ("PROPOSED — UNVALIDATED, backtest pending"). Once you've backtested it (or once you decide to trust the logic for live use given your current CSP-paused state), flip the flag and the engine immediately starts letting winners ride when you can't redeploy.