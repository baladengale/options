"""
Adaptive Profit Target — Capital-Scarcity-Modulated Option Profit Booking

This module implements Refinement A from MASTER_EXIT_FRAMEWORK.md:
Dynamic profit targets based on capital availability and deployment.

Base Logic:
- 50% profit target (Tastytrade consensus standard)
- Modulate UP when capital is abundant (less opportunity cost to hold)
- Hard floor at 21 DTE (gamma risk management)

Authority: Tastytrade 50% rule, modified for capital scarcity context.
Research: https://www.tastylive.com/concepts-strategies/managing-winners
"""

from dataclasses import dataclass
from typing import Optional
import logging

log = logging.getLogger(__name__)


@dataclass
class ProfitTarget:
    """Dynamic profit target for option positions"""
    base_target_pct: float      # Base 50% target
    adjusted_target_pct: float   # Adjusted target (50-85%)
    capital_scarcity_level: str # "SCARCE", "NORMAL", "ABUNDANT"
    reason: str                 # Explanation of adjustment


class AdaptiveProfitCalculator:
    """
    Calculate adaptive profit targets based on capital scarcity

    Implements Refinement A from MASTER_EXIT_FRAMEWORK.md:
    - 50% base (Tastytrade consensus)
    - 70% when capital abundant (open < 50%, cash > 25%)
    - 85% when very abundant (open < 25%, cash > 30%)
    - Always respect 21 DTE floor (gamma risk)
    """

    def __init__(
        self,
        max_open_positions: int = 8,
        max_cash_buffer_critical: float = 0.10,
        max_cash_buffer_warning: float = 0.15,
        max_cash_buffer_target: float = 0.20,
        base_profit_target: float = 50.0
    ):
        """
        Initialize calculator with portfolio constraints

        Args:
            max_open_positions: Maximum number of open option positions
            max_cash_buffer_critical: Critical cash buffer threshold (10%)
            max_cash_buffer_warning: Warning cash buffer threshold (15%)
            max_cash_buffer_target: Target cash buffer threshold (20%)
            base_profit_target: Base profit target percentage (50%)
        """
        self.max_open_positions = max_open_positions
        self.cash_critical = max_cash_buffer_critical
        self.cash_warning = max_cash_buffer_warning
        self.cash_target = max_cash_buffer_target
        self.base_target = base_profit_target

    def calculate_profit_target(
        self,
        open_position_count: int,
        current_cash_buffer_pct: float,
        days_to_expiry: int,
        net_liquidation: Optional[float] = None,
        cash_available: Optional[float] = None
    ) -> ProfitTarget:
        """
        Calculate adaptive profit target based on capital scarcity

        Args:
            open_position_count: Current number of open option positions
            current_cash_buffer_pct: Current cash buffer as percentage of NLV
            days_to_expiry: Days until option expiry
            net_liquidation: Total portfolio value (optional, for better precision)
            cash_available: Actual cash available (optional, for better precision)

        Returns:
            ProfitTarget with adjusted target and explanation
        """
        # === Step 1: Determine Capital Scarcity Level ===

        # Position utilization
        position_utilization = open_position_count / self.max_open_positions

        # Cash buffer health
        if current_cash_buffer_pct < self.cash_critical:
            cash_health = "CRITICAL"
        elif current_cash_buffer_pct < self.cash_warning:
            cash_health = "WARNING"
        elif current_cash_buffer_pct < self.cash_target:
            cash_health = "ADEQUATE"
        else:
            cash_health = "ABUNDANT"

        # Determine scarcity level
        if position_utilization < 0.25 and cash_health == "ABUNDANT":
            scarcity_level = "ABUNDANT"
            adjusted_target = 85.0
            reason = (
                f"Capital abundant: {open_position_count}/{self.max_open_positions} positions "
                f"({position_utilization:.0%} utilized), {current_cash_buffer_pct:.1%} cash buffer. "
                f"Low opportunity cost to hold for more theta."
            )
        elif position_utilization < 0.50 and current_cash_buffer_pct >= self.cash_target:
            scarcity_level = "NORMAL"
            adjusted_target = 70.0
            reason = (
                f"Capital adequate: {open_position_count}/{self.max_open_positions} positions "
                f"({position_utilization:.0%} utilized), {current_cash_buffer_pct:.1%} cash buffer. "
                f"Can afford to capture more theta."
            )
        else:
            scarcity_level = "SCARCE"
            adjusted_target = self.base_target  # 50%
            reason = (
                f"Capital constrained: {open_position_count}/{self.max_open_positions} positions "
                f"({position_utilization:.0%} utilized), {current_cash_buffer_pct:.1%} cash buffer. "
                f"Recycle capital at base target to free slots."
            )

        # === Step 2: Apply DTE Floor (Hard Rule) ===

        if days_to_expiry <= 21:
            # Override: 21 DTE is hard floor regardless of profit target
            # This is the Tastytrade gamma risk management rule
            adjusted_target = 0.0  # Exit regardless of profit
            reason = (
                f"DTE floor hit: {days_to_expiry} DTE ≤ 21. "
                f"Gamma risk elevated. Must exit or roll regardless of profit ({adjusted_target:.0f}%)."
            )

        return ProfitTarget(
            base_target_pct=self.base_target,
            adjusted_target_pct=adjusted_target,
            capital_scarcity_level=scarcity_level,
            reason=reason
        )

    def should_close_position(
        self,
        current_profit_pct: float,
        profit_target: ProfitTarget,
        days_to_expiry: int
    ) -> tuple[bool, str]:
        """
        Determine if position should be closed based on profit and DTE

        Args:
            current_profit_pct: Current unrealized profit as percentage of premium
            profit_target: Calculated profit target
            days_to_expiry: Days until expiry

        Returns:
            (should_close: bool, action_message: str)
        """
        # Rule 1: DTE floor (hard rule)
        if days_to_expiry <= 21:
            return True, (
                f"⏰ DTE FLOOR: {days_to_expiry} DTE ≤ 21 — MUST exit or roll "
                f"(gamma risk management, Tastytrade rule)"
            )

        # Rule 2: Profit target reached
        if current_profit_pct >= profit_target.adjusted_target_pct:
            target_type = profit_target.capital_scarcity_level
            return True, (
                f"💰 PROFIT TARGET: {current_profit_pct:.1f}% ≥ {profit_target.adjusted_target_pct:.0f}% "
                f"({target_type} capital). "
                f"Base: {profit_target.base_target_pct:.0f}%, Adjusted: {profit_target.adjusted_target_pct:.0f}%"
            )

        # Rule 3: Hold condition
        return False, (
            f"HOLD: {current_profit_pct:.1f}% profit < {profit_target.adjusted_target_pct:.0f}% target, "
            f"{days_to_expiry} DTE > 21 floor"
        )


