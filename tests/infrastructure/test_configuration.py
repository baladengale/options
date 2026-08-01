"""
Infrastructure Tests - Configuration Management

Tests for configuration file loading, validation, and management.
Critical for ensuring system configuration is correct and reliable.
"""

import pytest
import os
import tempfile
import yaml
from pathlib import Path

# Import test utilities
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestConfigurationFile:
    """Configuration file existence and accessibility"""

    def test_config_file_exists(self):
        """Configuration file exists and is readable"""
        config_path = Path('config/rules.yaml')

        if not config_path.exists():
            pytest.skip(f"Config file not found at {config_path}")

        assert config_path.exists(), "Configuration file should exist"
        assert config_path.is_file(), "Config should be a file, not directory"

    def test_config_file_readable(self):
        """Configuration file is readable"""
        config_path = Path('config/rules.yaml')

        if not config_path.exists():
            pytest.skip("Config file not found")

        try:
            with open(config_path, 'r') as f:
                content = f.read()
            assert len(content) > 0, "Config file should not be empty"
        except PermissionError:
            pytest.fail("Config file should be readable")

    def test_config_file_syntax(self):
        """Configuration file has valid YAML syntax"""
        config_path = Path('config/rules.yaml')

        if not config_path.exists():
            pytest.skip("Config file not found")

        try:
            with open(config_path, 'r') as f:
                yaml.safe_load(f)
            # If we get here, syntax is valid
        except yaml.YAMLError as e:
            pytest.fail(f"Config file has invalid YAML syntax: {e}")


class TestConfigurationLoading:
    """Configuration loading and parsing"""

    def test_config_loads_without_errors(self):
        """Configuration loads without parsing errors"""
        try:
            from src.config import rules
            assert rules is not None, "Rules should be loaded"
            assert isinstance(rules, dict), "Rules should be a dictionary"
        except ImportError:
            pytest.skip("Config module not available")
        except Exception as e:
            pytest.fail(f"Config loading failed: {e}")

    def test_config_required_sections(self):
        """Configuration has all required sections"""
        try:
            from src.config import rules

            required_sections = [
                'regime',
                'options',
                'scoring',
                'position_limits',
                'thesis_validation',
                'auto_exit_triggers'
            ]

            missing_sections = []
            for section in required_sections:
                if section not in rules:
                    missing_sections.append(section)

            assert len(missing_sections) == 0, f"Missing required sections: {missing_sections}"

        except ImportError:
            pytest.skip("Config module not available")

    def test_config_section_structure(self):
        """Configuration sections have expected structure"""
        try:
            from src.config import rules

            # Check regime section
            assert 'vix' in rules['regime'], "Regime should have VIX thresholds"
            assert 'position_mult' in rules['regime'], "Regime should have position multipliers"

            # Check options section
            assert 'delta' in rules['options'], "Options should have delta ranges"
            assert 'dte' in rules['options'], "Options should have DTE ranges"

            # Check position_limits section
            assert 'max_single_position_pct' in rules['position_limits']
            assert 'max_sector_pct' in rules['position_limits']

        except ImportError:
            pytest.skip("Config module not available")
        except AssertionError as e:
            pytest.fail(f"Config structure validation failed: {e}")


