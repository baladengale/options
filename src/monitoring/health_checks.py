"""
System Health Check Module

Provides comprehensive health checking for the options trading system.
Monitors critical components and provides status reporting.
"""

from datetime import datetime
from typing import Dict, List, Callable, Any
import logging
import sqlite3
import os

log = logging.getLogger(__name__)


class HealthCheckResult:
    """Result of a single health check"""

    def __init__(
        self,
        name: str,
        status: str,
        message: str = "",
        duration_ms: float = 0,
        severity: str = "warning"
    ):
        self.name = name
        self.status = status  # 'pass', 'fail', 'error'
        self.message = message
        self.duration_ms = duration_ms
        self.severity = severity  # 'critical', 'warning', 'info'
        self.timestamp = datetime.now()

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        return {
            'name': self.name,
            'status': self.status,
            'message': self.message,
            'duration_ms': self.duration_ms,
            'severity': self.severity,
            'timestamp': self.timestamp.isoformat()
        }


class SystemHealthChecker:
    """
    Comprehensive health checking system

    Monitors all critical system components and provides
    aggregated health status with actionable alerts.
    """

    def __init__(self):
        self.checks: List[Callable] = []
        self.last_check_time = None
        self.last_results = None

    def add_check(self, check_function: Callable, name: str = None, severity: str = "warning"):
        """
        Add a health check to the system

        Args:
            check_function: Function that runs the health check
            name: Descriptive name for the check
            severity: 'critical', 'warning', or 'info'
        """
        check_name = name or check_function.__name__
        self.checks.append({
            'function': check_function,
            'name': check_name,
            'severity': severity
        })

    def run_all_checks(self) -> Dict[str, Any]:
        """
        Run all registered health checks

        Returns:
            Dict with overall status and individual check results
        """
        start_time = datetime.now()
        results = {
            'timestamp': start_time.isoformat(),
            'overall_status': 'healthy',
            'checks': [],
            'summary': {
                'total': 0,
                'passed': 0,
                'failed': 0,
                'errors': 0,
                'critical_failures': 0
            }
        }

        for check in self.checks:
            try:
                check_start = datetime.now()
                result = check['function']()
                check_duration = (datetime.now() - check_start).total_seconds() * 1000

                if isinstance(result, HealthCheckResult):
                    health_result = result
                elif isinstance(result, bool):
                    health_result = HealthCheckResult(
                        name=check['name'],
                        status='pass' if result else 'fail',
                        duration_ms=check_duration,
                        severity=check['severity']
                    )
                elif isinstance(result, dict):
                    health_result = HealthCheckResult(
                        name=check['name'],
                        status=result.get('status', 'pass'),
                        message=result.get('message', ''),
                        duration_ms=check_duration,
                        severity=check['severity']
                    )
                else:
                    health_result = HealthCheckResult(
                        name=check['name'],
                        status='pass' if result else 'fail',
                        duration_ms=check_duration,
                        severity=check['severity']
                    )

                results['checks'].append(health_result.to_dict())

                # Update summary
                results['summary']['total'] += 1
                if health_result.status == 'pass':
                    results['summary']['passed'] += 1
                elif health_result.status == 'fail':
                    results['summary']['failed'] += 1
                    if health_result.severity == 'critical':
                        results['summary']['critical_failures'] += 1
                        results['overall_status'] = 'critical'
                elif health_result.status == 'error':
                    results['summary']['errors'] += 1
                    results['summary']['failed'] += 1
                    if health_result.severity == 'critical':
                        results['summary']['critical_failures'] += 1
                        results['overall_status'] = 'critical'

            except Exception as e:
                # Check itself raised an error
                results['checks'].append({
                    'name': check['name'],
                    'status': 'error',
                    'message': str(e),
                    'duration_ms': 0,
                    'severity': check['severity'],
                    'timestamp': datetime.now().isoformat()
                })
                results['summary']['total'] += 1
                results['summary']['errors'] += 1
                results['summary']['failed'] += 1

                if check['severity'] == 'critical':
                    results['summary']['critical_failures'] += 1
                    results['overall_status'] = 'critical'

        # Determine overall status
        if results['summary']['critical_failures'] > 0:
            results['overall_status'] = 'critical'
        elif results['summary']['failed'] > 0:
            results['overall_status'] = 'degraded'
        elif results['summary']['errors'] > 0:
            results['overall_status'] = 'degraded'
        else:
            results['overall_status'] = 'healthy'

        results['duration_ms'] = (datetime.now() - start_time).total_seconds() * 1000

        self.last_check_time = datetime.now()
        self.last_results = results

        return results

    def get_status_summary(self) -> str:
        """Get a human-readable status summary"""
        if not self.last_results:
            return "No health checks have been run"

        status = self.last_results['overall_status']
        summary = self.last_results['summary']

        return (
            f"System Status: {status.upper()}\n"
            f"Total Checks: {summary['total']}\n"
            f"Passed: {summary['passed']}\n"
            f"Failed: {summary['failed']}\n"
            f"Errors: {summary['errors']}\n"
            f"Critical Failures: {summary['critical_failures']}"
        )


