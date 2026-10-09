from datetime import date
from pathlib import Path

import pytest

from conftest import FakeCtx
from gies_bot.models.massmail_count import fair_prob, parse_archive, prob_at_least

PAGE = (Path(__file__).parent / "fixtures" / "massmail.html").read_text()
PARAMS = {"url": "https://example.test/archive", "start": "2026-10-07", "end": "2026-10-26",
          "threshold": 4, "rate_per_day": 0.12, "dispersion": 2.0}


def test_parser_keeps_duplicates_and_entries_without_a_time():
    entries = parse_archive(PAGE)
    assert len(entries) == 5
    assert entries[0] == {"date": date(2026, 10, 12), "time": "1:02 pm", "title": "Flu shots & you",
                          "href": "https://massmail.illinois.edu/massmail/3.html"}
    assert entries[2]["time"] == "" and entries[2]["date"] == date(2026, 10, 7)


def test_poisson_and_negative_binomial_hand_calcs():
    assert prob_at_least(3, 2.28, 1.0) == pytest.approx(0.399, abs=0.003)
    assert prob_at_least(3, 2.28, 2.0) == pytest.approx(0.367, abs=0.003)
    assert prob_at_least(0, 2.28, 2.0) == 1.0
    assert prob_at_least(1, 0.0, 2.0) == 0.0


def test_counts_window_entries_and_reports_each_once(at):
    result = fair_prob({}, PARAMS, FakeCtx({"archive": PAGE}, at(2026, 10, 13, 0)))
    assert result.data_ok and result.inputs["count"] == 3
    assert result.inputs["days_left"] == 14.0
    assert result.inputs["same_window_prior_years"][2025] == 1
    assert len(set(result.events)) == 3
    assert result.p_yes == pytest.approx(prob_at_least(1, 0.12 * 14, 2.0))


def test_window_closed_is_decided(at):
    assert fair_prob({}, PARAMS, FakeCtx({"archive": PAGE}, at(2026, 10, 27, 0))).p_yes == 0.0
    assert fair_prob({}, {**PARAMS, "threshold": 3}, FakeCtx({"archive": PAGE}, at(2026, 10, 27, 0))).p_yes == 1.0


def test_unparseable_or_stale_page_is_not_ok(at):
    assert not fair_prob({}, PARAMS, FakeCtx({"archive": "<html>redesigned</html>"}, at(2026, 10, 13, 0))).data_ok
    assert not fair_prob({}, PARAMS, FakeCtx({"archive": (PAGE, 3 * 3600)}, at(2026, 10, 13, 0))).data_ok
    assert not fair_prob({}, PARAMS, FakeCtx({}, at(2026, 10, 13, 0))).data_ok