def calculate_adaptive_profit_target(
    open_position_count: int,
    current_cash_buffer_pct: float,
    days_to_expiry: int,
    max_open_positions: int = 8
) -> ProfitTarget:
    """
    Convenience function for quick profit target calculation

    Args:
        open_position_count: Current number of open option positions
        current_cash_buffer_pct: Current cash buffer as percentage of NLV
        days_to_expiry: Days until option expiry
        max_open_positions: Maximum allowed open positions

    Returns:
        ProfitTarget with adjusted target
    """
    calculator = AdaptiveProfitCalculator(max_open_positions=max_open_positions)
    return calculator.calculate_profit_target(
        open_position_count=open_position_count,
        current_cash_buffer_pct=current_cash_buffer_pct,
        days_to_expiry=days_to_expiry
    )


if __name__ == "__main__":
    # Test the adaptive profit calculator
    print("=" * 70)
    print("ADAPTIVE PROFIT TARGET TEST")
    print("=" * 70)

    calculator = AdaptiveProfitCalculator(
        max_open_positions=8,
        base_profit_target=50.0
    )

    # Test scenarios
    scenarios = [
        # (open_positions, cash_buffer_pct, dte, description)
        (2, 0.30, 35, "Very abundant capital"),
        (4, 0.25, 40, "Adequate capital"),
        (7, 0.12, 45, "Capital constrained"),
        (3, 0.09, 20, "Below DTE floor regardless"),
    ]

    for open_pos, cash_pct, dte, desc in scenarios:
        print(f"\n📊 SCENARIO: {desc}")
        print(f"   Positions: {open_pos}/8 ({open_pos/8:.1%} utilized)")
        print(f"   Cash Buffer: {cash_pct:.1%}")
        print(f"   DTE: {dte}")

        target = calculator.calculate_profit_target(
            open_position_count=open_pos,
            current_cash_buffer_pct=cash_pct,
            days_to_expiry=dte
        )

        print(f"\n   → PROFIT TARGET: {target.adjusted_target_pct:.0f}%")
        print(f"      Base: {target.base_target_pct:.0f}% | Adjusted: {target.adjusted_target_pct:.0f}%")
        print(f"      Scarcity Level: {target.capital_scarcity_level}")
        print(f"      Reason: {target.reason}")

    print("\n" + "=" * 70)
    print("✅ Adaptive profit target test complete")
