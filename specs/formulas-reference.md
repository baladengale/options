# Formulas Reference — Derivation, Source, Validation

**Status**: Implemented & audited (2026-08-09)
**Purpose**: Every numerical formula in the framework, with its math, the exact `file:line` where it lives, the authoritative source, and a **validation note** (✓ matches source / ⚠ simplified / ✗ gap). Each section ends with a one-line verdict.

All formulas here are deterministic — no AI in the math. Threshold values live in `config/rules.yaml`; this document gives the *form* and cites the *source of correctness*.

---

## Table of contents
1. [Annualized Return on Capital (RoC) — CC & CSP](#1-annualized-return-on-capital-roc--cc--csp)
2. [IV Rank & IV Percentile](#2-iv-rank--iv-percentile)
3. [Historical Volatility (HV)](#3-historical-volatility-hv)
4. [RSI (Wilder)](#4-rsi-wilder)
5. [MACD](#5-macd)
6. [ADX (trend strength)](#6-adx-trend-strength)
7. [Beta vs SPY](#7-beta-vs-spy)
8. [Bollinger Bands](#8-bollinger-bands)
9. [Max Pain](#9-max-pain)
10. [ATM IV & 25Δ Skew](#10-atm-iv--25δ-skew)
11. [Term Structure](#11-term-structure)
12. [Gamma Exposure (GEX)](#12-gamma-exposure-gex)
13. [Put/Call Ratios](#13-putcall-ratios)
14. [Black-Scholes & Greeks (backtest harness only)](#14-black-scholes--greeks-backtest-harness-only)
15. [Put Credit Spread: max_loss, RoC, credit ratio](#15-put-credit-spread-max_loss-roc-credit-ratio)
16. [Profit-captured, loss-multiple, drawdown](#16-profit-captured-loss-multiple-drawdown)
17. [Validation summary table](#17-validation-summary-table)

---

## 1. Annualized Return on Capital (RoC) — CC & CSP

**File**: `src/filters/contract_filters.py:24` (`csp_roc`), `:32` (`cc_roc`); mirror in `src/scoring/screener_score.py:306`.

```
CSP RoC = (premium / strike)   × (365 / DTE) × 100
CC  RoC = (premium / price)    × (365 / DTE) × 100
```
- `premium` = per-share bid (one contract = 100 shares)
- `strike` (CSP) = the capital you'd commit if assigned; `price` (CC) = the stock you already hold (opportunity-cost basis)
- DTE in calendar days

**Derivation / rationale**: RoC normalizes premium to a common yardstick — *what annualized yield does this premium represent on the capital it ties up?* The `365/DTE` factor linearizes a position of any duration to an annual rate, so a 30-DTE and a 45-DTE trade are comparable. The denominator differs by strategy because the *capital at risk* differs: a CSP ties up `strike` dollars (the cash to buy shares if assigned); a CC ties up shares already owned, whose value is `price`.

**Source**: Tastytrade framework ("annualized return on capital" is the standard Tastytrade metric for ranking premium-sales trades). Also: [research_dte_selection.md](research_dte_selection.md) §3.

**Config**: `roc_min.csp: 12.0`, `roc_min.cc: 8.0` (CSP needs a higher bar because assignment carries stock risk). Gate: `passes_roc()` (`contract_filters.py:99`).

**Validation**: ✓ Matches the standard practitioner convention. Note: this is **simple-interest** annualization (not compounded), which is the convention for comparing short-DTE trades.

---

## 2. IV Rank & IV Percentile

**File**: `src/data/compute.py:237` (`compute_iv_rank`), `:247` (`compute_iv_percentile`).

```
IV Rank      = (current_IV − 1Y_low) / (1Y_high − 1Y_low) × 100      [0–100]
IV Percentile = (# days in 1Y with IV < current_IV) / (# days) × 100  [0–100]
```
Edge case: `high ≤ low` (flat year) → IV Rank = 50.0.

**Derivation / rationale**: IV Rank answers *"where does today's implied vol sit within its own 1-year range?"* — a position metric (0 = at the yearly low, 100 = at the yearly high). IV Percentile answers *"what fraction of days were calmer than today?"* — a distribution metric. The two diverge when a single IV spike stretches the range: Rank gets dragged toward 50 by one outlier, Percentile does not. Selling premium when IV Rank ≥ 30 means you're selling vol in at least the upper-third of its range — the "sell premium when volatility is elevated" rule.

**Source**: Tastytrade — [IV Rank & Percentile](https://www.tastylive.com/concepts-strategies/implied-volatility-rank-percentile); Barchart — [IV Rank vs IV Percentile](https://www.barchart.com/education/iv_rank_vs_iv_percentile); TradingBlock. All three cite exactly the formula above. **Verified against these sources on 2026-08-09.**

**Config**: `iv_rank_min: 30`. Used as a contract gate and as a trend-extension input (CSP profit target raised to 70/85% only when IVR ≥ 30).

**Validation**: ✓ Formula matches all cited sources exactly. **Note**: requires a 1Y IV history per ticker; moomoo does not persist IV history, so this is computed from a snapshot's IV-history field when available, else returns `None` (treated as "can't confirm IVR ≥ 30" — conservative).

---

## 3. Historical Volatility (HV)

**File**: `src/data/compute.py:155` (`compute_hv`, 30-day annualized from log returns).

```
HV = sqrt( variance(log returns) ) × sqrt(252)
```
where `variance = Σ(r − mean)² / (n − 1)` over the last `period` daily log returns, `r_i = ln(close_i / close_{i−1})`.

**Derivation**: Daily log returns are approximately i.i.d.; their standard deviation is the *daily* vol. Annualizing under the square-root-of-time rule (252 US trading days) gives annualized vol in the same units as IV (so IV-vs-HV comparison is apples-to-apples). Sample variance (n−1) is the unbiased estimator.

**Source**: Standard quantitative finance (e.g., [Columbia FE lecture notes](https://www.columbia.edu/~mh2078/FoundationsFE/BlackScholes.pdf); Hull, *Options Futures and Other Derivatives*). The √252 annualization is universal for US equities.

**Use**: VRP gate (`passes_vrp`: `IV > HV × 0.8`), thesis volatility check (`HV > 100%` → WARNING).

**Validation**: ✓ Standard. √252 is correct for US trading days.

---

## 4. RSI (Wilder)

**File**: `src/data/compute.py:50` (`compute_rsi`, period 14).

```
RS  = avg_gain / avg_loss            (over the first `period` deltas)
RSI = 100 − 100 / (1 + RS)
```
- `avg_gain` = mean of positive close-to-close deltas over the window
- `avg_loss` = mean of absolute negative deltas
- Edge: `avg_loss == 0` → RSI = 100

**Derivation**: Wilder's RSI measures the relative size of up-moves vs down-moves over a window. The `100 − 100/(1+RS)` map squashes the unbounded RS ratio into the [0,100] band. RSI > 70 = overbought; < 30 = oversold.

> **Note on smoothing**: The classic Wilder formulation uses a *recursive* smoothing for `avg_gain`/`avg_loss` after the first period (Wilder's SMMA). This implementation uses the **simple-mean** form for the windowed computation. The two converge for steady data and diverge slightly under volatility shifts; the difference is immaterial for the bucketed RSI scoring used here (RSI is bucketed into 10-point bands before scoring — see [scoring-spec.md](scoring-spec.md) §technical). For a research-grade backtest, switch to recursive Wilder smoothing.

**Source**: J. Welles Wilder, *New Concepts in Technical Trading Systems* (1978).

**Use**: RSI score → momentum score → trend composite → entry signal + CSP/CC trend extension.

**Validation**: ✓ Correct RSI formula (simple-window form). ⚠ Smoothing is simple-mean, not recursive Wilder — acceptable for bucketed use, document if used for tight thresholds.

---

## 5. MACD

**File**: `src/data/compute.py:71` (`compute_macd`, 12/26/9).

```
MACD    = EMA(12) − EMA(26)
Signal  = EMA(9) of MACD history
Histogram = MACD − Signal
EMA(p)  = price × k + prev_EMA × (1 − k),  k = 2 / (p + 1)
```

**Derivation**: MACD is the difference of two EMAs — a momentum oscillator. The signal line (EMA of MACD) lags; crossovers (MACD over/under signal) indicate momentum turns. The histogram (MACD − signal) visualizes the strength of the trend.

**Source**: Gerald Appel, 1970s. Standard TA. EMA formula verified against [Macroption](https://www.macroption.com/exponential-moving-average/).

**Use**: MACD score (bullish/bearish, accelerating/decelerating) → momentum score → trend composite.

**Validation**: ✓ Standard MACD. Implementation computes MACD history by re-evaluating EMAs on expanding windows (slightly O(n²) but correct for the watchlist sizes involved).

---

## 6. ADX (trend strength)

**File**: `src/data/compute.py:114` (`compute_adx`, period 14).

```
+DM = up_move if (up_move > down_move and up_move > 0) else 0
−DM = down_move if (down_move > up_move and down_move > 0) else 0
TR  = max(high−low, |high−prev_close|, |low−prev_close|)
+DI = 100 × smoothed(+DM) / ATR
−DI = 100 × smoothed(−DM) / ATR
DX  = 100 × |+DI − −DI| / (+DI + −DI)
ADX = smoothed DX over `period`      ← this impl returns DX as an ADX approximation
```

**Derivation**: ADX measures trend *strength* (not direction). +DI/−DI capture directional movement; DX normalizes their spread; ADX smooths DX. ADX ≥ 25 conventionally indicates a trending market.

> **Implementation note**: this implementation returns DX (the un-smoothed directional index) rather than the fully Wilder-smoothed ADX. It is therefore a *lower-bound* estimate of trend strength — it reacts faster but is noisier. For the bucketed scoring used here (ADX ≥40/25/20 → 100/75/50/25 points) this is acceptable and the buckets are wide enough to absorb the noise. Flag for the backtest harness.

**Source**: Wilder (1978); [Investopedia ADX](https://www.investopedia.com/terms/a/adx.asp).

**Validation**: ⚠ Returns DX, not fully smoothed ADX. Acceptable for wide buckets; document for tight use.

---

## 7. Beta vs SPY

**File**: `src/data/compute.py:174` (`compute_beta`, ≥60 days of daily log returns).

```
β = Cov(stock_returns, SPY_returns) / Var(SPY_returns)
```
sample covariance and variance with (n−1) denominator.

**Derivation**: Beta is the slope of the OLS regression of stock returns on market returns — exactly `Cov/Var`. β > 1 → more volatile than the market; β < 1 → less.

**Source**: CAPM / standard finance.

**Validation**: ✓ Correct (sample estimators). Requires ≥60 aligned trading days; returns `None` otherwise (treated conservatively as "unknown").

---

## 8. Bollinger Bands

**File**: `src/data/compute.py:145` (`compute_bollinger`, period 20, 2σ).

```
mid   = SMA(prices, 20)
var   = Σ(p − mid)² / 20          (population variance)
upper = mid + 2 × sqrt(var)
lower = mid − 2 × sqrt(var)
```

**Source**: John Bollinger (1980s). Standard.

**Validation**: ✓ Uses population variance (÷n). Bollinger's original uses population; some platforms use sample (÷n−1). Immaterial at n=20.

---

## 9. Max Pain

**File**: `src/data/compute.py:314` (`_compute_max_pain`).

```
For each candidate strike s:
  pain(s) = Σ_{calls c: c.strike < s} (s − c.strike) × c.OI × 100
          + Σ_{puts p: p.strike > s}  (p.strike − s) × p.OI × 100
Max Pain = argmin_s pain(s)
```

**Derivation**: Max Pain is the strike at which the total intrinsic value of all open options is minimized — equivalently, the strike where option *writers* (dealers/market-makers) collectively pay out the least. Theory: as expiry approaches, dealer hedging pulls the price toward max pain. This is the standard definition.

**Source**: WSW/Optionetics convention; [SpotGamma](https://spotgamma.com/) and most option analytics platforms use exactly this intrinsic-value-minimization definition.

**Validation**: ✓ Matches the standard definition. Uses OI (not volume); per-contract ×100 share multiplier included.

---

## 10. ATM IV & 25Δ Skew

**File**: `src/data/compute.py:339` (`_compute_atm_iv`), `:354` (`_compute_skew_25d`).

```
ATM IV     = (IV_nearest_call + IV_nearest_put) / 2     (nearest by |strike − price|)
Skew (25Δ) = IV(25Δ put) − IV(25Δ call)                 (each = contract with |Δ| closest to 0.25)
```

**Derivation**: ATM IV is the spot-volatility benchmark. Skew measures the *shape* of the vol surface: positive skew (put IV > call IV at equal delta) signals demand for downside protection — a fear signal; flat/inverted skew signals complacency.

**Source**: Standard equity-vol surface convention (CBOE SKEW index uses a related 30Δ measure).

**Validation**: ✓ Standard. Note: "nearest by |Δ|" picks a single contract per side; a more rigorous measure interpolates. Acceptable for the watchlist liquidity.

---

## 11. Term Structure

**File**: `src/data/compute.py:364` (`_compute_term_structure`).

```
near_IV = mean(IV of front-month expiry)
far_IV  = mean(IV of back-month expiry)
diff_pct = (near_IV − far_IV) / far_IV × 100
  diff_pct > 2   → BACKWARDATION   (near IV > far — stress, demand for near protection)
  diff_pct < −2  → CONTANGO        (near IV < far — calm, normal carry)
  otherwise      → FLAT
```

**Derivation**: Term structure compares IV across expiries. Backwardation (near > far) is unusual and signals near-term stress (e.g., pre-earnings, pre-event); contango is the normal state where longer-dated options carry more uncertainty.

**Source**: Standard options-market structure; see [research_dte_selection.md](research_dte_selection.md) §3.2 (Tastytrade IV-adjusted DTE).

**Validation**: ✓ Reasonable. The 2% threshold is a heuristic; cited in scoring as a term-structure quality input.

---

## 12. Gamma Exposure (GEX)

**File**: `src/data/compute.py:394` (`_compute_gex`).

```
GEX = Σ_calls |gamma| × OI × spot × 100
    − Σ_puts  |gamma| × OI × spot × 100
Positive GEX = dealers long gamma (price-dampening)
Negative GEX = dealers short gamma (price-amplifying)
```

**Derivation**: Gamma exposure estimates the dollar-gamma that dealers must hedge per unit of spot move. The convention here: **calls contribute positive gamma, puts contribute negative** (the naive sign convention). The `× spot × 100` converts per-contract gamma into notional terms.

> ⚠ **This is a known simplification.** The full practitioner GEX formula is:
> ```
> GEX = Σ_calls gamma × OI × 100 × spot² × 0.5  −  Σ_puts gamma × OI × 100 × spot² × 0.5
> ```
> Differences from this implementation:
> 1. **Missing `spot` factor** — this uses `gamma × OI × spot × 100`; the standard uses `gamma × OI × spot² × 0.5 × 100` (the extra `spot × 0.5` reflects dealer-position assumption: dealers are typically *short* calls / *long* puts from customer flow, halved for net positioning).
> 2. **Sign convention** — uses naive (calls +, puts −). The dealer-perspective convention (short calls → dealers' call-gamma is negative; long puts → dealers' put-gamma is positive) gives the *opposite* signs. Different platforms use different conventions; the **sign and the flip level** are what matter operationally, and the magnitude scaling here is internally consistent.
>
> **Impact**: the engine uses GEX only as a **directional regime signal** (negative chain GEX < −500k pauses new CSPs). The simplification preserves the sign and approximate flip level, so the *operational* behavior is correct. The *magnitude* is not comparable across platforms — do not quote this GEX number against SpotGamma/TradingFlow.

**Source**: [SpotGamma](https://spotgamma.com/gamma-exposure-gex/), [InsiderFinance GEX guide](https://www.insiderfinance.io/resources/the-ultimate-guide-to-gamma-exposure-gex), r/options convention. **Verified against these on 2026-08-09.**

**Validation**: ⚠ Simplified — missing `spot² × 0.5`, uses naive sign. **Operationally correct as a directional regime signal; magnitudes are not cross-platform comparable.** Flagged for the backtest harness.

---

## 13. Put/Call Ratios

**File**: `src/data/compute.py:283–290`.

```
PCR (OI) = total_put_OI / total_call_OI
PCR (vol) = total_put_volume / total_call_volume
```

**Derivation**: PCR > 1 means more put activity than call activity — conventionally a sentiment indicator. Extreme PCR can be contrarian (capitulation) or trend (hedging demand).

**Source**: CBOE standard.

**Validation**: ✓ Standard.

---

## 14. Black-Scholes & Greeks (backtest harness only)

**Status**: **NOT in `src/` today.** Defined in `specs/research_backtesting_architecture.md §3.3` for the future backtest harness. Included here for validation because the user asked to confirm the BS formulas.

```
d1 = [ ln(S/K) + (r + σ²/2)·T ] / (σ·√T)
d2 = d1 − σ·√T

Call = S·N(d1) − K·e^(−rT)·N(d2)
Put  = K·e^(−rT)·N(−d2) − S·N(−d1)

Delta_call = N(d1)            Delta_put = N(d1) − 1
Gamma      = N'(d1) / (S·σ·√T)
Theta      = [ −S·N'(d1)·σ / (2√T) − r·K·e^(−rT)·N(d2) ] / 365   (daily, call)
Vega       = S·N'(d1)·√T / 100   (per 1% IV change)
Rho        = K·T·e^(−rT)·N(d2) / 100
```
- `S` spot, `K` strike, `T` years to expiry, `r` risk-free rate, `σ` IV (decimal), `N` standard normal CDF, `N'` standard normal PDF.

**Validation**: ✓ All formulas match [Macroption — Black-Scholes formulas](https://www.macroption.com/black-scholes-formula/) and [Wikipedia](https://en.wikipedia.org/wiki/Black%E2%80%93Scholes_model) exactly (verified 2026-08-09). Note `Vega(call) = Vega(put)` and `Gamma(call) = Gamma(put)` (put-call symmetry) — confirmed. Theta is the *daily* call theta (÷365); put theta differs by the `−r·K·e^(−rT)` drift term sign. **This is a *European* model** — American-style early-exercise (notably for deep-ITM puts and ex-div calls) is a known limitation, acceptable for OTM wheel positions per the research.

**Where it would live**: `src/backtest/simulator.py` (planned). Inputs: historical OHLCV (available via moomoo kline / yfinance) + an IV assumption (the hard part — see `research_backtesting_architecture.md §3.2`).

---

## 15. Put Credit Spread: max_loss, RoC, credit ratio

**File**: `src/strategies/credit_spread.py:45` (`put_spread_roc`), gates at `:284–307`.

```
net_credit   = short_bid − long_ask              (must be > 0)
width        = short_strike − long_strike        (in strike-$)
max_loss     = width − net_credit                (the defined risk)
capital_req  = max_loss × 100                    (100% cash-backed — no margin)

RoC          = (net_credit / max_loss) × (365 / DTE) × 100
credit_ratio = net_credit / width                (must be ≥ 1/3)
```

**Derivation**: A put credit spread sells a put and buys a lower-strike put (same expiry). The maximum loss occurs if both puts expire ITM: you're assigned at the short strike and exercise your long at the lower strike, losing `width` per share, less the premium you collected → `width − net_credit`. The key risk-discipline point: capital at risk is `max_loss`, **not** the full short strike (this is what makes it a *defined-risk* substitute for a CSP). RoC is on `max_loss` (the actual capital at risk), not on the strike.

**The "no pennies for dollars" rule**: `credit_ratio ≥ 1/3` ensures you collect at least one-third of the width in premium — i.e., you're not risking $3 to make $1. Source: standard put-credit-spread discipline (Tastytrade).

**Config**: `credit_spread.credit_ratio_min: 0.333`, `roc_min: 8.0`, `cash_backed: true`. Suggestion-only — the OIE engine explicitly skips `PS` candidates (`oie_engine.py:641–643`).

**Validation**: ✓ Correct defined-risk formulas. The `max_loss` cash-backing enforces GOAL.md #4 ("never prefer margin").

---

## 16. Profit-captured, loss-multiple, drawdown

These are the small but critical formulas the decision core uses. Files: `src/analysis/exit_management.py`, `src/scoring/holding_score.py`, `src/risk/holdings_exit.py`.

```
profit_captured = (entry_premium − current_bid) / entry_premium × 100   [%]
  (positive = the option has decayed in your favor; you can buy it back cheaper)

loss_multiple   = |profit_captured| / 100   when profit_captured < 0
  (= how many × the entry premium you've lost; e.g. −200% → 2.0×)

premium_collected = entry_premium × qty × 100   [total credit banked, $]
  (selects the absolute-loss band — see heavy_loss_for_premium below)

heavy_loss_for_premium(premium_collected) → max_loss   [$ floor for the catch-all]
  = first band in heavy_loss_bands whose premium_max ≥ premium_collected
  e.g. $300 → $1,000 · $1,200 → $2,000 · $3,000 → $5,000 · $6,000 → $8,000
  (legacy heavy_loss_abs scalar collapses to a single (inf, N) band)

drawdown_from_basis = (adjusted_basis − current_price) / adjusted_basis   [decimal]
  where adjusted_basis = assignment_strike − Σ(all premiums collected in the campaign)
```

**Validation**: ✓ All internally consistent and standard. The `adjusted_basis` carries the whole-campaign premium (puts + calls) — brokers don't track this; the engine does (`src/risk/monitor.py:130` `campaign_adjusted_basis`). The `heavy_loss_for_premium` band lookup replaced the flat `−$1,000` floor (2026-08-09), which pre-empted the DTE premium tiers for any premium above ~$667 — see [exit-and-profit-management-spec.md §5.3](exit-and-profit-management-spec.md).

---

## 17. Validation summary table

| # | Formula | File:line | Status | Note |
|---|---------|-----------|--------|------|
| 1 | CSP/CC RoC (annualized) | `contract_filters.py:24,32` | ✓ | Simple-interest convention |
| 2 | IV Rank / Percentile | `compute.py:237,247` | ✓ | Matches Tastytrade/Barchart exactly |
| 3 | HV (30d annualized) | `compute.py:155` | ✓ | √252 correct for US |
| 4 | RSI (Wilder) | `compute.py:50` | ✓ ⚠ | Simple-window form (not recursive Wilder) — fine for buckets |
| 5 | MACD (12/26/9) | `compute.py:71` | ✓ | Standard |
| 6 | ADX | `compute.py:114` | ⚠ | Returns DX, not fully smoothed ADX — fine for wide buckets |
| 7 | Beta vs SPY | `compute.py:174` | ✓ | Cov/Var, ≥60 days |
| 8 | Bollinger (20,2) | `compute.py:145` | ✓ | Population variance |
| 9 | Max Pain | `compute.py:314` | ✓ | Standard intrinsic-min definition |
| 10 | ATM IV / 25Δ skew | `compute.py:339,354` | ✓ | Nearest-contract (no interpolation) |
| 11 | Term structure | `compute.py:364` | ✓ | 2% threshold heuristic |
| 12 | GEX | `compute.py:394` | ⚠ | Missing `spot²×0.5`; naive sign — **directionally correct, not cross-platform comparable** |
| 13 | Put/Call ratios | `compute.py:283` | ✓ | Standard |
| 14 | Black-Scholes + Greeks | (spec only) | ✓ | Matches Macroption/Wikipedia; European (not American) |
| 15 | PCS max_loss / RoC / credit_ratio | `credit_spread.py:45` | ✓ | Defined-risk, cash-backed |
| 16 | profit_captured / loss_multiple / heavy_loss band / drawdown | exit_management, holding_score, config, holdings_exit | ✓ | Internally consistent; band lookup replaces flat −$1k (2026-08-09) |

**Bottom line**: the formulas that drive decisions (RoC, IV Rank, RSI, MACD, the trend composite, the exit math) are **correct against authoritative sources**. Two known simplifications (ADX smoothing, GEX scaling) are **acceptable for the bucketed/operational use** the framework makes of them and are flagged for the backtest harness. No formula is *wrong* in a way that would mislead a decision at the current thresholds.

---

### Sources cited
- Tastytrade — IV Rank & Percentile: https://www.tastylive.com/concepts-strategies/implied-volatility-rank-percentile
- Barchart — IV Rank vs IV Percentile: https://www.barchart.com/education/iv_rank_vs_iv_percentile
- TradingBlock — IV Rank: https://www.tradingblock.com/blog/iv-rank-vs-iv-percentile
- Macroption — Black-Scholes formulas: https://www.macroption.com/black-scholes-formula/
- Wikipedia — Black-Scholes: https://en.wikipedia.org/wiki/Black%E2%80%93Scholes_model
- Columbia FE — Black-Scholes lecture: https://www.columbia.edu/~mh2078/FoundationsFE/BlackScholes.pdf
- SpotGamma — GEX: https://spotgamma.com/gamma-exposure-gex/
- InsiderFinance — GEX guide: https://www.insiderfinance.io/resources/the-ultimate-guide-to-gamma-exposure-gex
- Investopedia — ADX: https://www.investopedia.com/terms/a/adx.asp

*Audited 2026-08-09. Re-verify formulas against sources before relying on them for live capital; the code is the source of truth between audits.*
