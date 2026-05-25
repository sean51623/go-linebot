import re
from sgfmill import sgf


def sgf_coord_to_standard(coord: str) -> str:
    """Convert SGF coordinate to standard format (e.g., 'pd' -> 'F14')"""
    if not coord or len(coord) != 2:
        return None

    x = ord(coord[0]) - 97  # a=0, b=1, ..., z=25
    y = ord(coord[1]) - 97

    # Convert to standard format: A=0, B=1, ..., H=7, J=8, K=9, ..., T=18 (19x19 board)
    # Note: Go coordinate system skips I (because I and 1 are easily confused)
    if x < 8:
        # A-H (0-7)
        letter = chr(65 + x)  # A-H
    else:
        # J-T (8-18), skip I
        letter = chr(66 + x)  # J-T (because we skip I, so +66 instead of +65)

    number = 19 - y  # From top to bottom is 1-19

    return f"{letter}{number}"


def parse_ai_comment(comment: str) -> dict:
    """Parse English AI annotation"""
    if not comment:
        return {}

    result = {}

    # Parse move number, color, and actual played position
    # Format: Move 49: B F14
    move_match = re.match(r"Move\s*(\d+):\s*([BW])\s+([A-T]\d+)", comment)
    if move_match:
        result["move"] = int(move_match.group(1))
        result["color"] = move_match.group(2)
        result["played"] = move_match.group(3)

    # Parse win rate (after move)
    # Format: Win rate: B 59.4% or Win rate: W 60.5%
    winrate_match = re.search(r"Win rate:\s*[BW]\s*([\d.]+)%", comment)
    if winrate_match:
        result["winrate_after"] = float(winrate_match.group(1))

    # Parse point loss
    # Format: Estimated point loss: 2.2
    score_loss_match = re.search(r"Estimated point loss:\s*([\d.]+)", comment)
    if score_loss_match:
        result["score_loss"] = float(score_loss_match.group(1))

    # Parse best move
    # Format: Predicted top move was K15 (B+3.4).
    ai_best_match = re.search(r"Predicted top move was\s+([A-T]\d+)", comment)
    if ai_best_match:
        result["ai_best"] = ai_best_match.group(1)

    # Parse PV (variation)
    # Format: PV: BK15 L15 K14 J16 C13 or PV: BF11 F12 D11 B12 C13 C14 H12
    # Note: Only the first coordinate may have B/W prefix, subsequent B/W in coordinates are column names, should not be removed
    pv_match = re.search(r"PV:\s*([BW]?[A-T]\d+(?:\s+[A-T]\d+)*)", comment)
    if pv_match:
        pv_str = pv_match.group(1).strip()
        coords = pv_str.split()

        result["pv"] = [
            coord.replace("B", "").replace("W", "") if i == 0 else coord
            for i, coord in enumerate(coords)
            if len(coord) > 0
            and re.match(r"^[A-T]\d+$", coord.replace("B", "").replace("W", ""))
        ]

    return result


def extract_moves(root_node):
    """Extract information for each move from SGF tree structure"""
    moves = []
    move_number = 0
    previous_winrate = None

    def traverse(node):
        nonlocal move_number, previous_winrate

        if node is None:
            return

        current_move = None
        current_comment = None

        # Check if there's a move (B or W)
        # sgfmill uses get() method to get properties
        if node.has_property("B"):
            move_number += 1
            coord = node.get("B")
            # sgfmill returns a (row, col) tuple for normal moves, or None for pass
            # A coord is a tuple of two ints; don't unpack it like a list-of-moves
            if isinstance(coord, tuple) and len(coord) == 2 and isinstance(coord[0], int):
                # Convert (row_from_bottom, col) to SGF coord string (col_letter, row_letter_from_top)
                coord_str = chr(coord[1] + 97) + chr(
                    (18 - coord[0]) + 97
                )  # Convert to SGF format
            elif isinstance(coord, (list,)) and len(coord) > 0:
                coord = coord[0]
                coord_str = str(coord)
            else:
                coord_str = str(coord)
            current_move = {
                "move": move_number,
                "color": "B",
                "played": sgf_coord_to_standard(coord_str),
                "ai_best": None,
                "pv": [],
                "winrate_before": previous_winrate,
                "winrate_after": None,
                "score_loss": None,
            }
        elif node.has_property("W"):
            move_number += 1
            coord = node.get("W")
            # sgfmill returns a (row, col) tuple for normal moves, or None for pass
            # A coord is a tuple of two ints; don't unpack it like a list-of-moves
            if isinstance(coord, tuple) and len(coord) == 2 and isinstance(coord[0], int):
                # Convert (row_from_bottom, col) to SGF coord string (col_letter, row_letter_from_top)
                coord_str = chr(coord[1] + 97) + chr(
                    (18 - coord[0]) + 97
                )  # Convert to SGF format
            elif isinstance(coord, (list,)) and len(coord) > 0:
                coord = coord[0]
                coord_str = str(coord)
            else:
                coord_str = str(coord)
            current_move = {
                "move": move_number,
                "color": "W",
                "played": sgf_coord_to_standard(coord_str),
                "ai_best": None,
                "pv": [],
                "winrate_before": previous_winrate,
                "winrate_after": None,
                "score_loss": None,
            }

        # Check if there's a comment (C)
        if node.has_property("C"):
            comment = node.get("C")
            if isinstance(comment, (list, tuple)) and len(comment) > 0:
                comment = comment[0]
            if isinstance(comment, bytes):
                comment = comment.decode("utf-8", errors="ignore")
            current_comment = parse_ai_comment(comment)

        # If there's a move, merge comment information
        if current_move:
            if current_comment:
                # Extract information from comment (prioritize comment info as it's more accurate)
                if current_comment.get("played"):
                    current_move["played"] = current_comment["played"]
                if current_comment.get("color"):
                    current_move["color"] = current_comment["color"]
                if current_comment.get("ai_best"):
                    current_move["ai_best"] = current_comment["ai_best"]
                if current_comment.get("pv") and len(current_comment["pv"]) > 0:
                    current_move["pv"] = current_comment["pv"]
                if current_comment.get("winrate_after") is not None:
                    current_move["winrate_after"] = current_comment["winrate_after"]
                    # Update previous_winrate for next move
                    previous_winrate = current_comment["winrate_after"]
                if current_comment.get("score_loss") is not None:
                    current_move["score_loss"] = current_comment["score_loss"]

            moves.append(current_move)

        # Process child nodes
        for child in node:
            traverse(child)

    traverse(root_node)
    return moves


