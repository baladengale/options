# Scoring Spec — Ticker Score & Contract Penalty

**Status**: Implemented & audited (2026-08-09)
**Files**: `src/scoring/screener_score.py`, `src/scoring/holding_score.py`, `src/filters/contract_filters.py`
**Config**: `config/rules.yaml → scoring`, `options`, `position_limits`

> **Convention**: ticker scores and contract scores are **1–10, lower = better** (1 = best candidate, 10 = avoid). This is the opposite of a 0–100 "higher is better" score. Every sub-score below is also 1–10.

---

## 1. The ticker score — `_compute_ticker_score()`

**File**: `src/scoring/screener_score.py:32`. Weighted sum across 5 dimensions. Weights from `config/rules.yaml → scoring.weights` (sum to 1.0):

| Dimension | Weight | Source function |
|-----------|:---:|-----------------|
| Technical | 0.25 | `_score_technical()` `:76` |
| Options ecosystem | 0.25 | `_score_options_eco()` `:119` |
| Fundamental | 0.15 | `_score_fundamental()` `:149` |
| External sentiment | 0.20 | `_score_external()` `:173` |
| Macro / risk | 0.15 | `_score_macro()` `:201` |

```
ticker_score = Σ (dimension_score_i × weight_i)         → clamped to [1, 10]
```

Each dimension is itself a 1–10 composite (sub-scores below). The final `contract_score = ticker_score + contract_penalty(...)` (§3).

---

## 2. Dimension sub-scores

### 2.1 Technical (`_score_technical`) — weight 0.25
```
technical = rsi×0.35 + trend×0.30 + adx×0.20 + vol×0.15
```

| Input | 1 (best) | 3 | 5 | 7 | 9 (worst) |
|-------|:---:|:---:|:---:|:---:|:---:|
| **RSI(14)** | 45–55 | 40–60 | 35–65 | 30–70 | else |
| **Trend alignment** | price>SMA50>SMA200 | price>SMA200 | price>SMA50 | price>SMA200 only | else |
| **ADX** | ≥40 | ≥25 | ≥20 | else (8) | — |
| **Volume ratio** (today/90d avg) | >1.0 | >0.7 | else (7) | — | — |

*RSI buckets*: neutral (45–55) is best for new premium entries — not overbought, not oversold. *Trend alignment*: stacked bullish MAs = strongest. *ADX*: ≥25 = trending (good for directional premium); <20 = ranging (worse for selling premium). *Volume ratio*: >1 = above-average attention.

### 2.2 Options ecosystem (`_score_options_eco`) — weight 0.25
```
options_eco = spread×0.25 + iv_rank×0.25 + cap×0.25 + beta×0.25
```

| Input | 1 | 3 | 5 | 7/8 | 9 |
|-------|:---:|:---:|:---:|:---:|:---:|
| **Bid-ask spread %** | <0.5% | <1% | <3% | <5%→7, else→9 | — |
| **IV Rank** | 30–70 | 20–80 | >80 | else→7 | — |
| **Market cap** | >$500B | >$100B | >$10B | else→8 | — |
| **Beta vs SPY** | <1.0 | <1.5 | <2.0 | else→9 | — |

*Spread*: tightest = most liquid = best. *IV Rank 30–70*: elevated enough to sell rich premium, not so extreme it signals an imminent event. *Cap*: larger = deeper chains. *Beta*: lower = less correlated blow-up risk; >2.0 = aggressive.

### 2.3 Fundamental (`_score_fundamental`) — weight 0.15
```
fundamental = pe×0.40 + div×0.30 + eps×0.30
```

| Input | 1 | 3 | 5 | 6/7/8 |
|-------|:---:|:---:|:---:|:---:|
| **P/E (TTM)** | 10–25 | 25–40 | 40–60 | >60→8, else→5 (incl. negative) |
| **Dividend yield** | >2% | >1% | >0% | else→6 |
| **EPS TTM** | >0 → 1 | — | — | ≤0 → 7 |

