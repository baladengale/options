# Production Deployment — Challenges, Gaps, Go/No-Go

**Status**: Assessment (2026-08-09). This doc evaluates the framework's readiness for live capital and names the concrete gaps to close before trusting it with real money.
**Audited state**: 701 tests pass (0 fail, 17 skipped needing live data); pure-logic modules well covered.

> The framework is a **paper-trading engine with read-only real-account mirroring**. "Production deployment" here means: *is it safe to let this engine's recommendations drive real manual trades, and what would it take to trust an auto-execution layer?* No script today submits real orders — that boundary is the single biggest safety property and must be preserved.

---

## 1. What's solid (production-ready)

| Property | Evidence |
|----------|----------|
| **Read-only on the real account** | `moomoo_client.py` wraps only `OpenQuoteContext`; `portfolio_loader.py` reads but never places orders. `grep -rn "place_order\|TrdEnv.SIMULATE" src/ scripts/ skills/` → nothing executable. |
| **Layered architecture, no cross-script imports** | `grep -rn "from scripts\." scripts/` → nothing. Enforced. |
| **Single source of truth for thresholds** | Every threshold in `config/rules.yaml` → `src/config.py`. No hardcoded values in scripts/analysis. |
| **Deterministic decision core** | `decide_exit_action()` is a pure function; 701 tests pin its behavior. AI is narrative-only. |
| **Two-layer guardrails** | Per-trade BLOCK/WARN + staged recovery; coverage check; roll discipline; thesis-break gates. |
| **Paper DB audit trail** | `paper_trades` logs every event with `cash_change`; cash is derived (`seeded_cash + Σ cash_change`), never stale. |
| **Resilient data layer** | moomoo → yfinance fallback; option-chain retry on `ret=-1`; thesis gates treat missing data as NO_DATA (never fabricated). |
| **Deployment artifacts** | macOS LaunchAgent + systemd unit + crash-recovery wrapper; structured logging. |
| **Formula correctness** | IV Rank, RSI, Black-Scholes (spec), RoC, max pain, beta all match authoritative sources (see [formulas-reference.md](formulas-reference.md)). |

---

## 2. What's a gap (must-address before live capital)

### Gap A — **No backtest harness** 🔴 (critical)
The engine runs on **live snapshots only**. You cannot replay 2008, 2020, or 2022 through it. Every rule marked `PROPOSED / UNVALIDATED` in `config/rules.yaml` (BULLISH position_mult 0.80, `bypass_scarce_when_csp_paused`, the OTM-only close gate, the thesis-break gates) is paper-trading on faith.

**The risk**: the win-rate illusion ([playbook §1](loss-management-playbook.md)) — a 96% win rate is structural for short premium, not evidence the system works. The rare losses decide everything, and you can't see them without a backtest.

**The fix path**: `specs/research_backtesting_architecture.md` designs the harness (Black-Scholes simulation → ORATS historical data). Phase 1 (BS + flat vol) is ~2 weeks; Phase 3 (paid data) is the production-grade answer. **Until this exists, treat every "PROPOSED" rule as research, not policy.**

