"""Whether an article computes, or only recites.

The writer may only calculate with registered figures, so "add a worked
example" is only enforceable if someone can tell, in code, that one exists.
This is that someone.
"""

from __future__ import annotations

from app.normalize import arithmetic_in
from app.seo import defects
from tests.unit.test_seo import CONFIG, INDEX, draft


def test_an_operation_between_two_figures_counts():
    assert arithmetic_in("7 × 56.9 = 398 V")
    assert arithmetic_in("۴۵۰ تقسیم بر ۵۶.۹ می‌شود ۷.۹")
    assert arithmetic_in("۴۵۰ تقسیم‌بر ۵۶.۹")  # ZWNJ instead of a space
    assert arithmetic_in("8 ÷ 0.8 = 10 kWh")
    assert arithmetic_in("۵۲.۳۱ ضربدر (۱ + ۰.۰۰۲۵ × ۳۵)")


def test_a_range_or_a_date_is_not_a_calculation():
    """A page of prices and a date line would otherwise pass as a worked example."""
    assert not arithmetic_in("yield falls by 10-30% a year")
    assert not arithmetic_in("published 2026/09/11")
    assert not arithmetic_in("between ۱۴ تا ۲۰ amps")
    assert not arithmetic_in("a 580 W panel with 52.31 V open-circuit voltage")


def test_a_body_that_only_states_figures_is_a_major_defect():
    body = "## Sizing\n\nA 5 kWh battery at 80% depth of discharge. That is the figure to buy.\n"
    found = {i.issue_id: i for i in defects(draft(body=body), INDEX, [], CONFIG)}
    assert found["CALC-MISSING"].severity == "major"
    assert "2 figure(s)" in found["CALC-MISSING"].problem


def test_a_body_that_computes_is_left_alone():
    ids = [i.issue_id for i in defects(draft(), INDEX, [], CONFIG)]
    assert "CALC-MISSING" not in ids
