from datetime import UTC, datetime, timedelta, timezone

import pytest

from marketing_intelligence.change_event_filters import (
    change_event_list_url,
    parse_change_event_list_state,
)


def test_local_dates_form_inclusive_day_and_half_open_utc_range() -> None:
    state, errors = parse_change_event_list_state(
        event_type="title_changed",
        date_from="2026-07-17",
        date_to="2026-07-18",
        page="2",
        local_timezone=timezone(timedelta(hours=3)),
    )
    assert not errors and state is not None
    assert state.from_time == datetime(2026, 7, 16, 21, tzinfo=UTC)
    assert state.before_time == datetime(2026, 7, 18, 21, tzinfo=UTC)
    assert change_event_list_url(7, state) == (
        "/sites/7/changes?event_type=title_changed&date_from=2026-07-17"
        "&date_to=2026-07-18&page=2"
    )


@pytest.mark.parametrize("page", ["", "1.0", "+1", "01", " 1", "0", "-2"])
def test_page_requires_canonical_positive_integer(page: str) -> None:
    state, errors = parse_change_event_list_state(
        event_type="",
        date_from="",
        date_to="",
        page=page,
        local_timezone=timezone.utc,
    )
    assert state is None
    assert "page" in errors
