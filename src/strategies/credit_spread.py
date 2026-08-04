"""Put Credit Spread (PCS) scoring — defined-risk income supplement.

A put credit spread = SELL a put at the short strike + BUY a put at a lower
(long) strike, same expiry. Net credit = short premium − long premium. Max
loss = width − net credit (defined, bounded). Max profit = net credit.

WHEN PCS BEATS A PLAIN CSP (the core decision):
  • CSP is paused (VIX>25 / cash<20% / VOLATILE+ regime / stock >15% off basis)
    — you still want income but can't/shouldn't commit the full strike in cash.
  • You want the directional/IV thesis but NOT the shares (PCS = no assignment
    beyond the long strike; pure premium play).
  • The strike is too large to fully cash-secure with available cash.

Plain CSP stays better when you WANT the shares at a good basis (wheel entry)
and CSP is allowed — don't spread what you want to be assigned.

Risk discipline (honors GOAL.md #4 "never prefer margin"):
  • capital_required = max_loss × 100, NOT the full short strike.
  • max_loss must be 100% cash-backed (cash_backed gate).
  • credit_ratio_min (default 1/3): never take pennies for dollars of risk.
  • width cap: keep max_loss bounded.
  • Short-leg delta within the regime delta range (reuses passes_delta).

This module is pure + deterministic — no network, no AI. All thresholds flow
from config/rules.yaml → src.config. Tests build synthetic PUT chains; no
moomoo connection is required.
"""

from typing import Optional

from src.config import get_config, Config
from src.data.models import StockSnapshot, OptionSnapshot, TradeCandidate
from src.filters.contract_filters import (
    passes_liquidity, passes_delta, iv_sane, passes_vrp,
)
from src.scoring.screener_score import (
    _cfg_val, _contract_penalty, _reason, _score_stars,
)


# ═══════════════════════════════════════════════════════════════
# RoC FORMULA
# ═══════════════════════════════════════════════════════════════

def put_spread_roc(net_credit: float, width: float, dte: int) -> float:
    """Annualized return on capital for a put credit spread.

    Capital at risk = max_loss = width - net_credit (per share).
    RoC = (net_credit / max_loss) × (365 / DTE) × 100.

    Returns 0.0 for degenerate inputs (no credit, no width, no time, or when
    net_credit >= width which would make max_loss <= 0).
    """
    if net_credit <= 0 or width <= 0 or dte <= 0:
        return 0.0
    max_loss = width - net_credit
    if max_loss <= 0:
        return 0.0
    return (net_credit / max_loss) * (365.0 / dte) * 100


# ═══════════════════════════════════════════════════════════════
# SPREAD-SPECIFIC PENALTY
# ═══════════════════════════════════════════════════════════════

def _spread_penalty(short_leg: OptionSnapshot, long_leg: OptionSnapshot,
                    net_credit: float, width: float,
                    cfg: Optional[Config] = None) -> float:
    """Per-spread score adjustment (added to ticker score). Lower = better.

    Reuses the single-leg contract penalty on the SHORT leg (it carries the
    directional risk and dominates the spread's greeks), then layers spread-
    specific quality gates on top. Mirrors _contract_penalty()'s structure so
    PCS scores remain comparable to CSP/CC on the same 1-10 scale.
    """
    if cfg is None:
        cfg = get_config()

    # 1. Short-leg quality (DTE window, OI, spread, delta, IV, volume) — reuse
    #    the shared ladder so a PCS isn't ranked above an equally-setup CSP
    #    purely on tiny-capital RoC.
    short_delta = abs(short_leg.delta or 0)
    # RoC used only to place the short leg in the bonus ladder; cap it so the
    # spread's high RoC-on-max_loss doesn't inflate the short-leg bonus.
    ladder_roc = min(
        put_spread_roc(net_credit, width, short_leg.dte),
        _cfg_val(lambda c: c.roc_min_csp) * 1.5,
    )
    penalty = _contract_penalty(short_leg, short_delta, ladder_roc)

    # 2. Credit-quality gate: penalize thin credits (pennies for dollars).
    #    credit_ratio = net_credit / width; >= 1/3 is healthy.
    credit_ratio = net_credit / width if width > 0 else 0.0
    if credit_ratio < cfg.credit_spread_credit_ratio_min:
        penalty += 2.0
    elif credit_ratio < cfg.credit_spread_credit_ratio_min * 1.5:
        penalty += 0.8
    else:
        penalty -= 0.5  # reward fat credits

    # 3. Width quality: tighter = less capital at risk (better RoC), but too
    #    tight = no real protection. Sweet spot in the mid range.
    w_min = cfg.credit_spread_width_min
    w_max = cfg.credit_spread_width_max
    w_mid = (w_min + w_max) / 2
    if width < w_min or width > w_max:
        penalty += 1.5
    elif abs(width - w_mid) <= (w_max - w_min) / 4:
        penalty -= 0.3  # near the middle of the allowed band

    # 4. Long-leg liquidity: a thin protective leg is hard to exit / adjust.
    if (long_leg.open_interest or 0) < cfg.credit_spread_long_leg_oi:
        penalty += 1.0
    if (long_leg.volume or 0) < cfg.credit_spread_long_leg_volume:
        penalty += 0.5

    # 5. Long-leg spread: wide bid-ask on the protective leg erodes the credit.
    if (long_leg.bid_ask_spread_pct or 0) > 5:
        penalty += 1.0
    elif (long_leg.bid_ask_spread_pct or 0) > 2:
        penalty += 0.5

    return penalty


