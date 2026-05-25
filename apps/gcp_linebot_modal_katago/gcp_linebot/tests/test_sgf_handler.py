import pytest
from handlers.sgf_handler import parse_sgf, get_top_winrate_diff_moves


# ---------------------------------------------------------------------------
# parse_sgf
# ---------------------------------------------------------------------------

def test_parse_sgf_move_count():
    sgf_content = "(;GM[1]FF[4]SZ[19];B[pd];W[dp];B[pp])"
    result = parse_sgf(sgf_content)
    assert result["totalMoves"] == 3

def test_parse_sgf_first_move_color():
    sgf_content = "(;GM[1]FF[4]SZ[19];B[pd];W[dp])"
    result = parse_sgf(sgf_content)
    assert result["moves"][0]["color"] == "B"
    assert result["moves"][1]["color"] == "W"
    # [dp]: d=col3 -> D, p=row15 from bottom -> 19-15=4 -> D4
    assert result["moves"][1]["played"] == "D4"

def test_parse_sgf_first_move_coordinate():
    # [pd]: p=col15 -> Q (skip I), d=row3 -> 19-3=16 -> Q16
    sgf_content = "(;GM[1]FF[4]SZ[19];B[pd])"
    result = parse_sgf(sgf_content)
    assert result["moves"][0]["played"] == "Q16"

def test_parse_sgf_empty_game():
    sgf_content = "(;GM[1]FF[4]SZ[19])"
    result = parse_sgf(sgf_content)
    assert result["totalMoves"] == 0
    assert result["moves"] == []

def test_parse_sgf_invalid_raises():
    with pytest.raises(ValueError):
        parse_sgf("this is not sgf")

def test_parse_sgf_pass_move_does_not_crash():
    # tt = pass in 19x19 SGF; pass IS counted in totalMoves, played=None
    sgf_content = "(;GM[1]FF[4]SZ[19];B[pd];W[tt])"
    result = parse_sgf(sgf_content)
    assert result["totalMoves"] == 2
    assert result["moves"][1]["played"] is None


# ---------------------------------------------------------------------------
# get_top_winrate_diff_moves
# ---------------------------------------------------------------------------

SAMPLE_MOVES = [
    {"move": 1, "color": "B", "played": "D4",  "winrate_before": 50.0, "winrate_after": 48.0, "score_loss": 1.0},
    {"move": 3, "color": "B", "played": "K10", "winrate_before": 52.0, "winrate_after": 45.0, "score_loss": 3.5},
    {"move": 5, "color": "W", "played": "Q16", "winrate_before": 55.0, "winrate_after": 40.0, "score_loss": 8.0},
    {"move": 7, "color": "B", "played": "R4",  "winrate_before": 60.0, "winrate_after": 58.5, "score_loss": 0.5},
]

def test_get_top_returns_sorted_by_move_number():
    result = get_top_winrate_diff_moves(SAMPLE_MOVES, top_n=2)
    # Top 2 by winrate_diff: move5 (15.0) and move3 (7.0); sorted by move number → [3, 5]
    assert [m["move"] for m in result] == [3, 5]

def test_get_top_respects_top_n():
    result = get_top_winrate_diff_moves(SAMPLE_MOVES, top_n=1)
    assert len(result) == 1
    assert result[0]["move"] == 5  # biggest drop

def test_get_top_excludes_winrate_increases():
    moves_with_increase = [
        {"move": 1, "color": "B", "played": "D4", "winrate_before": 48.0, "winrate_after": 55.0},
    ]
    result = get_top_winrate_diff_moves(moves_with_increase, top_n=5)
    assert result == []

def test_get_top_empty_input():
    assert get_top_winrate_diff_moves([], top_n=5) == []

def test_get_top_missing_winrate_fields():
    moves = [{"move": 1, "color": "B", "played": "D4"}]  # no winrate fields
    result = get_top_winrate_diff_moves(moves, top_n=5)
    assert result == []

def test_get_top_adds_winrate_diff_field():
    result = get_top_winrate_diff_moves(SAMPLE_MOVES, top_n=4)
    for m in result:
        assert "winrate_diff" in m
        assert m["winrate_diff"] == m["winrate_before"] - m["winrate_after"]