*P/E 10–25*: fairly-valued quality. *Negative P/E*: scores 5 here but a **separate eligibility check** (`is_wheel_eligible`) auto-skips loss-makers (`net_profit<0 AND eps_ttm<0`). Trusted tickers (AMD, PLTR, TSLA) skip the high-P/E flag in thesis validation but negative-P/E still blocks.

### 2.4 External sentiment (`_score_external`) — weight 0.20
```
external = clamp(4.0 + Δconsensus + Δtarget + Δearnings + Δinsider + Δnews, [1, 10])
```

| Adjustment | Condition | Δ |
|------------|-----------|:---:|
| Analyst consensus | STRONG_BUY | −1.5 |
|  | BUY | −0.8 |
|  | HOLD | +0.5 |
|  | SELL | +3.0 |
|  | STRONG_SELL | +5.0 |
| Target price upside | >15% | −1.0 |
|  | >5% | −0.5 |
|  | <−10% | +2.0 |
| Earnings blackout (≤14d) | true | +2.0 |
| Insider activity | BUYING | −1.0 |
|  | SELLING | +1.5 |
| News score (0–100) | ≥70 | −1.0 |
|  | ≤30 | +2.0 |
|  | ≤40 | +1.0 |

### 2.5 Macro / risk (`_score_macro`) — weight 0.15
```
macro = base(regime) + (2.0 if earnings_blackout else 0),  clamped [1,10]
  BULLISH→2, NEUTRAL→3, CAUTIOUS→4, VOLATILE→6, BEARISH→8, else→5
```

---

## 3. Contract penalty — `_contract_penalty()`

**File**: `src/scoring/screener_score.py:237`. Added to the ticker score to produce the per-contract `contract_score = ticker_score + penalty`. **Lower penalty = better contract.** All amounts from `cfg.contract_penalty(key)` (rules.yaml `scoring.contract_penalty`).

| Component | Condition | Δ to penalty |
|-----------|-----------|:---:|
| DTE | < `hard_block` (7) | **+99** (block) |
|  | < `weekly_max` (14) | +3.0 |
|  | < `penalty_start` (21) | +1.5 |
|  | < `optimal_min` (30) | +0.5 |
|  | ≤ `optimal_max` (45) | **−0.5** (bonus) |
|  | ≤ `long_start` (60) | 0 |
|  | > 60 | +1.0 |
| Open interest | < 100 | +1.5 |
|  | < `oi_min` (500) | +0.5 |
| Bid-ask spread | > 5% | +2.0 |
|  | > 2% | +1.0 |
| Delta | < 0.15 | +0.5 |
| RoC | > 24% | **−1.5** (bonus) |
|  | > 18% | −0.8 |
|  | > 15% | −0.3 |
| IV | > `high_iv_threshold` (35) | −0.5 (bonus — elevated premium) |
| Volume | < 50 | +1.0 |
|  | < `volume_min` (10) | +2.0 |

**Interpretation**: the DTE ladder encodes the research finding that 30–45 DTE is the theta/gamma sweet spot (`research_dte_selection.md`); <21 DTE is penalized (gamma cliff); <7 is blocked. The OI/spread/volume penalties enforce liquidity. RoC and IV bonuses reward rich premium. Negative deltas on RoC/IV are *bonuses* (they reduce the penalty → lower score → better rank).

The **star rating** (`_score_stars` `:326`) buckets the final contract score: ★★★★★ = 1–2 (excellent), down to ☆ = 8+.

---

## 4. Contract gates — `passes_all_gates()`

**File**: `src/filters/contract_filters.py:146`. **Hard gates** that exclude a contract entirely (not just penalize). Run before scoring, and again before execution.

| Gate | Function | Rule |
|------|----------|------|
| Liquidity | `passes_liquidity` | bid > 0, OI ≥ `oi_min` (500), volume ≥ `volume_min` (10) |
| Delta | `passes_delta` | within `delta_range(strategy, regime)`; CSP also `|Δ| ≤ 0.70` (deep-ITM = not premium selling) |
| IV sanity | `iv_sane` | `0 < IV < 500` |
| VRP | `passes_vrp` | `IV > HV(30d) × 0.8` (only sell when options priced above realized vol) |
| RoC | `passes_roc` | CSP ≥ 12%, CC ≥ 8% (annualized) |
| Concentration | `passes_concentration` | `capital ≤ net_liq × max_single_position_pct` |
| Cash buffer | `passes_cash_buffer` | cash ≥ 10% NLV; capital ≤ 80% BP; OR fits CSP headroom |