# Predefined health check functions

def check_database_connectivity() -> HealthCheckResult:
    """Check database connectivity"""
    try:
        db_path = 'db/options.db'
        if not os.path.exists(db_path):
            return HealthCheckResult(
                name="database_connectivity",
                status="fail",
                message=f"Database file not found at {db_path}",
                severity="critical"
            )

        conn = sqlite3.connect(db_path)
        conn.execute("SELECT 1")
        conn.close()

        return HealthCheckResult(
            name="database_connectivity",
            status="pass",
            message="Database connection successful",
            severity="critical"
        )

    except Exception as e:
        return HealthCheckResult(
            name="database_connectivity",
            status="error",
            message=f"Database connection failed: {str(e)}",
            severity="critical"
        )


def check_config_validity() -> HealthCheckResult:
    """Check configuration file validity"""
    try:
        from src.config import rules

        # Check if required sections exist
        required_sections = ['regime', 'options', 'scoring', 'position_limits']
        missing_sections = [s for s in required_sections if s not in rules]

        if missing_sections:
            return HealthCheckResult(
                name="config_validity",
                status="fail",
                message=f"Missing config sections: {missing_sections}",
                severity="critical"
            )

        return HealthCheckResult(
            name="config_validity",
            status="pass",
            message="Configuration valid and complete",
            severity="critical"
        )

    except ImportError:
        return HealthCheckResult(
            name="config_validity",
            status="error",
            message="Cannot import configuration module",
            severity="critical"
        )
    except Exception as e:
        return HealthCheckResult(
            name="config_validity",
            status="error",
            message=f"Configuration check failed: {str(e)}",
            severity="critical"
        )


def check_disk_space() -> HealthCheckResult:
    """Check disk space availability"""
    try:
        import shutil
        total, used, free = shutil.disk_usage("/")
        free_percent = (free / total) * 100

        if free_percent < 5:
            return HealthCheckResult(
                name="disk_space",
                status="fail",
                message=f"Critical: Only {free_percent:.1f}% disk space free",
                severity="warning"
            )
        elif free_percent < 10:
            return HealthCheckResult(
                name="disk_space",
                status="fail",
                message=f"Warning: Only {free_percent:.1f}% disk space free",
                severity="warning"
            )

        return HealthCheckResult(
            name="disk_space",
            status="pass",
            message=f"Disk space OK: {free_percent:.1f}% free",
            severity="warning"
        )

    except Exception as e:
        return HealthCheckResult(
            name="disk_space",
            status="error",
            message=f"Disk space check failed: {str(e)}",
            severity="warning"
        )


