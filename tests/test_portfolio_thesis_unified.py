"""Tests for the unified thesis-validation set in scripts/portfolio.py.

Covers:
- `_thesis_targets` unions stocks + option underlyings + watchlist (TO-DO #4).
- Do-Not-Wheel auto-removal on recovery: a ticker whose thesis recovers to the
  configured status is removed early instead of waiting out 6 months (TO-DO #5).

`scripts/portfolio.py` runs code at import via __file__, so we load the helper
with importlib (same pattern as test_portfolio_monthly_guardrail.py). The DNL
recovery behavior is verified against src/data/do_not_wheel_list.py directly,
mirroring the logic _print_thesis applies.
"""

import importlib.util
from pathlib import Path

import pytest

from src.data.do_not_wheel_list import DoNotWheelList
from src.data.portfolio_loader import Portfolio, Funds

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "portfolio.py"


def _load_targets_fn():
    spec = importlib.util.spec_from_file_location("_portfolio_thesis", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._thesis_targets


_thesis_targets = _load_targets_fn()


def _pf(stocks=None, options=None):
    """Build a Portfolio with stock + option dicts."""
    return Portfolio(
        funds=Funds(),
        stocks=stocks or {},
        options=options or {},
    )


# ── _thesis_targets: stocks ∪ option underlyings ∪ watchlist ──────

def test_targets_includes_stock_holdings():
    pf = _pf(stocks={'V': {'qty': 100}, 'MSFT': {'qty': 10}})
    targets = _thesis_targets(pf)
    assert targets['V'] == 'stock'
    assert targets['MSFT'] == 'stock'


def test_targets_includes_option_underlyings():
    """A ticker held ONLY via an option position is still thesis-checked."""
    pf = _pf(options={'US.AVGO260731P350000': {'ticker': 'AVGO', 'type': 'PUT'}})
    targets = _thesis_targets(pf)
    assert 'AVGO' in targets
    assert targets['AVGO'] == 'option'


def test_targets_stock_takes_precedence_over_option():
    """A ticker held as both stock and option is labeled 'stock'."""
    pf = _pf(
        stocks={'V': {'qty': 500}},
        options={'US.V260821C380000': {'ticker': 'V', 'type': 'CALL'}},
    )
    targets = _thesis_targets(pf)
    assert targets['V'] == 'stock'


def test_targets_unifies_all_three_sources():
    pf = _pf(
        stocks={'V': {'qty': 500}},                                      # stock
        options={'US.AVGO260731P350000': {'ticker': 'AVGO'}},            # option
    )
    targets = _thesis_targets(pf)
    # V (stock), AVGO (option), plus watchlist tickers from config
    assert {'V', 'AVGO'} <= set(targets)
    # At least one watchlist ticker from config/rules.yaml default_watchlist.
    # _thesis_targets normalizes (strips US.), so compare normalized.
    from src.config import get_config
    wl = {str(t).upper().replace('US.', '') for t in get_config().default_watchlist}
    assert wl & set(targets), "watchlist tickers should appear as 'watchlist' targets"
    assert any(s == 'watchlist' for s in targets.values())


def test_targets_dedups_each_ticker_once():
    pf = _pf(
        stocks={'AAPL': {'qty': 5}},
        options={'US.AAPL260918C300000': {'ticker': 'AAPL'}},
    )
    targets = _thesis_targets(pf)
    assert list(targets).count('AAPL') == 1


# ── Do-Not-Wheel auto-removal on recovery ─────────────────────────
# _print_thesis removes a listed ticker when status ∈ recovery_ok and
# dnl.is_excluded(ticker). We verify the DoNotWheelList mechanics that gate it.

@pytest.fixture
def fresh_dnl(tmp_path):
    """A DoNotWheelList backed by a temp YAML so tests never touch the real file."""
    return DoNotWheelList(config_path=str(tmp_path / "dnl.yaml"))


def test_dnl_add_then_manual_remove(fresh_dnl):
    """remove() takes a ticker off the list immediately (used on recovery)."""
    fresh_dnl.add('AMD', months=6, reason='P/E 184 - speculative')
    assert fresh_dnl.is_excluded('AMD')
    fresh_dnl.remove('AMD')
    assert not fresh_dnl.is_excluded('AMD')
    assert fresh_dnl.get_reason('AMD') is None


def test_dnl_recovery_logic_intact_status_triggers_removal(fresh_dnl):
    """Mirror of _print_thesis: INTACT + currently excluded → remove."""
    from src.analysis.thesis_validator import ThesisStatus
    fresh_dnl.add('PLTR', months=6, reason='P/E 195 - speculative')
    # Simulate a re-validation that comes back INTACT
    recovery_ok = {ThesisStatus.INTACT}
    status = ThesisStatus.INTACT
    if status in recovery_ok and fresh_dnl.is_excluded('PLTR'):
        fresh_dnl.remove('PLTR')
    assert not fresh_dnl.is_excluded('PLTR')


def test_dnl_broken_status_keeps_ticker_listed(fresh_dnl):
    """A still-BROKEN ticker must NOT be removed — stays for 6 months."""
    from src.analysis.thesis_validator import ThesisStatus
    fresh_dnl.add('TSLA', months=6, reason='P/E 285 - speculative')
    recovery_ok = {ThesisStatus.INTACT}
    status = ThesisStatus.BROKEN
    if status in recovery_ok and fresh_dnl.is_excluded('TSLA'):
        fresh_dnl.remove('TSLA')
    assert fresh_dnl.is_excluded('TSLA')


def test_dnl_damaged_keeps_ticker_by_default(fresh_dnl):
    """Default recovery_status_for_removal=INTACT → DAMAGED does not remove."""
    from src.analysis.thesis_validator import ThesisStatus
    fresh_dnl.add('AVGO', months=6, reason='P/E elevated')
    recovery_ok = {ThesisStatus.INTACT}              # default
    status = ThesisStatus.DAMAGED
    if status in recovery_ok and fresh_dnl.is_excluded('AVGO'):
        fresh_dnl.remove('AVGO')
    assert fresh_dnl.is_excluded('AVGO')             # still listed


def test_dnl_config_allows_damaged_recovery(fresh_dnl, monkeypatch):
    """If recovery_status_for_removal=DAMAGED, a DAMAGED ticker is removed."""
    from src.config import get_config
    from src.analysis.thesis_validator import ThesisStatus
    monkeypatch.setattr(type(get_config()), 'thesis_validation',
                        lambda self, k, d=None: 'DAMAGED' if k == 'recovery_status_for_removal' else d)
    fresh_dnl.add('AVGO', months=6, reason='P/E elevated')
    cfg_rec = get_config().thesis_validation('recovery_status_for_removal', 'INTACT')
    recovery_ok = ({ThesisStatus.INTACT, ThesisStatus.DAMAGED}
                   if str(cfg_rec).upper() == 'DAMAGED' else {ThesisStatus.INTACT})
    status = ThesisStatus.DAMAGED
    if status in recovery_ok and fresh_dnl.is_excluded('AVGO'):
        fresh_dnl.remove('AVGO')
    assert not fresh_dnl.is_excluded('AVGO')
