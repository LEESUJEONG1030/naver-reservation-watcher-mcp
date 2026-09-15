from datetime import UTC, datetime


def test_create_and_get_watch(isolated_db):
    db = isolated_db
    watch_id = db.create_watch(
        store_name="테스트식당", store_url=None, target_date="2026-10-03",
        time_min="18:00", time_max="20:00", time_priority=["19:00", "18:30"],
        party_size=2, watch_start_at=datetime.now(UTC).isoformat(),
        watch_end_at="2026-10-04T00:00:00+00:00", mode="ASK", status="PENDING",
    )
    watch = db.get_watch(watch_id)
    assert watch["store_name"] == "테스트식당"
    assert watch["time_priority"] == ["19:00", "18:30"]
    assert watch["status"] == "PENDING"


def test_update_watch_json_fields_roundtrip(isolated_db):
    db = isolated_db
    watch_id = db.create_watch(
        store_name="A", store_url=None, target_date="2026-10-03",
        time_min="18:00", time_max="20:00", time_priority=None,
        party_size=2, watch_start_at=datetime.now(UTC).isoformat(),
        watch_end_at="2026-10-04T00:00:00+00:00", mode="AUTO", status="PENDING",
    )
    db.update_watch(watch_id, status="AWAITING_APPROVAL", pending_candidate={"time": "19:00", "party_size": 2})
    watch = db.get_watch(watch_id)
    assert watch["status"] == "AWAITING_APPROVAL"
    assert watch["pending_candidate"] == {"time": "19:00", "party_size": 2}


def test_delete_watch(isolated_db):
    db = isolated_db
    watch_id = db.create_watch(
        store_name="A", store_url=None, target_date="2026-10-03",
        time_min="18:00", time_max="20:00", time_priority=None,
        party_size=2, watch_start_at=datetime.now(UTC).isoformat(),
        watch_end_at="2026-10-04T00:00:00+00:00", mode="AUTO", status="PENDING",
    )
    assert db.delete_watch(watch_id) is True
    assert db.get_watch(watch_id) is None
    assert db.delete_watch(watch_id) is False


def test_due_watches_filters_by_status_and_time(isolated_db):
    db = isolated_db
    past = "2020-01-01T00:00:00+00:00"
    future = "2999-01-01T00:00:00+00:00"

    due_id = db.create_watch(
        store_name="Due", store_url=None, target_date="2026-10-03",
        time_min="18:00", time_max="20:00", time_priority=None, party_size=2,
        watch_start_at=past, watch_end_at="2026-10-04T00:00:00+00:00",
        mode="AUTO", status="WATCHING",
    )
    db.update_watch(due_id, next_check_at=past)

    not_due_id = db.create_watch(
        store_name="NotDue", store_url=None, target_date="2026-10-03",
        time_min="18:00", time_max="20:00", time_priority=None, party_size=2,
        watch_start_at=past, watch_end_at="2026-10-04T00:00:00+00:00",
        mode="AUTO", status="WATCHING",
    )
    db.update_watch(not_due_id, next_check_at=future)

    reserved_id = db.create_watch(
        store_name="Reserved", store_url=None, target_date="2026-10-03",
        time_min="18:00", time_max="20:00", time_priority=None, party_size=2,
        watch_start_at=past, watch_end_at="2026-10-04T00:00:00+00:00",
        mode="AUTO", status="RESERVED",
    )
    db.update_watch(reserved_id, next_check_at=past)

    due = db.due_watches(datetime.now(UTC).isoformat())
    due_ids = {w["id"] for w in due}
    assert due_id in due_ids
    assert not_due_id not in due_ids
    assert reserved_id not in due_ids


def test_due_watches_excludes_needs_human_and_awaiting_approval(isolated_db):
    db = isolated_db
    past = "2020-01-01T00:00:00+00:00"

    for status in ("NEEDS_HUMAN", "AWAITING_APPROVAL"):
        wid = db.create_watch(
            store_name=status, store_url=None, target_date="2026-10-03",
            time_min="18:00", time_max="20:00", time_priority=None, party_size=2,
            watch_start_at=past, watch_end_at="2026-10-04T00:00:00+00:00",
            mode="AUTO", status=status,
        )
        db.update_watch(wid, next_check_at=past)

    due = db.due_watches(datetime.now(UTC).isoformat())
    assert due == []


def test_jobs_lifecycle(isolated_db):
    db = isolated_db
    job_id = db.create_job(type_="CHECK_NOW", watch_id=None, payload={"a": 1})
    claimed = db.claim_pending_jobs()
    assert len(claimed) == 1
    assert claimed[0]["id"] == job_id
    assert claimed[0]["status"] == "RUNNING"

    db.complete_job(job_id, status="DONE", result={"ok": True})
    job = db.get_job(job_id)
    assert job["status"] == "DONE"
    assert job["result"] == {"ok": True}


def test_heartbeat(isolated_db):
    db = isolated_db
    assert db.is_watcher_alive(60) is False
    db.update_heartbeat(1234)
    assert db.is_watcher_alive(60) is True
    hb = db.get_heartbeat()
    assert hb["pid"] == 1234
