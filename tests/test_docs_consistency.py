"""Docs ↔ config consistency — the 2026-08-14 drift audit as a regression test.

GOAL.md / OIE_SYSTEM_PROMPT.md / CLAUDE.md previously hardcoded limit values
that had drifted from config/rules.yaml (15% vs 25% single-position, 8 vs 10
open positions, 15-20 vs 12-20 NEUTRAL VIX band, a misleading "Cash + Stock >
70% of CSP liability" checklist item, and an abort-vs-yfinance-fallback
contradiction). rules.yaml is the single source of truth; these tests fail
whenever docs and config drift apart again.

Expected strings are BUILT FROM config values so the coupling is explicit:
change rules.yaml → these tests tell you which doc lines are now stale.
"""

from pathlib import Path

import pytest

from src.config import get_config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module')
def cfg():
    return get_config()


@pytest.fixture(scope='module')
def goal():
    return (ROOT / 'GOAL.md').read_text()


@pytest.fixture(scope='module')
def prompt():
    return (ROOT / 'OIE_SYSTEM_PROMPT.md').read_text()


@pytest.fixture(scope='module')
def claude_md():
    return (ROOT / 'CLAUDE.md').read_text()


# ── Position limits follow rules.yaml ─────────────────────────────

def test_single_position_limit_matches_config(goal, prompt, cfg):
    cap_pct = int(round(cfg.max_single_position_pct * 100))
    assert f"≤ {cap_pct}% of net liq hard cap" in goal
    assert f"≤ {cap_pct}% net liq hard cap" in prompt
    # the stale pre-reconciliation values must be gone
    assert "≤ 15% of net liquidation" not in goal
    assert "Single position ≤ 15%" not in prompt


def test_open_positions_limit_matches_config(goal, prompt, claude_md, cfg):
    assert f"Open option positions ≤ {cfg.max_open_positions}" in prompt
    assert f"Open positions (option contracts) | ≤ {cfg.max_open_positions}" in goal
    assert "Open positions ≤ 8" not in prompt
    assert "Open positions ≤ 8" not in claude_md
    assert "≤ 8 total" not in goal


def test_daily_new_positions_follow_config(goal, cfg):
    assert f"≤ {cfg.max_daily_new_positions}/day config cap" in goal


def test_credit_stress_gate_documented(goal, prompt, cfg):
    cap_pct = int(round(cfg.credit_stress_position_mult_cap * 100))
    assert f"position size is capped at {cap_pct}%" in goal
    assert f"capped at {cap_pct}%" in prompt
    assert "credit_stress_position_mult_cap" in goal
    assert "credit_stress_position_mult_cap" in prompt


# ── Regime band ───────────────────────────────────────────────────

def test_neutral_vix_band_matches_config(goal, prompt, cfg):
    band = f"{int(cfg.vix_low)}-{int(cfg.vix_normal)}"
    assert f"| NEUTRAL | {band} |" in goal
    assert f"NEUTRAL ({band})" in prompt
    assert "| NEUTRAL | 15-20 |" not in goal   # the old undefined-gap band


# ── Retired misleading wording ────────────────────────────────────

def test_cash_stock_70pct_checklist_item_retired(goal, prompt):
    """'Cash + Stock > 70% of CSP liability' passed nominally (199%) while
    cash alone covered 19% — stock cannot settle a put assignment."""
    assert "Cash + Stock values > 70%" not in goal
    assert "Cash + Stock values > 70%" not in prompt
    assert "cash-secured" in goal.lower()
    assert "cash-secured" in prompt.lower()


# ── Collar gate ───────────────────────────────────────────────────

def test_collar_gate_documented(goal, prompt):
    assert "FREE shares" in goal and "collar" in goal.lower()
    assert "FREE shares" in prompt and "collar check" in prompt


# ── Abort-vs-fallback policy (no contradiction) ────────────────────

def test_data_fallback_policy_unambiguous(prompt):
    assert "Portfolio state is moomoo-only" in prompt
    assert "YFINANCE_FALLBACK" in prompt
    # §6 fallback section must scope the fallback to market data only
    assert "market data only — portfolio state never falls back" in prompt


def test_venv_interpreter_documented(prompt):
    assert ".venv/bin/python" in prompt