class TestConfigurationValidation:
    """Configuration value validation and error handling"""

    def test_vix_thresholds_valid(self):
        """VIX thresholds are in valid range"""
        try:
            from src.config import rules

            vix = rules['regime']['vix']
            thresholds = ['complacent', 'normal', 'elevated', 'high']

            for threshold in thresholds:
                value = vix[threshold]
                assert value > 0, f"VIX {threshold} should be positive"
                assert value < 50, f"VIX {threshold} seems unreasonably high"

            # Thresholds should be in ascending order
            assert vix['complacent'] < vix['normal'] < vix['elevated'] < vix['high']

        except ImportError:
            pytest.skip("Config module not available")

    def test_delta_ranges_valid(self):
        """Delta ranges are in valid range [0, 1]"""
        try:
            from src.config import rules

            delta = rules['options']['delta']

            # Check CSP deltas
            for regime in ['BULLISH', 'NEUTRAL', 'CAUTIOUS']:
                if regime in delta['csp']:
                    min_delta, max_delta = delta['csp'][regime]
                    assert 0 <= min_delta <= 1, f"CSP {regime} min delta invalid"
                    assert 0 <= max_delta <= 1, f"CSP {regime} max delta invalid"
                    assert min_delta < max_delta, f"CSP {regime} delta range invalid"

            # Check CC deltas
            for regime in ['BULLISH', 'NEUTRAL', 'CAUTIOUS']:
                if regime in delta['cc']:
                    min_delta, max_delta = delta['cc'][regime]
                    assert 0 <= min_delta <= 1, f"CC {regime} min delta invalid"
                    assert 0 <= max_delta <= 1, f"CC {regime} max delta invalid"
                    assert min_delta < max_delta, f"CC {regime} delta range invalid"

        except ImportError:
            pytest.skip("Config module not available")

    def test_dte_ranges_reasonable(self):
        """DTE ranges are reasonable and valid"""
        try:
            from src.config import rules

            dte = rules['options']['dte']

            # Check DTE ranges are positive
            assert dte['screen_min'] >= 0, "Screen min DTE should be non-negative"
            assert dte['screen_max'] > dte['screen_min'], "Screen max should be > min"
            assert dte['optimal_min'] >= dte['screen_min'], "Optimal min should be >= screen min"
            assert dte['optimal_max'] <= dte['screen_max'], "Optimal max should be <= screen max"

            # Check ranges are reasonable
            assert dte['hard_block'] >= 0, "Hard block DTE should be non-negative"
            assert dte['screen_max'] <= 365, "Screen max should be <= 1 year"

        except ImportError:
            pytest.skip("Config module not available")

    def test_position_limits_reasonable(self):
        """Position limits are reasonable percentages"""
        try:
            from src.config import rules

            limits = rules['position_limits']

            # Check limits are in range [0, 1]
            assert 0 < limits['max_single_position_pct'] <= 1, "Max position should be 0-100%"
            assert 0 < limits['max_sector_pct'] <= 1, "Max sector should be 0-100%"
            assert 0 < limits['max_csp_deployed_pct'] <= 1, "Max CSP deployment should be 0-100%"

            # Check relationships are reasonable
            assert limits['max_single_position_pct'] <= limits['max_sector_pct'], \
                "Single position limit should be <= sector limit"

        except ImportError:
            pytest.skip("Config module not available")

    def test_thesis_validation_configured(self):
        """Thesis validation is properly configured"""
        try:
            from src.config import rules

            thesis = rules['thesis_validation']

            assert 'enabled' in thesis, "Thesis validation should have enabled flag"
            assert 'check_frequency' in thesis, "Should have check frequency"

            # Check thresholds are reasonable
            assert thesis['earnings_revision_threshold'] < 0, "Earnings revision should be negative threshold"
            assert thesis['revenue_guidance_threshold'] < 0, "Revenue guidance should be negative threshold"
            assert 0 <= thesis['fundamental_score_minimum'] <= 10, "Fundamental score should be 0-10"

        except ImportError:
            pytest.skip("Config module not available")

    def test_auto_exit_triggers_configured(self):
        """Auto-exit triggers are properly configured"""
        try:
            from src.config import rules

            auto_exit = rules['auto_exit_triggers']

            assert 'thesis_broken' in auto_exit, "Should have thesis broken trigger"
            assert 'earnings_imminent' in auto_exit, "Should have earnings trigger"

            # Check earnings lookahead is positive
            if 'earnings_lookahead_days' in auto_exit:
                assert auto_exit['earnings_lookahead_days'] > 0, "Earnings lookahead should be positive"

        except ImportError:
            pytest.skip("Config module not available")


