"""
Realistic Position Limits - Quality-Adjusted & Context-Dependent

This module implements staged guardrails that acknowledge reality while preventing
disaster. The limits progress from emergency → target → comfort based on cash buffer
and portfolio health.

Research-backed approach:
- Emergency limits address current situation (V concentration, CSP over-deployment)
- Target limits are sustainable for quality stocks
- Comfort limits allow natural concentration for exceptional businesses (V, AAPL)
"""

from dataclasses import dataclass
from typing import List, Dict, Optional
import logging

from src.config import get_config

log = logging.getLogger(__name__)


class PositionLimits:
    """
    Realistic Position Limits - Staged by Recovery Progress

    These limits acknowledge current reality while guiding recovery:
    - EMERGENCY: Current situation (accept V concentration, fix CSP deployment)
    - TARGET: Sustainable Wheel strategy (quality stocks, reasonable deployment)
    - COMFORT: Optimal execution (strong cash buffer, flexible deployment)
    """

    # === EMERGENCY LIMITS (Current Situation - Week 1-2) ===
    # Accept your current reality, reduce disaster risk

    MAX_POSITION_PCT_EMERGENCY = 0.15    # 15% - Need reduction ASAP
    MAX_SECTOR_PCT_EMERGENCY = 0.40      # 40% - Accept financials for V temporarily

    # CSP deployment: CRITICAL because current deployment is 66% with 9% cash
    MAX_CSP_LIABILITY_CRITICAL = 0.15  # 15% - Emergency: stop new CSPs immediately

    # Complexity limits: Accept current learning curve
    MAX_TOTAL_POSITIONS_EMERGENCY = 10   # 10 positions - You're at 10, OK
    MAX_MONTHLY_ORDERS_EMERGENCY = 15    # 15 orders - tightened from 20 (spec §10)

    # === TARGET LIMITS (Sustainable Wheel - Month 2-3) ===
    # Where you want to be after emergency fixes

    MAX_POSITION_PCT_TARGET = 0.25       # 25% - Reasonable for core holdings
    MAX_SECTOR_PCT_TARGET = 0.35         # 35% - Allows natural concentration
    MAX_POSITION_PCT_QUALITY = 0.30      # 30% - For exceptional quality (V, AAPL)

    # CSP deployment: Reasonable with adequate cash buffer
    MAX_CSP_LIABILITY_WARNING = 0.25     # 25% - After cash > 15%
    MAX_CSP_LIABILITY_TARGET = 0.35     # 35% - After cash > 20%

    # Complexity: Manageable for systematic execution
    MAX_TOTAL_POSITIONS_TARGET = 10      # 10 positions - Manageable
    MAX_MONTHLY_ORDERS_TARGET = 30      # 30 orders - True Wheel frequency

    # === COMFORT LIMITS (Optimal Execution - Month 3+) ===
    # When cash buffer is strong, allow more flexibility

    MAX_CSP_LIABILITY_COMFORT = 0.50    # 50% - With 30% cash buffer
    MAX_CASH_BUFFER_CRITICAL = 0.10     # 10% - Below this is emergency
    MAX_CASH_BUFFER_WARNING = 0.15      # 15% - Minimum acceptable
    MAX_CASH_BUFFER_TARGET = 0.20       # 20% - Good buffer
    MAX_CASH_BUFFER_COMFORT = 0.30      # 30% - Strong buffer

    # Cash-buffer based CSP deployment
    CSP_LIMIT_10PCT_CASH = 0.15         # 15% CSP max at 10% cash
    CSP_LIMIT_15PCT_CASH = 0.25         # 25% CSP max at 15% cash
    CSP_LIMIT_20PCT_CASH = 0.35         # 35% CSP max at 20% cash
    CSP_LIMIT_30PCT_CASH = 0.50         # 50% CSP max at 30% cash


@dataclass
class GuardrailViolation:
    """A guardrail violation with context and remediation"""
    guardrail_type: str
    current_value: float
    limit_value: float
    severity: str  # WARN, BLOCK, CRITICAL
    message: str
    required_action: str
    stage: str  # EMERGENCY, TARGET, COMFORT