### Gap B — **Margin rule is WARN, not BLOCK, in the loop** 🟡 (important)
The config has `max_margin_pct: 0.30` (GOAL.md #4: never prefer margin, 30% max, 15-day clear). But:
- `GuardrailChecker.check()` issues margin as a **WARN**, not BLOCK.
- The OIE engine calls `check_new_trade` but does **not** call `validate_margin_for_new_csp` before CSP execution.
- Live `margin_used_pct` is fetched from moomoo but not consistently threaded into the engine.
- `oie_engine.py:523` historically hardcoded `buying_power = cash * 2`.

**The risk**: a CSP-heavy paper book can implicitly borrow against stock collateral without tripping the 30% rule. In a correlated downturn this is exactly the "hidden leverage" the playbook warns about (§2: cash-secured is full-notional equity exposure deferred).

**The fix**: `specs/margin-guardrail.md` is the complete implementation plan — `compute_margin_headroom`, `compute_csp_expiry_concentration`, `validate_margin_for_new_csp`, upgrade margin WARN→BLOCK, wire into the OIE loop. ~1-2 days of work, medium risk (touches the hot path), reversible. **Do this before trusting CSP-deployment limits on real capital.**

### Gap C — **GEX simplification** 🟡 (context-dependent)
`_compute_gex` uses `gamma × OI × spot × 100` with naive signs (calls +, puts −), omitting the `spot² × 0.5` dealer-position factor (see [formulas-reference.md](formulas-reference.md) §12).

**The risk**: low for current use — the engine uses GEX only as a directional regime signal (negative chain GEX < −500k pauses new CSPs), and the simplification preserves the sign. But the *magnitude* is not comparable to SpotGamma/TradingFlow, so anyone reading the GEX number as an absolute will be misled.

**The fix**: either (a) document it loudly (done in formulas-reference), or (b) implement the full `gamma × OI × 100 × spot² × 0.5` with an explicit dealer-position assumption. (b) is cheap; do it when you next touch `compute.py`.

### Gap D — **ADX returns DX, not smoothed ADX** 🟢 (minor)
`compute_adx` returns the un-smoothed directional index, not the fully Wilder-smoothed ADX (see [formulas-reference.md](formulas-reference.md) §6).

**The risk**: immaterial at current use — ADX is bucketed into 40/25/20 thresholds, which are wide enough to absorb the noise. A tight-threshold use would expose it.

**The fix**: implement recursive Wilder smoothing when you build the backtest harness (research-grade).

### Gap E — **Decision-review script untested + requires live data** 🟡 (important)
`scripts/decision_review.py` (731 lines, the evidence source for [profit-target-optimization.md](profit-target-optimization.md)) has **no test file** and pulls live order history + yfinance prices. Its verdicts drove the §6/§7 spec changes.

**The risk**: the spec's "14 contracts HURT by premature close" finding is the empirical basis for the OTM-only close gate. If the script has a bug, the gate may be solving a non-problem (or the wrong one).

**The fix**: add `tests/test_decision_review.py` with mocked order history + prices; reproduce the §3.2 numbers. Until then, treat the OTM gate as a reasonable-but-unverified behavioral guardrail.

### Gap F — **No auto-execution layer (by design — but the boundary must hold)** 🟢 (safety)
The framework's strongest property is that **no script submits real orders**. If/when an auto-executor is added (the spec's "P6 out of scope"), the entire safety model changes:
- Every `PROPOSED` rule becomes load-bearing.
- The collar check (`collar_check.py`) must be called and `all_clear` enforced before every order.
- The structured `ProfitDecision` must be threaded out of `_score_option` (currently collapsed to a string — see [profit-target-optimization.md](profit-target-optimization.md) §5).
- Moomoo paper (`TrdEnv.SIMULATE`) is the bridge; `research_backtesting_architecture.md §8` sketches it.

**Recommendation**: do not add auto-execution until Gaps A and B are closed and a backtest validates the rule set.

---

## 3. Deployment challenges (operational)

| Challenge | Status | Mitigation |
|-----------|--------|------------|
| **OpenD availability** | The whole engine stops if OpenD is down. | Wrapper `run_oie.sh` launches OpenD via bundle ID, waits 90s, launchd `KeepAlive` restarts. Engine sleeps 60s when markets closed. |
| **moomoo rate limits** | ~3 calls/sec throttle in `moomoo_client.py`. | Batched calls (400 tickers); chain-code caching; price-history caching per session. |
| **HKD→USD FX** | Account is multi-currency; naïve ÷7.8 drifts. | Live yfinance `HKD=X`, banded 7.5–8.2, fallback 7.8, cached per process. |
| **moomoo 30-day chain-code API limit** | Chains chunked in 29-day windows. | `get_option_chain_codes` chunks + retries 3× on `ret=-1`. |
| **yfinance fallback divergence** | yfinance fundamentals ≠ moomoo snapshot fields. | `--no-external` mode skips thesis deep-checks; missing data → NO_DATA, never fabricated. |
| **Stale data** | No real-account DB means every run is fresh — but a hung OpenD returns zeros. | `fetch_live_portfolio` has a hardcoded safe-fallback tuple; callers should detect zeros. (Worth a freshness assertion.) |
| **macOS sleep / lid-close** | LaunchAgent pauses on sleep. | Run on a desktop Mini or prevent sleep; the engine catches up on wake. |
| **Singapore time zone** | User is SGT; US market hours are SGT 21:30–04:00 (or 22:30–05:00 DST). | `is_market_open()` uses ET; the LaunchAgent runs continuously and sleeps through closed hours. |
| **Secrets** | `config/email.yaml`, `.env.sh` carry SMTP creds / API keys. | Both gitignored. `email.yaml.example` is the template. Never commit. |

