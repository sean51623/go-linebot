import pytest
from handlers.go_engine import GoBoard


# ---------------------------------------------------------------------------
# Coordinate parsing
# ---------------------------------------------------------------------------

def test_parse_coordinates_valid():
    board = GoBoard()
    # D4: D is index 3 in col_labels, row = 19 - 4 = 15
    assert board.parse_coordinates("D4") == (15, 3)

def test_parse_coordinates_skips_i():
    board = GoBoard()
    # J is index 8 (I is skipped), row = 19 - 10 = 9
    assert board.parse_coordinates("J10") == (9, 8)

def test_parse_coordinates_corner_a1():
    board = GoBoard()
    assert board.parse_coordinates("A1") == (18, 0)

def test_parse_coordinates_corner_t19():
    board = GoBoard()
    assert board.parse_coordinates("T19") == (0, 18)

def test_parse_coordinates_invalid_letter():
    board = GoBoard()
    assert board.parse_coordinates("I10") is None  # I is skipped

def test_parse_coordinates_out_of_bounds():
    board = GoBoard()
    assert board.parse_coordinates("A20") is None

def test_parse_coordinates_empty():
    board = GoBoard()
    assert board.parse_coordinates("") is None


# ---------------------------------------------------------------------------
# Place stone — basic
# ---------------------------------------------------------------------------

def test_place_stone_success():
    board = GoBoard()
    success, msg = board.place_stone("D4", 1)
    assert success
    assert board.board[15][3] == 1

def test_place_stone_occupied():
    board = GoBoard()
    board.place_stone("D4", 1)
    success, msg = board.place_stone("D4", 2)
    assert not success
    assert "已經有棋子" in msg

def test_place_stone_invalid_coord():
    board = GoBoard()
    success, msg = board.place_stone("Z99", 1)
    assert not success


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def test_single_stone_capture():
    board = GoBoard()
    # White at D4=(15,3), surrounded on three sides by black
    board.board[15][3] = 2  # white at D4
    board.board[14][3] = 1  # black at D5
    board.board[15][2] = 1  # black at C4
    board.board[16][3] = 1  # black at D3
    # Black plays E4=(15,4) — captures white at D4
    success, msg = board.place_stone("E4", 1)
    assert success
    assert board.board[15][3] == 0  # white captured
    assert "提吃了 1 顆子" in msg

def test_group_capture():
    board = GoBoard()
    # Two white stones at D4=(15,3) and E4=(15,4), surrounded by black
    board.board[15][3] = 2  # D4 white
    board.board[15][4] = 2  # E4 white
    # Surround: D5, C4, D3 for D4 side; F4, E5, E3 for E4 side
    board.board[14][3] = 1  # D5
    board.board[15][2] = 1  # C4
    board.board[16][3] = 1  # D3
    board.board[14][4] = 1  # E5
    board.board[16][4] = 1  # E3
    # Black plays F4=(15,5) — captures both white stones
    success, msg = board.place_stone("F4", 1)
    assert success
    assert board.board[15][3] == 0
    assert board.board[15][4] == 0
    assert "提吃了 2 顆子" in msg


# ---------------------------------------------------------------------------
# Ko rule
# ---------------------------------------------------------------------------

def test_ko_blocks_immediate_recapture():
    board = GoBoard()
    # Directly set ko_point to simulate a position after a capture
    board.ko_point = (5, 5)  # F14
    success, msg = board.place_stone("F14", 1)
    assert not success
    assert "打劫" in msg

def test_ko_cleared_after_other_move():
    board = GoBoard()
    board.ko_point = (5, 5)
    board.place_stone("D4", 1)  # play elsewhere
    assert board.ko_point is None


# ---------------------------------------------------------------------------
# Suicide rule
# ---------------------------------------------------------------------------

def test_suicide_blocked():
    board = GoBoard()
    # Surround B18=(1,1) with four white stones, then try to place black there
    board.board[0][1] = 2  # B19=(0,1)
    board.board[2][1] = 2  # B17=(2,1)
    board.board[1][0] = 2  # A18=(1,0)
    board.board[1][2] = 2  # C18=(1,2)
    success, msg = board.place_stone("B18", 1)
    assert not success
    assert "禁手" in msg

def test_capture_is_not_suicide():
    board = GoBoard()
    # Black captures a white stone even though the capturing stone's group
    # would have zero liberties if the capture didn't happen — legal move
    board.board[0][1] = 2  # white at B19=(0,1)
    board.board[1][0] = 1  # black at A18=(1,0)
    board.board[0][2] = 1  # black at C19=(0,2)
    board.board[1][1] = 1  # black at B18=(1,1) — removes last liberty except A19
    # A19=(0,0): white at B19 now has only A19 as its last liberty
    # Placing black at A19 captures white at B19, giving the new stone a liberty
    success, msg = board.place_stone("A19", 1)
    assert success
    assert board.board[0][1] == 0  # white captured