class GuardrailChecker:
    """
    Guardrail checker with staged limits

    This class implements realistic guardrails that:
    1. Acknowledge current situation (emergency limits)
    2. Guide toward sustainable targets (target limits)
    3. Allow flexibility for quality stocks (comfort limits)
    """

    def __init__(
        self,
        net_liquidation: float,
        cash: float,
        buying_power: float,
        open_positions: int = 0,
        monthly_orders: int = 0,
        csp_liability: float = 0.0
    ):
        """
        Initialize guardrail checker

        Args:
            net_liquidation: Total portfolio value
            cash: Available cash
            buying_power: Total buying power
            open_positions: Number of open option positions
            monthly_orders: Number of orders this month
            csp_liability: Total cash needed if all CSPs assign
        """
        self.net_liquidation = net_liquidation
        self.cash = cash
        self.buying_power = buying_power
        self.open_positions = open_positions
        self.monthly_orders = monthly_orders
        self.csp_liability = csp_liability

        self.cash_buffer_pct = cash / net_liquidation if net_liquidation > 0 else 0
        self.csp_deployment_pct = csp_liability / net_liquidation if net_liquidation > 0 else 0

    @staticmethod
    def _gl(key: str, fallback):
        """Read a guardrail limit from config/rules.yaml → guardrail_limits,
        falling back to the hardcoded constant if not configured."""
        try:
            cfg = get_config()
            return cfg.guardrail_limits(key, fallback)
        except Exception:
            return fallback

    @classmethod
    def _cash_critical(cls):
        return cls._gl('cash_buffer_critical', PositionLimits.MAX_CASH_BUFFER_CRITICAL)

    @classmethod
    def _cash_warning(cls):
        return cls._gl('cash_buffer_warning', PositionLimits.MAX_CASH_BUFFER_WARNING)

    @classmethod
    def _cash_target(cls):
        return cls._gl('cash_buffer_target', PositionLimits.MAX_CASH_BUFFER_TARGET)

    def get_current_stage(self) -> str:
        """
        Determine current recovery stage based on cash buffer

        Returns:
            'EMERGENCY', 'TARGET', or 'COMFORT'
        """
        if self.cash_buffer_pct < self._cash_critical():
            return 'EMERGENCY'
        elif self.cash_buffer_pct < self._cash_target():
            return 'TARGET'
        else:
            return 'COMFORT'

    def get_position_limit(self) -> float:
        """
        Get position concentration limit based on stage and stock quality

        Returns:
            Maximum position size as percentage of net liquidation
        """
        stage = self.get_current_stage()

        if stage == 'EMERGENCY':
            return self._gl('max_position_pct_emergency', PositionLimits.MAX_POSITION_PCT_EMERGENCY)
        elif stage == 'TARGET':
            return self._gl('max_position_pct_target', PositionLimits.MAX_POSITION_PCT_TARGET)
        else:  # COMFORT
            return self._gl('max_position_pct_quality', PositionLimits.MAX_POSITION_PCT_QUALITY)

    def get_sector_limit(self) -> float:
        """
        Get sector concentration limit based on stage

        Returns:
            Maximum sector concentration as percentage of net liquidation
        """
        stage = self.get_current_stage()

        if stage == 'EMERGENCY':
            return self._gl('max_sector_pct_emergency', PositionLimits.MAX_SECTOR_PCT_EMERGENCY)
        else:  # TARGET or COMFORT
            return self._gl('max_sector_pct_target', PositionLimits.MAX_SECTOR_PCT_TARGET)

    def get_csp_limit(self) -> float:
        """
        Get CSP deployment limit based on cash buffer

        Returns:
            Maximum CSP deployment as percentage of net liquidation
        """
        cash_pct = self.cash_buffer_pct

        if cash_pct < self._cash_critical():
            return self._gl('max_csp_liability_critical', PositionLimits.CSP_LIMIT_10PCT_CASH)
        elif cash_pct < self._cash_warning():
            return self._gl('max_csp_liability_warning', PositionLimits.CSP_LIMIT_15PCT_CASH)
        elif cash_pct < self._cash_target():
            return self._gl('max_csp_liability_target', PositionLimits.CSP_LIMIT_20PCT_CASH)
        else:
            return self._gl('max_csp_liability_comfort', PositionLimits.CSP_LIMIT_30PCT_CASH)

    def get_position_count_limit(self) -> int:
        """
        Get maximum position count based on stage

        Returns:
            Maximum number of open positions
        """
        stage = self.get_current_stage()

        if stage == 'EMERGENCY':
            return int(self._gl('max_total_positions_emergency', PositionLimits.MAX_TOTAL_POSITIONS_EMERGENCY))
        else:
            return int(self._gl('max_total_positions_target', PositionLimits.MAX_TOTAL_POSITIONS_TARGET))

    def get_monthly_order_limit(self) -> int:
        """
        Get maximum monthly orders based on stage

        Returns:
            Maximum orders per month
        """
        stage = self.get_current_stage()

        if stage == 'EMERGENCY':
            return int(self._gl('max_monthly_orders_emergency', PositionLimits.MAX_MONTHLY_ORDERS_EMERGENCY))
        else:
            return int(self._gl('max_monthly_orders_target', PositionLimits.MAX_MONTHLY_ORDERS_TARGET))

    def check_all_guardrails(self, positions: Optional[Dict] = None) -> List[GuardrailViolation]:
        """
        Check all guardrails and return violations

        Args:
            positions: Dict of positions {ticker: {market_value, sector}}

        Returns:
            List of GuardrailViolation objects
        """
        violations = []

        # === CHECK 1: Cash Buffer ===
        cash_critical = self._cash_critical()
        cash_warning = self._cash_warning()
        if self.cash_buffer_pct < cash_critical:
            violations.append(GuardrailViolation(
                guardrail_type="cash_buffer",
                current_value=self.cash_buffer_pct,
                limit_value=cash_critical,
                severity="CRITICAL",
                message=f"Cash buffer {self.cash_buffer_pct:.1%} < {cash_critical:.0%} - EMERGENCY",
                required_action="Build cash buffer immediately - add $2-3K/month via CC income",
                stage="EMERGENCY"
            ))
        elif self.cash_buffer_pct < cash_warning:
            violations.append(GuardrailViolation(
                guardrail_type="cash_buffer",
                current_value=self.cash_buffer_pct,
                limit_value=cash_warning,
                severity="WARN",
                message=f"Cash buffer {self.cash_buffer_pct:.1%} < {cash_warning:.0%} - Below minimum",
                required_action="Build cash to 15% minimum - monthly savings + CC income",
                stage="TARGET"
            ))

        # === CHECK 2: CSP Deployment ===
        csp_limit = self.get_csp_limit()
        if self.csp_deployment_pct > csp_limit:
            violations.append(GuardrailViolation(
                guardrail_type="csp_deployment",
                current_value=self.csp_deployment_pct,
                limit_value=csp_limit,
                severity="CRITICAL" if self.csp_deployment_pct > 0.50 else "BLOCK",
                message=f"CSP deployment {self.csp_deployment_pct:.1%} > {csp_limit:.0%} limit",
                required_action="Close 2-3 CSPs immediately to reduce liability, stop new CSPs until < limit",
                stage=self.get_current_stage()
            ))

        # === CHECK 3: Position Concentration ===
        if positions:
            position_limit = self.get_position_limit()
            for ticker, pos_data in positions.items():
                concentration = pos_data['market_value'] / self.net_liquidation
                if concentration > position_limit:
                    violations.append(GuardrailViolation(
                        guardrail_type="position_concentration",
                        current_value=concentration,
                        limit_value=position_limit,
                        severity="WARN",
                        message=f"{ticker} at {concentration:.1%} > {position_limit:.0%} limit",
                        required_action=f"Only CC allowed on {ticker} to reduce concentration, let existing expire",
                        stage=self.get_current_stage()
                    ))

        # === CHECK 4: Sector Concentration ===
        if positions:
            sector_limit = self.get_sector_limit()
            sector_breakdown = self._calculate_sector_concentration(positions)

            for sector, value in sector_breakdown.items():
                concentration = value / self.net_liquidation
                if concentration > sector_limit:
                    violations.append(GuardrailViolation(
                        guardrail_type="sector_concentration",
                        current_value=concentration,
                        limit_value=sector_limit,
                        severity="WARN",
                        message=f"{sector} at {concentration:.1%} > {sector_limit:.0%} limit",
                        required_action=f"Block new {sector} positions, allow existing to expire",
                        stage=self.get_current_stage()
                    ))

        # === CHECK 5: Position Count ===
        position_limit = self.get_position_count_limit()
        if self.open_positions > position_limit:
            violations.append(GuardrailViolation(
                guardrail_type="position_count",
                current_value=self.open_positions,
                limit_value=position_limit,
                severity="WARN",
                message=f"{self.open_positions} positions > {position_limit} limit",
                required_action="Allow existing to expire, no new positions until < limit",
                stage=self.get_current_stage()
            ))

        # === CHECK 6: Monthly Orders ===
        order_limit = self.get_monthly_order_limit()
        if self.monthly_orders > order_limit:
            violations.append(GuardrailViolation(
                guardrail_type="monthly_orders",
                current_value=self.monthly_orders,
                limit_value=order_limit,
                severity="BLOCK",
                message=f"{self.monthly_orders} orders > {order_limit} limit",
                required_action="Stop trading until next month - review strategy",
                stage=self.get_current_stage()
            ))

        return violations

    def check_new_trade(
        self,
        ticker: str,
        strategy: str,
        notional: float,
        sector: str,
        current_positions: Optional[Dict] = None
    ) -> tuple[bool, List[GuardrailViolation]]:
        """
        Check if a new trade would violate any guardrails

        Args:
            ticker: Stock ticker
            strategy: 'CC' or 'CSP'
            notional: Trade size (strike * 100 * contracts)
            sector: Stock sector
            current_positions: Current positions for concentration check

        Returns:
            (allowed: bool, violations: List[GuardrailViolation])
        """
        violations = []
        allowed = True

        # Simulate adding the new position
        new_concentration = notional / self.net_liquidation

        # For CSP, check if we can afford assignment
        if strategy == 'CSP':
            new_csp_liability = self.csp_liability + notional
            new_csp_deployment = new_csp_liability / self.net_liquidation
            csp_limit = self.get_csp_limit()

            if new_csp_deployment > csp_limit:
                violations.append(GuardrailViolation(
                    guardrail_type="csp_deployment",
                    current_value=new_csp_deployment,
                    limit_value=csp_limit,
                    severity="BLOCK",
                    message=f"New CSP would push deployment to {new_csp_deployment:.1%} > {csp_limit:.0%}",
                    required_action="Block new CSP - close existing CSPs first",
                    stage=self.get_current_stage()
                ))
                allowed = False

        # Check position concentration
        position_limit = self.get_position_limit()

        if current_positions and ticker in current_positions:
            current_mv = current_positions[ticker]['market_value']
            new_concentration = (current_mv + notional) / self.net_liquidation
        else:
            new_concentration = notional / self.net_liquidation

        if new_concentration > position_limit:
            violations.append(GuardrailViolation(
                guardrail_type="position_concentration",
                current_value=new_concentration,
                limit_value=position_limit,
                severity="BLOCK",
                message=f"New position would put {ticker} at {new_concentration:.1%} > {position_limit:.0%}",
                required_action=f"Block new {ticker} position - existing too large",
                stage=self.get_current_stage()
            ))
            allowed = False

        # Check sector concentration
        if current_positions:
            sector_limit = self.get_sector_limit()
            sector_breakdown = self._calculate_sector_concentration(current_positions)
            current_sector_mv = sector_breakdown.get(sector, 0)
            new_sector_deployment = (current_sector_mv + notional) / self.net_liquidation

            if new_sector_deployment > sector_limit:
                violations.append(GuardrailViolation(
                    guardrail_type="sector_concentration",
                    current_value=new_sector_deployment,
                    limit_value=sector_limit,
                    severity="BLOCK",
                    message=f"New position would put {sector} at {new_sector_deployment:.1%} > {sector_limit:.0%}",
                    required_action=f"Block new {sector} position - sector concentrated",
                    stage=self.get_current_stage()
                ))
                allowed = False

        # Check monthly order limit
        if self.monthly_orders >= self.get_monthly_order_limit():
            violations.append(GuardrailViolation(
                guardrail_type="monthly_orders",
                current_value=self.monthly_orders,
                limit_value=self.get_monthly_order_limit(),
                severity="BLOCK",
                message=f"Monthly order limit reached ({self.monthly_orders}/{self.get_monthly_order_limit()})",
                required_action="Wait until next month to open new positions",
                stage=self.get_current_stage()
            ))
            allowed = False

        return allowed, violations

    def _calculate_sector_concentration(self, positions: Dict) -> Dict[str, float]:
        """Calculate total market value by sector"""
        sector_breakdown = {}

        for ticker, pos_data in positions.items():
            sector = pos_data.get('sector', 'Other')
            mv = pos_data.get('market_value', 0)

            if sector not in sector_breakdown:
                sector_breakdown[sector] = 0
            sector_breakdown[sector] += mv

        return sector_breakdown

    def get_summary(self) -> Dict:
        """Get summary of current guardrail status"""
        return {
            "stage": self.get_current_stage(),
            "cash_buffer_pct": self.cash_buffer_pct,
            "csp_deployment_pct": self.csp_deployment_pct,
            "limits": {
                "position": self.get_position_limit(),
                "sector": self.get_sector_limit(),
                "csp": self.get_csp_limit(),
                "position_count": self.get_position_count_limit(),
                "monthly_orders": self.get_monthly_order_limit()
            },
            "current_values": {
                "cash": self.cash,
                "net_liquidation": self.net_liquidation,
                "open_positions": self.open_positions,
                "monthly_orders": self.monthly_orders,
                "csp_liability": self.csp_liability
            }
        }