---

## 4. Go / No-Go gates for live capital

Before sizing up real trades based on this engine's recommendations, the following should be true. Each is independently load-bearing.

### Must-pass (🔴 blocks live capital)
1. **Backtest harness exists** and reproduces the expected improvement on the "PROPOSED" rules (Gap A). Until then, only the *unconditional* rules (50% profit-take, 21-DTE floor, net-credit-only rolling, the delta gates) are validated — and only by their source research, not by your own data.
2. **Margin rule upgraded to BLOCK** and wired into the OIE loop (Gap B). `margin-guardrail.md` is the plan.
3. **`test_decision_review.py` exists** and reproduces the §3.2 numbers (Gap E). The OTM-only close gate is otherwise unverified.
4. **Full test suite green on a machine with OpenD up** — including the 17 currently-skipped live-data tests.

### Should-pass (🟡 before sizing up)
5. **GEX either fixed or loudly documented** at the consumption point (Gap C) — so no one mistakes the magnitude for a SpotGamma number.
6. **A live margin/freshness assertion** — abort if moomoo returns zeros or `margin_used_pct` is missing.
7. **Paper OIE has run ≥ 3 months** end-to-end through at least one regime change, with the digest archived, and the decisions reviewed against hindsight.

### Nice-to-have (🟢 for hardening)
8. ADX recursive smoothing (Gap D).
9. Structured `ProfitDecision` threaded out of `_score_option` (prepares for any future executor).
10. CSP-expiry-concentration check wired into the loop (part of Gap B's spec).

---

## 5. Robustness verdict by dimension

| Dimension | Verdict | Why |
|-----------|---------|-----|
| **Correctness of core math** | ✅ Solid | Formulas match sources; 701 tests pin behavior. |
| **Safety boundary (read-only)** | ✅ Solid | No order-submission surface; grep-verifiable. |
| **Loss-side rules** | ✅ Solid | Layered delta + premium + absolute stops; thesis-break exits; price backstops. Mature. |
| **Profit-side rules (trend extension)** | ⚠ Unvalidated | Correct *logic*, but the trend thresholds (70/85%, trend_composite ≥ 50/70) are not backtested on your universe. Paper-trade first. |
| **Margin enforcement** | ⚠ Gap | WARN not BLOCK; not fully wired. Fix per `margin-guardrail.md`. |
| **Backtesting** | 🔴 Missing | The biggest gap. No way to replay history. |
| **Regime coverage** | ✅ Good | 5 regimes with adapted sizing/delta/mix; the table is sound. Extremes need human judgment (see [regime-playbook.md](regime-playbook.md)). |
| **Operational resilience** | ✅ Good | Fallbacks, retries, caching, LaunchAgent crash recovery. Freshness assertion would help. |
| **Audit trail** | ✅ Solid | `paper_trades` is a complete cash-change log; snapshots over time. |
| **Deployment artifacts** | ✅ Solid | launchd + systemd + wrapper; structured logs. |

---

## 6. The honest summary

This is a **well-engineered paper-trading framework with a sound deterministic core**, not a production auto-trader. For its intended purpose — *screening, scoring, and paper-validating a disciplined wheel strategy* — it is production-ready today. The formulas are correct, the guardrails are real, the test suite is green, and the read-only boundary is enforceable.

For **live capital**, the framework is ready to *inform manual trades* on the unconditional rules (50% profit-take, 21-DTE floor, net-credit-only rolls, the delta gates, thesis-break exits, position limits). It is **not** ready to trust the trend-extension rules, the margin loop, or any auto-execution until the backtest harness (Gap A) and the margin upgrade (Gap B) land.

**The single highest-leverage next step is the backtest harness.** Everything else is incremental hardening; the backtest is what converts "PROPOSED" to "POLICY."

---

### References
- [architecture-spec.md](architecture-spec.md) — the actual system
- [formulas-reference.md](formulas-reference.md) — formula validation
- [margin-guardrail.md](margin-guardrail.md) — the Gap B implementation plan
- [research_backtesting_architecture.md](research_backtesting_architecture.md) — the Gap A design
- [loss-management-playbook.md](loss-management-playbook.md) — the evidence base
- [regime-playbook.md](regime-playbook.md) — bull/bear/volatile/stagnant analysis