The orchestrator returns `(True, '')` on full pass or `(False, 'reason')` on first failure.

---

## 5. Holding score & option decisions — `holding_score.py`

For *existing* positions (not screening). Two outputs:

### 5.1 Holding score — `_score_holding()` `:27`
Scores a stock holding 1–10 from a neutral base of **5.0**, adjusted additively:
- RSI 45–55 → −1.0; outside 30–70 → +2.0
- Trend price>SMA50>SMA200 → −1.0; below SMA200 → +1.5
- Volume ratio >1.5 → −0.5; <0.5 → +0.5
- Analyst STRONG_BUY → −1.0; SELL → +2.0
- Earnings blackout → +1.5
- News ≥70 → −0.5; ≤30 → +1.0
- Regime BEARISH → +2.0; VOLATILE → +1.0

Clamped to [1, 10].

### 5.2 Option decision — `_score_option()` `:151`
The option-layer decision engine. Returns `(score, decision_string, ProfitDecision)`. Layered logic (each layer adds to the score and emits an emoji-tagged decision):

1. **Profit booking** (`:177`) — delegates to `decide_profit_target` (see [exit-and-profit-management-spec.md](exit-and-profit-management-spec.md)).
2. **OTM-only close gate** (`:189–213`) — overrides an auto-CLOSE to HOLD when the position is far OTM (`|Δ| < 0.30`) with ample DTE (`> 21`) — let theta work.
3. **Per-ticker frequency cap** (`:201`) — suppresses a profit-taking CLOSE once a ticker hits `max_closes_per_ticker_per_month` (2). Defensive closes (delta stop, MANAGE_DTE) still fire.
4. **DTE management** (`:233–252`) — ≤3 DTE → "Let expire/assign"; ≤7 → monitor; **21 DTE is the universal management point**.
5. **Delta gates** (`:262–279`) — CSP `|Δ| ≥ 0.60` → STOP (+2.0); CC ≥ 0.50 → high-assignment (+1.5); CSP ITM ≥ 0.50 (+1.0); inside the gamma zone (DTE ≤ 21) the decision delta relaxes to 0.40.
6. **Premium-multiple stops** (`:285–312`) — `profit_captured < 0`: DTE >30 → alert 2.0×/close 3.0×; 21–30 → 1.0×/2.0×; ≤21 → 0.5×/1.5× (+2.5 score at close).
7. **Earnings blackout** (`:315–319`) — +1.5, "close before".
8. **Heavy-loss catch-all** (`:322–348`) — `pl < −$1000` → thesis-aware: BROKEN → +0.5 "Exit Wheel"; DAMAGED → +1.0 "Monitor"; intact → +1.5 "Hold".

Decision strings: ✅ CLOSE · 🔄 ROLL · 🛑 STOP · 📅 MANAGE DTE · ⏸️ HOLD (capped) · 📉 UNDERWATER.

---

## 6. The negative-GEX CSP pause

`_compute_chain_gex()` (`screener_score.py:297`) computes a simplified chain GEX (see [formulas-reference.md](formulas-reference.md) §12). A **negative chain GEX below −500k** pauses new CSP candidates for that ticker — dealers short gamma amplify moves, so CSP assignment risk is elevated. This is a *directional* signal (the simplification preserves the sign).

---

## 7. Validation

- `tests/test_screener_scoring.py` — ticker scoring + contract penalty with **exact expected values** (table-driven).
- `tests/test_screener_score.py` — scoring engine integration.
- `tests/test_holding_score.py` — option CLOSE/HOLD/ROLL decisions with stop-loss scenarios.
- `tests/test_eligibility.py` — loss-maker auto-skip.
- `tests/test_overlap.py` — overlap detection.

All pass (audited 2026-08-09). Coverage: `screener_score` 84%, `holding_score` 80%, `contract_filters` 42% (gate-branch coverage; the orchestrator paths are exercised through the consumers).