if __name__ == "__main__":
    # Test the guardrails with current portfolio state
    print("=" * 60)
    print("REALISTIC GUARDRAILS TEST")
    print("=" * 60)

    # Current portfolio state (from docs)
    checker = GuardrailChecker(
        net_liquidation=238000,
        cash=21460,
        buying_power=120000,
        open_positions=10,
        monthly_orders=40,
        csp_liability=155000
    )

    print(f"\n📊 CURRENT STAGE: {checker.get_current_stage()}")
    print(f"💰 Cash Buffer: {checker.cash_buffer_pct:.1%}")
    print(f"📈 CSP Deployment: {checker.csp_deployment_pct:.1%}")

    print(f"\n📏 LIMITS:")
    print(f"  Position: {checker.get_position_limit():.0%}")
    print(f"  Sector: {checker.get_sector_limit():.0%}")
    print(f"  CSP: {checker.get_csp_limit():.0%}")
    print(f"  Positions: {checker.get_position_count_limit()}")
    print(f"  Monthly Orders: {checker.get_monthly_order_limit()}")

    # Simulate positions
    positions = {
        'V': {'market_value': 185000, 'sector': 'Financials'},
        'AAPL': {'market_value': 1200, 'sector': 'Technology'},
        'AMD': {'market_value': 3600, 'sector': 'Technology'}
    }

    violations = checker.check_all_guardrails(positions)

    print(f"\n🚨 VIOLATIONS: {len(violations)}")
    for v in violations:
        print(f"\n  {v.severity}: {v.message}")
        print(f"  Action: {v.required_action}")

    print("\n✅ Guardrails test complete")
