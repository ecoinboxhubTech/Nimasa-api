from datetime import datetime, timedelta, timezone

from app.services.alerts import Point, derive_status, find_triggers

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def p(hours_ago, value, kind="model_estimate"):
    return Point(NOW - hours_ago * H, value, kind)


def trig(points, **kw):
    args = dict(operator=">=", threshold=2.0, suppression_hours=6, points=points, now=NOW)
    args.update(kw)
    return find_triggers(**args)


def test_positive():
    assert len(trig([p(1, 2.4)])) == 1


def test_negative():
    assert trig([p(1, 1.9)]) == []


def test_boundary_is_inclusive():
    assert len(trig([p(1, 2.0)])) == 1


def test_ignores_missing_nan_forecast_and_future():
    assert trig([p(1, None), p(1, float("nan")), p(-3, 9.0, "forecast"), Point(NOW + H, 9.0, "model_estimate")]) == []


def test_suppression_window_and_retrigger():
    got = trig([p(10, 2.2), p(8, 2.3), p(5, 2.4), p(3, 2.5)])
    assert [x.t for x in got] == [NOW - 10 * H, NOW - 3 * H]


def test_known_event_still_suppresses_followers():
    known = {NOW - 5 * H}
    assert trig([p(5, 2.4), p(3, 2.6)], known_times=known) == []


def test_prior_event_suppresses():
    assert trig([p(3, 2.6)], last_trigger_at=NOW - 5 * H) == []


def test_less_than_operator():
    assert len(trig([p(1, 0.4)], operator="<=", threshold=0.5)) == 1


def test_status_derivation_never_marks_stale_as_active():
    valid = NOW + H
    assert derive_status(valid, "fresh", NOW) == "active"
    assert derive_status(valid, "stale", NOW) == "suppressed_stale"
    assert derive_status(NOW - H, "fresh", NOW) == "expired"