def parse_sgf(sgf_content) -> dict:
    """Parse SGF file content (accepts str or bytes)"""
    try:
        # Use sgfmill to parse - from_string expects a string, not bytes
        if isinstance(sgf_content, bytes):
            sgf_string = sgf_content.decode("utf-8")
        elif isinstance(sgf_content, str):
            sgf_string = sgf_content
        else:
            raise TypeError(f"Expected str or bytes, got {type(sgf_content)}")

        game = sgf.Sgf_game.from_string(sgf_string)
        root = game.get_root()

        # Extract moves
        moves = extract_moves(root)

        return {"moves": moves, "totalMoves": len(moves)}
    except Exception as error:
        raise ValueError(f"Failed to parse SGF: {str(error)}")


def filter_critical_moves(moves: list, threshold: float = 2.0) -> list:
    """Filter critical moves (moves with score_loss greater than threshold)"""
    if not moves or not isinstance(moves, list):
        return []

    return [
        move
        for move in moves
        if move.get("score_loss") is not None and move["score_loss"] > threshold
    ]


def get_top_winrate_diff_moves(moves: list, top_n: int = 20) -> list:
    """Get top N moves with highest winrate difference (winrate_before - winrate_after)"""
    if not moves or not isinstance(moves, list):
        return []

    # Filter moves with both winrate_before and winrate_after
    moves_with_winrate_diff = []
    for move in moves:
        winrate_before = move.get("winrate_before")
        winrate_after = move.get("winrate_after")

        if (
            winrate_before is not None
            and winrate_after is not None
            and isinstance(winrate_before, (int, float))
            and isinstance(winrate_after, (int, float))
        ):
            # Calculate winrate difference (how much winrate dropped)
            winrate_diff = winrate_before - winrate_after
            # Only include moves where winrate actually decreased
            if winrate_diff > 0:
                move_copy = move.copy()
                move_copy["winrate_diff"] = winrate_diff
                moves_with_winrate_diff.append(move_copy)

    # Sort by winrate_diff descending (biggest drop first)
    sorted_by_winrate_diff = sorted(
        moves_with_winrate_diff, key=lambda x: x["winrate_diff"], reverse=True
    )

    # Take top N moves
    top_moves = sorted_by_winrate_diff[:top_n]

    # Finally sort by move number ascending
    return sorted(top_moves, key=lambda x: x["move"])


def get_top_score_loss_moves(moves: list, top_n: int = 20) -> list:
    """Get top N moves with highest score loss."""
    if not moves or not isinstance(moves, list):
        return []

    moves_with_score_loss = [
        move
        for move in moves
        if move.get("score_loss") is not None
        and isinstance(move.get("score_loss"), (int, float))
        and move.get("score_loss") > 0
    ]

    sorted_by_score_loss = sorted(
        moves_with_score_loss, key=lambda x: x["score_loss"], reverse=True
    )
    top_moves = sorted_by_score_loss[:top_n]
    return sorted(top_moves, key=lambda x: x["move"])


def get_top_review_moves(moves: list, top_n: int = 20, metric: str = "winrate") -> list:
    """Get top review moves by selected metric: winrate or score_loss."""
    if metric == "score_loss":
        return get_top_score_loss_moves(moves, top_n)
    return get_top_winrate_diff_moves(moves, top_n)
