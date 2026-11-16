from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import create_engine, select

from src.extract.strava import (
    DailyRateLimitReached,
    Settings,
    StravaClient,
    StravaTokens,
    extract_activities,
    metadata,
    raw_strava_activities,
    strava_sync_state,
)


class FakeClient:
    def __init__(self, activities: list[dict[str, Any]]) -> None:
        self.activities = activities
        self.page_calls: list[dict[str, int | None]] = []
        self.stream_calls: list[int] = []
        self.fail_stream_ids: set[int] = set()

    def activity_page(
        self,
        page: int,
        before: int,
        after: int | None = None,
    ) -> list[dict[str, Any]]:
        self.page_calls.append({"page": page, "before": before, "after": after})
        if after is None:
            return self.activities
        return []

    def activity_streams(self, activity_id: int) -> dict[str, Any] | None:
        self.stream_calls.append(activity_id)
        if activity_id in self.fail_stream_ids:
            raise DailyRateLimitReached("rate limit")
        return {"time": {"data": [0, 1], "series_type": "time"}}


def _activity(activity_id: int, kind: str, started_at: datetime) -> dict[str, Any]:
    return {
        "id": activity_id,
        "type": kind,
        "sport_type": kind,
        "start_date": started_at.isoformat(),
        "name": f"Workout {activity_id}",
        "distance": 1234.5,
    }


def _engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def test_authorization_url_requests_read_all_and_uses_state(tmp_path: Path) -> None:
    settings = Settings(
        "client-id", "secret", "http://localhost/exchange_token", tmp_path / "tokens"
    )
    client = StravaClient(settings)

    query = parse_qs(urlsplit(client.authorization_url()).query)

    assert query["scope"] == ["activity:read_all"]
    assert query["state"] == [client.oauth_state]


def test_expired_access_token_is_refreshed_and_rotated(tmp_path: Path) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_at": 2_000_000_000,
            }

    class Session:
        def __init__(self) -> None:
            self.post_calls: list[dict[str, Any]] = []

        def post(self, url: str, **kwargs: Any) -> Response:
            self.post_calls.append(kwargs)
            return Response()

    token_path = tmp_path / "tokens.json"
    settings = Settings(
        "client-id", "secret", "http://localhost/exchange_token", token_path
    )
    session = Session()
    client = StravaClient(settings, session=session)  # type: ignore[arg-type]
    client.token_store.save(
        StravaTokens(
            access_token="expired-access",
            refresh_token="old-refresh",
            expires_at=1,
        )
    )
    client.tokens = client.token_store.load()

    assert client._access_token() == "new-access"
    assert session.post_calls[0]["data"]["refresh_token"] == "old-refresh"
    assert client.token_store.load().refresh_token == "new-refresh"  # type: ignore[union-attr]


def test_extract_preserves_raw_json_stores_run_streams_and_is_incremental() -> None:
    engine = _engine()
    started_at = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    run = _activity(101, "Run", started_at)
    ride = _activity(102, "Ride", datetime(2026, 11, 3, 12, 0, tzinfo=UTC))
    client = FakeClient([run, ride])

    assert extract_activities(engine, client) == 2
    assert extract_activities(engine, client) == 0

    with engine.connect() as connection:
        rows = connection.execute(
            select(
                raw_strava_activities.c.strava_activity_id,
                raw_strava_activities.c.payload,
                raw_strava_activities.c.streams,
                raw_strava_activities.c.streams_status,
            ).order_by(raw_strava_activities.c.strava_activity_id)
        ).all()
        sync_state = connection.execute(select(strava_sync_state)).mappings().one()

    assert rows[0].payload == run
    assert rows[0].streams == {"time": {"data": [0, 1], "series_type": "time"}}
    assert rows[0].streams_status == "available"
    assert rows[1].payload == ride
    assert rows[1].streams is None
    assert rows[1].streams_status == "not_applicable"
    assert client.stream_calls == [101]
    assert sync_state["sync_mode"] == "idle"
    assert any(call["after"] is not None for call in client.page_calls)
    engine.dispose()


def test_daily_limit_leaves_stream_pending_for_next_run() -> None:
    engine = _engine()
    activity = _activity(201, "Run", datetime(2026, 11, 4, 12, 0, tzinfo=UTC))
    client = FakeClient([activity])
    client.fail_stream_ids.add(201)

    assert extract_activities(engine, client) == 1
    client.fail_stream_ids.clear()
    assert extract_activities(engine, client) == 0

    with engine.connect() as connection:
        row = connection.execute(select(raw_strava_activities)).mappings().one()
    assert row["streams_status"] == "available"
    assert row["streams"] == {"time": {"data": [0, 1], "series_type": "time"}}
    engine.dispose()
