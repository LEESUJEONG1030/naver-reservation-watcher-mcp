from nrw.time_utils import in_range, pick_best_candidate, rank_all_candidates


def test_in_range_basic():
    assert in_range("19:00", "18:00", "20:00")
    assert not in_range("17:59", "18:00", "20:00")
    assert not in_range("20:01", "18:00", "20:00")


def test_pick_best_candidate_uses_priority_order():
    available = ["18:00", "18:30", "19:30"]
    picked = pick_best_candidate(available, "18:00", "20:00", ["19:00", "18:30", "19:30"])
    # 19:00 is priority #1 but not available -> fall through to 18:30
    assert picked == "18:30"


def test_pick_best_candidate_no_priority_picks_earliest_in_range():
    available = ["20:30", "19:30", "18:00"]
    picked = pick_best_candidate(available, "18:00", "20:00", None)
    assert picked == "18:00"


def test_pick_best_candidate_none_when_nothing_matches():
    available = ["17:00", "21:00"]
    assert pick_best_candidate(available, "18:00", "20:00", None) is None
    assert pick_best_candidate(["19:00"], "18:00", "20:00", ["19:30"]) is None


def test_rank_all_candidates_orders_priority_first_then_rest():
    available = ["18:30", "19:00", "19:30", "20:00"]
    ranked = rank_all_candidates(available, "18:00", "20:00", ["19:30", "19:00"])
    assert ranked == ["19:30", "19:00", "18:30", "20:00"]