# ═══════════════════════════════════════════════════════════════
# SHORT-LEG ELIGIBILITY (reuses shared gates, skips CSP-RoC gate)
# ═══════════════════════════════════════════════════════════════

def _short_leg_eligible(short_leg: OptionSnapshot, regime: str, snap: StockSnapshot,
                        cfg: Optional[Config] = None) -> tuple[bool, str]:
    """Gate the short leg with the SHARED contract gates, but NOT the CSP RoC
    gate (spread RoC is computed on max_loss, not the full strike).

    Applies: liquidity, delta (CSP regime range), IV sanity, VRP.
    Returns (passed, reason).
    """
    if cfg is None:
        cfg = get_config()

    if not passes_liquidity(short_leg, cfg):
        return False, 'short-leg liquidity'
    ok, reason = passes_delta(short_leg, 'CSP', regime, cfg)
    if not ok:
        return False, f'short-leg {reason}'
    if not iv_sane(short_leg):
        return False, 'short-leg IV sanity'
    if not passes_vrp(short_leg, getattr(snap, 'hv_30d', None)):
        return False, 'short-leg VRP'
    return True, ''


# ═══════════════════════════════════════════════════════════════
# LONG-LEG SELECTION
# ═══════════════════════════════════════════════════════════════

def _pick_long_leg(short_leg: OptionSnapshot, same_expiry_puts: list,
                   cfg: Optional[Config] = None) -> Optional[OptionSnapshot]:
    """Pick the best protective (long) put for a given short leg.

    "Best" = the long leg that MAXIMIZES net credit per dollar of width while
    staying inside the allowed width band and meeting long-leg liquidity. We
    search candidate strikes by walking the preferred widths first (greedy),
    then fall back to any strike within [width_min, width_max].

    Ties settle on the narrower width (less capital at risk).
    """
    if cfg is None:
        cfg = get_config()

    short_strike = short_leg.strike
    short_bid = short_leg.bid or 0.0
    if short_bid <= 0:
        return None

    w_min = cfg.credit_spread_width_min
    w_max = cfg.credit_spread_width_max

    # Candidate long puts: same expiry, strike strictly below short, tradable.
    candidates = [
        p for p in same_expiry_puts
        if p is not short_leg
        and (p.strike or 0) < short_strike
        and (p.bid or 0) > 0
        and (p.open_interest or 0) >= cfg.credit_spread_long_leg_oi
        and (p.volume or 0) >= cfg.credit_spread_long_leg_volume
    ]
    if not candidates:
        return None

    best = None
    best_key = None  # (credit_ratio desc, width asc) → maximize credit/width
    for p in candidates:
        width = short_strike - p.strike
        if width < w_min or width > w_max:
            continue
        # Net credit per share = short bid − long ask (we pay the ask to buy).
        net_credit = short_bid - (p.ask or 0.0)
        if net_credit <= 0:
            continue
        credit_ratio = net_credit / width
        # Prefer high credit_ratio, then narrow width.
        key = (round(credit_ratio, 4), round(-width, 4))
        if best_key is None or key > best_key:
            best_key = key
            best = p
    return best


# ═══════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════

