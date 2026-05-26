import json
import time
from pathlib import Path
from typing import Optional, Dict, Any

from sgfmill import sgf

from config import config
from logger import logger
from handlers.go_engine import GoBoard


# ---------------------------------------------------------------------------
# Module-level state (moved from line_handler.py)
# ---------------------------------------------------------------------------

game_states: Dict[str, Dict[str, Any]] = {}
game_ids: Dict[str, str] = {}
vs_ai_modes: Dict[str, bool] = {}
current_sgf_files: Dict[str, str] = {}  # target_id → filename


def get_game_id(target_id: str) -> str:
    """Get or create game ID for a target (user/group/room)
    Game ID is a unique identifier for each game session.
    """
    if target_id not in game_ids:
        # Generate new game ID (timestamp-based)
        game_ids[target_id] = f"game_{int(time.time())}"
        logger.info(f"Created new game ID for {target_id}: {game_ids[target_id]}")
    return game_ids[target_id]


def enable_vs_ai_mode(target_id: str) -> bool:
    """Enable VS AI mode for a target"""
    try:
        vs_ai_modes[target_id] = True
        logger.info(f"Enabled VS AI mode for {target_id}")
        return True
    except Exception as error:
        logger.error(f"Failed to enable VS AI mode for {target_id}: {error}", exc_info=True)
        return False


def disable_vs_ai_mode(target_id: str) -> bool:
    """Disable VS AI mode for a target"""
    try:
        vs_ai_modes[target_id] = False
        logger.info(f"Disabled VS AI mode for {target_id}")
        return True
    except Exception as error:
        logger.error(f"Failed to disable VS AI mode for {target_id}: {error}", exc_info=True)
        return False


def is_vs_ai_mode(target_id: str) -> bool:
    """Check if VS AI mode is enabled for a target"""
    return vs_ai_modes.get(target_id, False)


def get_game_state(target_id: str) -> Dict[str, Any]:
    """Get or create game state for a target (user/group/room)

    If game state doesn't exist in memory, try to restore from latest SGF file.
    If no SGF file exists, create a new game.
    """
    if target_id not in game_states:
        # Try to restore from SGF file
        restored = restore_game_from_sgf(target_id)
        if restored:
            game_states[target_id] = restored
            # Try to extract game_id from restored SGF file path
            current_file = Path(__file__)
            project_root = current_file.parent.parent.parent
            static_dir = project_root / "static"
            pattern = f"game_{target_id}_*"
            sgf_files = list(static_dir.glob(f"**/{pattern}/*.sgf"))
            if sgf_files:
                # Extract game_id from path: static/{game_id}/game_{target_id}_{timestamp}.sgf
                latest_sgf = max(sgf_files, key=lambda p: p.stat().st_mtime)
                game_id = latest_sgf.parent.name
                game_ids[target_id] = game_id
            logger.info(f"Restored game state for {target_id} from SGF file")
        else:
            # Create new game
            game_states[target_id] = {
                "game": GoBoard(),
                "current_turn": 1,  # 1=黑, 2=白
                "sgf_game": sgf.Sgf_game(size=19),
            }
            # Generate new game ID
            get_game_id(target_id)
            logger.info(f"Created new game state for {target_id}")
    return game_states[target_id]


def restore_game_from_sgf_file(sgf_path: str) -> Optional[Dict[str, Any]]:
    """Restore game state from a specific SGF file path"""
    try:
        # Load SGF file
        with open(sgf_path, "rb") as f:
            sgf_game = sgf.Sgf_game.from_bytes(f.read())

        # Rebuild board state from SGF
        game = GoBoard()
        current_turn = 1  # Start with black
        last_move_coords = None

        # Traverse SGF to rebuild board
        for node in sgf_game.get_main_sequence():
            color, move = node.get_move()
            if move is not None:
                # move is (sgf_row, sgf_col), where sgf_row 0 is bottom
                sgf_r, sgf_c = move

                # Convert to engine coordinates (row 0 is top)
                r = 18 - sgf_r
                c = sgf_c

                last_move_coords = (r, c)
                stone_val = 1 if color == "b" else 2

                # Place stone on board
                game.board[r][c] = stone_val

                # Handle capture logic (simplified - just remove captured stones)
                opponent = 2 if stone_val == 1 else 1
                captured_stones = []
                neighbors = [(r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)]
                for nr, nc in neighbors:
                    if 0 <= nr < 19 and 0 <= nc < 19:
                        if game.board[nr][nc] == opponent:
                            group, libs = game.get_group_and_liberties(nr, nc)
                            if libs == 0:
                                for gr, gc in group:
                                    captured_stones.append((gr, gc))

                # Remove captured stones
                for cr, cc in captured_stones:
                    game.board[cr][cc] = 0

                # Update ko point (simplified)
                my_group, my_libs = game.get_group_and_liberties(r, c)
                if len(captured_stones) == 1 and my_libs == 1:
                    game.ko_point = captured_stones[0]
                else:
                    game.ko_point = None

                # Switch turn
                current_turn = 2 if stone_val == 1 else 1

        return {
            "game": game,
            "current_turn": current_turn,
            "sgf_game": sgf_game,
        }
    except Exception as error:
        logger.error(
            f"Failed to restore game from SGF file {sgf_path}: {error}", exc_info=True
        )
        return None


