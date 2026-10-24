"""Talk 17 add-ons to talk 16: mean vs percentiles, users vs requests, partial pages. Seeded."""
import pytest

from sysdesign.ext import tail_extras as te

XS = te.sample()
S = te.summary(XS)


def test_mean_about_90():
    assert S["mean_ms"] == pytest.approx(90, abs=3)


def test_p50_about_55():
    assert S["p50_ms"] == pytest.approx(55, abs=2)


def test_p99_about_543():
    assert S["p99_ms"] == pytest.approx(543, rel=0.05)


def test_mean_sits_above_median():
    assert S["mean_ms"] > S["p50_ms"] * 1.4


def test_most_requests_are_faster_than_the_mean():
    assert S["share_slower_than_mean"] < 0.35


def test_quoted_numbers_kept_separate():
    assert "quoted" in te.QUOTED["source"] and te.QUOTED["p99_ms"] == 543


def test_pct_edges():
    assert te.pct([1, 2, 3, 4], 0) == 1 and te.pct([1, 2, 3, 4], 100) == 4 and te.pct([5], 99) == 5


def test_users_hit_formula():
    assert te.users_hit(0.01, 1) == pytest.approx(0.01)
    assert te.users_hit(0.01, 100) == pytest.approx(0.634, abs=0.001)


def test_users_hit_rejects_bad_input():
    with pytest.raises(ValueError):
        te.users_hit(-0.1, 5)
    with pytest.raises(ValueError):
        te.users_hit(0.01, -1)


def test_simulation_matches_formula():
    thr = te.pct(XS, 99)
    for n in (10, 50):
        assert te.users_hit_sim(XS, thr, n) == pytest.approx(te.users_hit(0.01, n), abs=0.03)


def test_one_percent_of_requests_is_most_users_at_100():
    assert te.users_hit(0.01, 100) > 0.6


# ---------------------------------------------------------------- partial pages
P = te.page_study()


def test_page_wait_for_all_p99_is_slow():
    assert P["wait_for_all"]["p99_ms"] > 1500


def test_partial_page_p99_is_the_deadline():
    assert P["partial"]["p99_ms"] == P["deadline_ms"] == 250.0


def test_partial_page_marks_few_parts_loading():
    assert 0 < P["parts_loading_mean"] < 10 and P["parts_loading_p99"] <= 15


def test_render_page_all_ready_before_deadline():
    r = te.render_page([10, 20, 30], 100)
    assert r["ready_ms"] == 30 and r["loading"] == 0


def test_render_page_marks_slow_part():
    r = te.render_page([10, 500, 30], 100)
    assert r["ready_ms"] == 100 and r["parts"] == ["ready", "loading", "ready"]


def test_render_page_no_deadline_waits():
    assert te.render_page([10, 500], None)["ready_ms"] == 500


def test_fanout_makes_wait_for_all_worse():
    small = te.page_study(fanout=5, pages=500)
    assert small["wait_for_all"]["p99_ms"] < P["wait_for_all"]["p99_ms"]


def test_results_deterministic():
    assert te.results() == te.results()
