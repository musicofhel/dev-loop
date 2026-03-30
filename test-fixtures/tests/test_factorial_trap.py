"""Pre-seeded tests for TB-2 failure-to-retry validation.

These tests are copied into the OOTestProject1 worktree BEFORE the agent runs.
They include edge cases that first-attempt implementations commonly miss:
- TypeError for float input (agents often only check for negative)
- Exact error message matching (agents often use different wording)
- Large number correctness (factorial(20))
"""

import pytest

from oo_test_project.calculator import factorial


class TestFactorial:
    """Tests for the factorial function."""

    def test_factorial_zero(self):
        assert factorial(0) == 1

    def test_factorial_one(self):
        assert factorial(1) == 1

    def test_factorial_five(self):
        assert factorial(5) == 120

    def test_factorial_ten(self):
        assert factorial(10) == 3628800

    def test_factorial_twenty(self):
        """Large factorial — verify exact integer result."""
        assert factorial(20) == 2432902008176640000

    def test_factorial_negative_raises(self):
        """Negative input must raise ValueError with specific message."""
        with pytest.raises(ValueError, match="negative"):
            factorial(-1)

    def test_factorial_negative_large(self):
        with pytest.raises(ValueError, match="negative"):
            factorial(-100)

    def test_factorial_float_raises(self):
        """Float input must raise TypeError with specific message."""
        with pytest.raises(TypeError, match="integer"):
            factorial(1.5)

    def test_factorial_float_whole_number(self):
        """Even 5.0 should raise TypeError — must be int type."""
        with pytest.raises(TypeError, match="integer"):
            factorial(5.0)