def create_sgf_with_first_n_moves(sgf_game: sgf.Sgf_game, n_moves: int) -> sgf.Sgf_game:
    """Create a new SGF game with only the first N moves from the original SGF

    Args:
        sgf_game: Original SGF game object
        n_moves: Number of moves to keep (1-based, so n_moves=10 means first 10 moves)

    Returns:
        New SGF game object with only the first N moves
    """
    # Create a new SGF game with the same board size
    new_sgf = sgf.Sgf_game(size=sgf_game.get_size())

    # Copy root properties (like komi, rules, etc.)
    root = sgf_game.get_root()
    new_root = new_sgf.get_root()

    # Copy common root properties except moves
    # Common SGF properties: SZ (size), KM (komi), RU (rules), DT (date),
    # PB/PW (player names), RE (result), HA (handicap), PL (player to move)
    common_props = ["SZ", "KM", "RU", "DT", "PB", "PW", "RE", "HA", "PL", "FF", "CA", "GM", "AP"]
    for prop in common_props:
        if root.has_property(prop):
            values = root.get(prop)
            if values is not None:
                if isinstance(values, (list, tuple)) and len(values) > 0:
                    new_root.set(prop, values[0] if len(values) == 1 else values)
                else:
                    new_root.set(prop, values)

    # Get main sequence and take first N moves
    sequence = sgf_game.get_main_sequence()
    move_count = 0
    current_node = new_root

    for node in sequence:
        color, move = node.get_move()
        if move is not None:
            move_count += 1
            if move_count > n_moves:
                break  # Stop after N moves

            # Create a new child node with this move
            new_node = current_node.new_child()
            new_node.set_move(color, move)
            current_node = new_node

    return new_sgf


def restore_game_from_sgf(target_id: str) -> Optional[Dict[str, Any]]:
    """Try to restore game state from latest SGF file for this target"""
    try:
        current_file = Path(__file__)
        project_root = current_file.parent.parent.parent
        static_dir = project_root / "static"

        if not static_dir.exists():
            return None

        # Find SGF file for this target
        # Pattern: static/{game_id}/game_{target_id}.sgf (fixed filename)
        # Try to find the latest game_id folder with this target's SGF
        pattern = f"**/game_{target_id}.sgf"
        sgf_files = list(static_dir.glob(pattern))

        if not sgf_files:
            return None

        # Get the latest file (by modification time)
        latest_sgf = max(sgf_files, key=lambda p: p.stat().st_mtime)

        # Use the helper function to restore
        return restore_game_from_sgf_file(str(latest_sgf))
    except Exception as error:
        logger.error(
            f"Failed to restore game from SGF for {target_id}: {error}", exc_info=True
        )
        return None


def save_game_sgf(target_id: str) -> Optional[str]:
    """Save current game SGF to file in game-specific folder
    Updates the same SGF file for the same game session (same game_id)
    """
    if target_id not in game_states:
        return None

    state = game_states[target_id]
    sgf_game = state["sgf_game"]

    try:
        current_file = Path(__file__)
        project_root = current_file.parent.parent.parent
        static_dir = project_root / "static"

        # Get or create game ID
        game_id = get_game_id(target_id)

        # Create game-specific folder
        game_dir = static_dir / game_id
        game_dir.mkdir(parents=True, exist_ok=True)

        # Use fixed filename for the same game (no timestamp, so it gets overwritten)
        filename = f"game_{target_id}.sgf"
        file_path = game_dir / filename

        with open(file_path, "wb") as f:
            f.write(sgf_game.serialise())

        logger.info(f"Saved/Updated game SGF to {file_path}")
        return str(file_path)
    except Exception as error:
        logger.error(f"Failed to save game SGF: {error}", exc_info=True)
        return None


def reset_game_state(target_id: str):
    """Reset game state for a target and create new game ID
    Note: This function does NOT change vs_ai_mode status, which is stored separately
    in the vs_ai_modes dictionary.
    """
    if target_id in game_states:
        game_states[target_id] = {
            "game": GoBoard(),
            "current_turn": 1,
            "sgf_game": sgf.Sgf_game(size=19),
        }
        # Generate new game ID for new game
        game_ids[target_id] = f"game_{int(time.time())}"
        logger.info(
            f"Reset game state for {target_id}, new game ID: {game_ids[target_id]}"
        )
