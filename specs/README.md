# Specs — Options Wheel Framework

This folder is the **authoritative reference** for how the framework actually works. Every spec describes implemented behavior with exact `file:line` references, and every formula carries its derivation, authoritative source, and a validation note. Specs that propose changes are marked `PROPOSED / UNVALIDATED`.

> **Relationship to code**: When a spec and the code disagree, **the code is the source of truth** and the spec is stale. Open an issue / update the spec. The reverse is never true — never "fix" the code to match a stale spec without a backtest.

## Document map

### Reference specs (describe what's implemented)
| Document | Subject |
|----------|---------|
| [architecture-spec.md](architecture-spec.md) | The layered, **read-only + paper-DB** architecture. Replaces the older DB-centric design. |
| [formulas-reference.md](formulas-reference.md) | Every formula (Black-Scholes, IV Rank, RSI, MACD, ADX, HV, GEX, RoC, max pain, skew) with derivation, source, and validation. |
| [scoring-spec.md](scoring-spec.md) | The 5-dimension ticker score (1–10) + contract penalty, with exact sub-score tables. |
| [exit-and-profit-management-spec.md](exit-and-profit-management-spec.md) | The single exit decision core — trend-modulated profit targets, loss stops, rolling discipline. |
| [guardrails-and-risk-spec.md](guardrails-and-risk-spec.md) | Two-layer guardrails, collar/coverage check, holdings-exit framework, margin model. |
| [oie-paper-engine-spec.md](oie-paper-engine-spec.md) | OIE cycle phases, paper DB schema, wheel rotation. |

### Analysis specs (think-it-through docs)
| Document | Subject |
|----------|---------|
| [regime-playbook.md](regime-playbook.md) | How the engine behaves across **bull / bear / volatile / stagnant** markets; per-regime strengths + gaps. |
| [production-deployment.md](production-deployment.md) | Deployment challenges, known gaps (GEX, backtest, live margin), go/no-go gates for live capital. |

### Original PROPOSED design specs (read for rationale/evidence; current state is in the reference specs above)
| Document | Subject |
|----------|---------|
| [profit-loss-management-spec.md](profit-loss-management-spec.md) | Original trend-modulated profit-booking design. **Implemented** — see exit-and-profit-management-spec.md for current state. |
| [profit-target-optimization.md](profit-target-optimization.md) | Original OTM-close-gate + frequency-cap design + 60-day decision-review evidence. **Implemented** — backtest sign-off still pending. |
| [margin-guardrail.md](margin-guardrail.md) | The 30%-margin enforcement **plan**. **Partially implemented** (WARN not BLOCK; not wired into loop) — see production-deployment.md Gap B. |
| [position_sizing_standard.md](position_sizing_standard.md) | Original sizing standard. **Partially superseded** by config + guardrails-and-risk-spec.md; keep for the sizing math, recovery rules, and account-size table. Script references corrected to real scripts. |

### Evidence bases (research that informed the rules)
| Document | Subject |
|----------|---------|
| [loss-management-playbook.md](loss-management-playbook.md) | 64 cited sources on wheel losses, assignment, drawdowns, Singapore tax/estate notes. |
| [research_dte_selection.md](research_dte_selection.md) | Why 30–45 DTE entry / 21 DTE exit / 50% profit-take (Tastytrade, Option Alpha, ApexVol). |
| [research_backtesting_architecture.md](research_backtesting_architecture.md) | Design for the (not-yet-built) backtest harness — references future scripts (`ingest_history.py`, `backtest_runner.py`) by design. |

### Superseded
- `SPECS.md` *(removed 2026-08-09)* — the original v2 spec described a DB-centric architecture (`src/db/`, `src/signals/`, `src/trade/`, a `SyncEngine`, SQLite tables like `signals_log`/`daily_digest`/`price_history`) that was **never implemented**. The real system is read-only with only `db/oie_paper.db`. That design is retained in `research_backtesting_architecture.md` as a future-harness reference; the current-state architecture lives in `architecture-spec.md`.

## Validation status (audited)

| Area | Status |
|------|--------|
| Test suite | **701 passed, 0 failed, 17 skipped** (skips need live moomoo/yfinance) |
| Pure-logic coverage | `profit_management` 93%, `exit_management`/`trend` 89%, `credit_spread` 90%, `collar_check`/`holdings_exit` 100% |
| Formula correctness | IV Rank, RSI (Wilder), Black-Scholes Greeks, RoC annualization all match authoritative sources |
| Known simplifications | GEX omits `spot²` and dealer-position `0.5` factor (see [formulas-reference.md](formulas-reference.md) §GEX) |
| Not yet implemented | Backtest harness; live-margin enforcement in the OIE loop (designed, not wired) |

*Last audit: 2026-08-09.*
