from __future__ import annotations

from ChannelsParser.models import SearchFilters
from ChannelsParser.storage import ChannelStorage


def test_storage_persists_scan_progress_checkpoint(tmp_path) -> None:
    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()
    storage.create_scan("scan", user_id=1, mode="search", queries=["x"], filters=SearchFilters())
    storage.update_scan_progress(
        "scan",
        progress={"queries_done": 1, "reports_found": 2},
        total_candidates=10,
        total_reports=2,
    )
    storage.fail_scan("scan", error="boom")

    with storage._connect() as connection:  # validate durable checkpoint schema
        row = connection.execute(
            "SELECT total_candidates, total_reports, progress FROM scans WHERE scan_id = 'scan'"
        ).fetchone()
    assert row["total_candidates"] == 10
    assert row["total_reports"] == 2
    assert '"queries_done": 1' in row["progress"]