def score_put_credit_spreads(puts: list, snap: StockSnapshot, ticker: str,
                              ticker_score: float, regime: str,
                              net_liq: float = 0, cash: float = 0,
                              cfg: Optional[Config] = None,
                              gex_negative: bool = False) -> list:
    """Score put credit spreads for one ticker. Returns list[TradeCandidate]
    with strategy='PS'.

    Reuses the PUT contracts already fetched by the screener (no extra API
    call). Buckets puts by expiry, pairs each eligible short leg with the best
    long leg, and returns at most `credit_spread_max_per_ticker` per ticker
    (one best spread per expiry, then the top-N across expiries).

    Args:
        puts: list[OptionSnapshot] with option_type == 'PUT' (any expiry).
        snap: StockSnapshot for the underlying (hv_30d, last_price used).
        ticker: bare ticker symbol, e.g. 'AAPL'.
        ticker_score: 1-10 ticker-level score from _compute_ticker_score().
        regime: market regime label (BULLISH, NEUTRAL, etc.).
        net_liq: net liquidation (for cash-backed gate).
        cash: available cash (for cash-backed gate).
        cfg: Config (auto-loaded if None).
        gex_negative: if True, dealer is short gamma → skip (parity with CSP).

    Returns:
        list[TradeCandidate] sorted by score (best first), at most
        credit_spread_max_per_ticker entries.
    """
    if cfg is None:
        cfg = get_config()
    if not cfg.credit_spread_enabled:
        return []
    if gex_negative:  # dealer short gamma = same caution as CSP
        return []

    # Bucket puts by expiry so both legs share an expiry.
    by_expiry: dict[str, list] = {}
    for p in puts:
        if p.option_type != 'PUT':
            continue
        if not p.expiry:
            continue
        by_expiry.setdefault(p.expiry, []).append(p)

    results: list[TradeCandidate] = []
    code = cfg.credit_spread_strategy_code

    for expiry, expiry_puts in by_expiry.items():
        # Short-leg candidates: sort by descending strike so we consider the
        # highest (closest to ATM) short strikes first — they pay the most.
        short_candidates = sorted(
            [p for p in expiry_puts if (p.bid or 0) > 0],
            key=lambda p: p.strike,
            reverse=True,
        )

        best_for_expiry: Optional[TradeCandidate] = None

        for short_leg in short_candidates:
            ok, reason = _short_leg_eligible(short_leg, regime, snap, cfg)
            if not ok:
                continue

            long_leg = _pick_long_leg(short_leg, expiry_puts, cfg)
            if long_leg is None:
                continue

            width = short_leg.strike - long_leg.strike
            # Net credit per share: sell short @ bid, buy long @ ask.
            net_credit = (short_leg.bid or 0.0) - (long_leg.ask or 0.0)
            if net_credit <= 0 or width <= 0:
                continue

            max_loss = width - net_credit
            if max_loss <= 0:
                continue

            # Credit-ratio gate (hard — no pennies for dollars).
            if net_credit / width < cfg.credit_spread_credit_ratio_min:
                continue

            # RoC gate on max_loss (the honest capital at risk).
            roc = put_spread_roc(net_credit, width, short_leg.dte)
            if roc < cfg.credit_spread_roc_min:
                continue

            # Cash-backed gate (GOAL.md #4): max_loss must be covered by cash.
            # We check it when cash data is available; screener may pass cash=0
            # in --force mode, in which case the gate is skipped there and
            # enforced downstream by the position-sizing guardrails.
            capital_required = max_loss * 100
            if cfg.credit_spread_cash_backed and cash > 0:
                if capital_required > cash:
                    continue

            contract_score = ticker_score + _spread_penalty(
                short_leg, long_leg, net_credit, width, cfg)
            contract_score = round(contract_score, 2)

            cand = TradeCandidate(
                ticker=ticker,
                strategy=code,
                score=contract_score,
                strike=short_leg.strike,
                expiry=expiry,
                dte=short_leg.dte,
                delta=abs(short_leg.delta or 0),
                bid=short_leg.bid,
                ask=short_leg.ask,
                premium=round(net_credit * 100, 2),
                annualized_roc_pct=round(roc, 1),
                iv=short_leg.implied_vol,
                iv_rank=short_leg.iv_rank or 50.0,
                open_interest=short_leg.open_interest,
                capital_required=round(capital_required, 0),
                reason=_reason(ticker_score, contract_score, code),
                long_strike=long_leg.strike,
                spread_width=round(width, 2),
                net_credit=round(net_credit, 2),
                max_loss=round(max_loss, 2),
            )
            if best_for_expiry is None or cand.score < best_for_expiry.score:
                best_for_expiry = cand

        if best_for_expiry is not None:
            results.append(best_for_expiry)

    # Keep the best N across expiries (default 1 per ticker).
    results.sort(key=lambda c: c.score)
    return results[:cfg.credit_spread_max_per_ticker]