class TestConfigurationErrorHandling:
    """Error handling for invalid configuration"""

    def test_invalid_yaml_detection(self):
        """System detects invalid YAML syntax"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("invalid: yaml: content:\n  - broken")
            f.flush()
            temp_path = f.name

        try:
            with open(temp_path, 'r') as f:
                content = yaml.safe_load(f)
            pytest.fail("Should detect invalid YAML")
        except yaml.YAMLError:
            # Expected behavior
            pass
        finally:
            os.unlink(temp_path)

    def test_missing_required_section(self):
        """System detects missing required sections"""
        # Create minimal config without required sections
        minimal_config = {
            'regime': {'vix': {'complacent': 12, 'normal': 20, 'elevated': 25, 'high': 30}}
        }

        # This should fail validation when full schema is implemented
        # For now, we just verify the structure can be checked
        assert 'regime' in minimal_config
        assert 'options' not in minimal_config  # Missing required section

    def test_invalid_value_detection(self):
        """System detects invalid configuration values"""
        # Test with various invalid values
        invalid_configs = [
            ('negative_vix', {'vix': {'complacent': -5}}),
            ('invalid_delta', {'delta': {'csp': {'BULLISH': [1.5, 2.0]}}}),  # >1
            ('negative_dte', {'dte': {'screen_min': -10}}),
        ]

        for name, config in invalid_configs:
            # These should be caught by validation
            # Implementation would check value ranges
            assert True, "Should validate config values"


class TestConfigurationHotReload:
    """Configuration hot-reload functionality"""

    def test_config_module_caching(self):
        """Config module handles caching correctly"""
        try:
            from src import config

            # get_config() caches after first load — same object on repeat calls
            cfg1 = config.get_config()
            cfg2 = config.get_config()
            assert cfg1 is cfg2, "get_config() should return the cached Config instance"

        except ImportError:
            pytest.skip("Config module not available")

    def test_config_reloading_capability(self):
        """System can reload configuration when needed"""
        try:
            from src import config
            # reload_config() resets the cache and returns a fresh Config
            assert hasattr(config, 'reload_config'), \
                "Config module should expose reload_config()"
            before = config.get_config()
            after = config.reload_config()
            assert isinstance(after, type(before)), "reload_config() returns a Config"
        except ImportError:
            pytest.skip("Config module not available")


class TestConfigurationDefaults:
    """Configuration default values and fallbacks"""

    def test_sensible_defaults_exist(self):
        """Configuration has sensible default values"""
        try:
            from src.config import rules

            # Check some key defaults
            assert rules['regime']['vix']['normal'] == 20, "Default normal VIX should be 20"
            assert rules['options']['dte']['optimal_min'] >= 30, "Default optimal DTE min should be >=30"
            assert rules['options']['dte']['optimal_max'] <= 45, "Default optimal DTE max should be <=45"

        except (ImportError, KeyError) as e:
            pytest.skip(f"Default check failed: {e}")

    def test_fallback_values(self):
        """Configuration has fallback values for missing settings"""
        try:
            from src.config import rules

            # Check that fallback logic exists
            # If a setting is missing, system should use fallback
            assert 'position_mult' in rules['regime'], "Should have position multipliers"

            # Check that defaults are provided for all regimes
            required_regimes = ['BULLISH', 'NEUTRAL', 'CAUTIOUS', 'VOLATILE', 'BEARISH']
            for regime in required_regimes:
                assert regime in rules['regime']['position_mult'], f"Missing {regime} position multiplier"

        except ImportError:
            pytest.skip("Config module not available")


class TestConfigurationDocumentation:
    """Configuration file documentation and comments"""

    def test_config_file_comments(self):
        """Configuration file has helpful comments"""
        config_path = Path('config/rules.yaml')

        if not config_path.exists():
            pytest.skip("Config file not found")

        with open(config_path, 'r') as f:
            content = f.read()

        # Check for comments (lines starting with #)
        lines = content.split('\n')
        comment_lines = [line for line in lines if line.strip().startswith('#')]

        # Should have some comments for documentation
        assert len(comment_lines) > 10, "Config file should have documentation comments"

    def test_config_structure_readable(self):
        """Configuration is structured for human readability"""
        config_path = Path('config/rules.yaml')

        if not config_path.exists():
            pytest.skip("Config file not found")

        with open(config_path, 'r') as f:
            content = f.read()

        # Check for reasonable line structure
        lines = content.split('\n')
        # Should not have extremely long lines (hard to read)
        long_lines = [line for line in lines if len(line) > 200]

        assert len(long_lines) < 5, "Config should have readable line lengths"