def check_memory_usage() -> HealthCheckResult:
    """Check memory usage is reasonable"""
    try:
        import psutil
        memory = psutil.virtual_memory()
        memory_percent = memory.percent

        if memory_percent > 95:
            return HealthCheckResult(
                name="memory_usage",
                status="fail",
                message=f"Critical: Memory usage at {memory_percent:.1f}%",
                severity="warning"
            )
        elif memory_percent > 85:
            return HealthCheckResult(
                name="memory_usage",
                status="fail",
                message=f"Warning: Memory usage at {memory_percent:.1f}%",
                severity="warning"
            )

        return HealthCheckResult(
            name="memory_usage",
            status="pass",
            message=f"Memory usage OK: {memory_percent:.1f}%",
            severity="warning"
        )

    except ImportError:
        return HealthCheckResult(
            name="memory_usage",
            status="pass",
            message="psutil not available, skipping memory check",
            severity="info"
        )
    except Exception as e:
        return HealthCheckResult(
            name="memory_usage",
            status="error",
            message=f"Memory check failed: {str(e)}",
            severity="warning"
        )


def check_log_file_health() -> HealthCheckResult:
    """Check log files are accessible and not too large"""
    try:
        import glob

        log_files = glob.glob("logs/*.log") + glob.glob("logs/*.log.*")

        if not log_files:
            return HealthCheckResult(
                name="log_file_health",
                status="pass",
                message="No log files found (system may not be running)",
                severity="info"
            )

        # Check if any log file is too large (>100MB)
        large_logs = []
        for log_file in log_files:
            try:
                size_mb = os.path.getsize(log_file) / (1024 * 1024)
                if size_mb > 100:
                    large_logs.append(f"{log_file}: {size_mb:.1f}MB")
            except:
                pass

        if large_logs:
            return HealthCheckResult(
                name="log_file_health",
                status="fail",
                message=f"Large log files: {large_logs}",
                severity="warning"
            )

        return HealthCheckResult(
            name="log_file_health",
            status="pass",
            message=f"Log files OK: {len(log_files)} files",
            severity="warning"
        )

    except Exception as e:
        return HealthCheckResult(
            name="log_file_health",
            status="error",
            message=f"Log file check failed: {str(e)}",
            severity="warning"
        )


def create_default_health_checker() -> SystemHealthChecker:
    """
    Create a health checker with default checks

    Returns:
        SystemHealthChecker with standard health checks registered
    """
    checker = SystemHealthChecker()

    # Add default checks in order of priority
    checker.add_check(check_database_connectivity, "database_connectivity", "critical")
    checker.add_check(check_config_validity, "config_validity", "critical")
    checker.add_check(check_disk_space, "disk_space", "warning")
    checker.add_check(check_memory_usage, "memory_usage", "warning")
    checker.add_check(check_log_file_health, "log_file_health", "info")

    return checker


if __name__ == "__main__":
    # Run health checks when executed directly
    import sys

    print("=" * 60)
    print("SYSTEM HEALTH CHECK")
    print("=" * 60)

    checker = create_default_health_checker()
    results = checker.run_all_checks()

    print(f"\nTimestamp: {results['timestamp']}")
    print(f"Overall Status: {results['overall_status'].upper()}")
    print(f"Total Checks: {results['summary']['total']}")
    print(f"Passed: {results['summary']['passed']}")
    print(f"Failed: {results['summary']['failed']}")
    print(f"Errors: {results['summary']['errors']}")
    print(f"Critical Failures: {results['summary']['critical_failures']}")
    print(f"Duration: {results['duration_ms']:.0f}ms")

    print("\nDetailed Results:")
    print("-" * 60)

    for check in results['checks']:
        status_symbol = "✓" if check['status'] == 'pass' else "✗"
        print(f"{status_symbol} {check['name']}: {check['status'].upper()}")

        if check['message']:
            print(f"  → {check['message']}")

        if check['status'] != 'pass':
            print(f"  → Severity: {check['severity'].upper()}")

    print("-" * 60)

    # Exit with appropriate code
    if results['overall_status'] == 'critical':
        sys.exit(2)
    elif results['overall_status'] == 'degraded':
        sys.exit(1)
    else:
        sys.exit(0)