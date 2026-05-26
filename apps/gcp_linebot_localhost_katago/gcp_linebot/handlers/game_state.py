import asyncio
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

from sgfmill import sgf

from config import config
from logger import logger
from handlers.go_engine import GoBoard


# ============================================================================
# State persistence functions (GCS-based, for Cloud Run stateless instances)
# ============================================================================


async def save_state_to_gcs(target_id: str, state_data: Dict[str, Any]) -> bool:
    """Save game state to GCS with no-cache to prevent caching issues"""
    try:
        from services.storage import upload_buffer
        import json

        remote_path = f"target_{target_id}/state/game_state.json"
        state_json = json.dumps(state_data, default=str).encode("utf-8")
        logger.info(f"save_state_to_gcs: state_json = {state_json.decode('utf-8')}")

        # 設定快取控制：no-store 確保每次都要回源伺服器檢查
        # 這樣可以避免公開 URL 的快取問題
        await upload_buffer(
            state_json,
            remote_path,
            content_type="application/json",
            cache_control="no-store",
        )
        logger.debug(f"Saved game state for {target_id} to GCS (with no-cache)")
        return True
    except Exception as error:
        logger.error(
            f"Failed to save state to GCS for {target_id}: {error}", exc_info=True
        )
        return False


async def load_state_from_gcs(target_id: str) -> Optional[Dict[str, Any]]:
    """Load game state from GCS using SDK (bypasses public cache)"""
    try:
        from services.storage import download_file_as_text, file_exists
        import json

        remote_path = f"target_{target_id}/state/game_state.json"
        if not await file_exists(remote_path):
            return None

        # 使用 SDK 讀取會直接繞過公開快取層，保證拿到最新版
        state_text = await download_file_as_text(remote_path)
        state_data = json.loads(state_text)
        logger.debug(f"Loaded game state for {target_id} from GCS: {state_data}")
        return state_data
    except Exception as error:
        logger.error(
            f"Failed to load state from GCS for {target_id}: {error}", exc_info=True
        )
        return None


async def save_sgf_file_path(target_id: str, sgf_path: str, file_name: str) -> bool:
    """Save SGF file path to GCS"""
    try:
        from services.storage import upload_buffer
        import json

        remote_path = f"target_{target_id}/state/sgf_file_path.json"
        data = {"sgf_path": sgf_path, "file_name": file_name}
        data_json = json.dumps(data).encode("utf-8")
        await upload_buffer(data_json, remote_path)
        logger.debug(f"Saved SGF file path for {target_id} to GCS")
        return True
    except Exception as error:
        logger.error(
            f"Failed to save SGF file path to GCS for {target_id}: {error}",
            exc_info=True,
        )
        return False


async def load_sgf_file_path(target_id: str) -> Optional[Dict[str, str]]:
    """Load SGF file path from GCS"""
    try:
        from services.storage import download_file, file_exists
        import json

        remote_path = f"target_{target_id}/state/sgf_file_path.json"
        if not await file_exists(remote_path):
            return None

        data_bytes = await download_file(remote_path)
        data = json.loads(data_bytes.decode("utf-8"))
        logger.debug(f"Loaded SGF file path for {target_id} from GCS")
        return data
    except Exception as error:
        logger.error(
            f"Failed to load SGF file path from GCS for {target_id}: {error}",
            exc_info=True,
        )
        return None


async def get_game_id(target_id: str) -> str:
    """Get or create game ID for a target (user/group/room)
    Game ID is a unique identifier for each game session.
    """
    state = await load_state_from_gcs(target_id)
    if state and "game_id" in state:
        return state["game_id"]

    # Generate new game ID (timestamp-based)
    new_game_id = f"game_{int(time.time())}"
    # Save to GCS, preserving existing fields like vs_ai_mode
    existing_state = await load_state_from_gcs(target_id)
    if existing_state is None:
        existing_state = {}
    existing_state["game_id"] = new_game_id
    existing_state["current_turn"] = 1
    await save_state_to_gcs(target_id, existing_state)
    logger.info(f"Created new game ID for {target_id}: {new_game_id}")
    return new_game_id


async def enable_vs_ai_mode(target_id: str) -> bool:
    """Enable VS AI mode for a target"""
    try:
        state = await load_state_from_gcs(target_id)
        if state is None:
            state = {}

        state["vs_ai_mode"] = True
        success = await save_state_to_gcs(target_id, state)
        if success:
            logger.info(f"Enabled VS AI mode for {target_id}")
        return success
    except Exception as error:
        logger.error(f"Failed to enable VS AI mode for {target_id}: {error}", exc_info=True)
        return False


async def disable_vs_ai_mode(target_id: str) -> bool:
    """Disable VS AI mode for a target"""
    try:
        state = await load_state_from_gcs(target_id)
        if state is None:
            state = {}

        state["vs_ai_mode"] = False
        success = await save_state_to_gcs(target_id, state)
        if success:
            logger.info(f"Disabled VS AI mode for {target_id}")
        return success
    except Exception as error:
        logger.error(f"Failed to disable VS AI mode for {target_id}: {error}", exc_info=True)
        return False


async def is_vs_ai_mode(target_id: str) -> bool:
    """Check if VS AI mode is enabled for a target"""
    try:
        state = await load_state_from_gcs(target_id)
        if state is None:
            return False
        return state.get("vs_ai_mode", False)
    except Exception as error:
        logger.error(f"Failed to check VS AI mode for {target_id}: {error}", exc_info=True)
        return False


async def get_game_state(target_id: str) -> Dict[str, Any]:
    """Get or create game state for a target (user/group/room)

    Loads from GCS: tries to restore from latest SGF file, or creates a new game.
    """
    # Load state metadata from GCS
    state_meta = await load_state_from_gcs(target_id)

    if state_meta and "game_id" in state_meta:
        game_id = state_meta["game_id"]
        # Try to load SGF from GCS
        from services.storage import download_file, file_exists

        sgf_remote_path = f"target_{target_id}/boards/{game_id}/game.sgf"
        if await file_exists(sgf_remote_path):
            try:
                sgf_bytes = await download_file(sgf_remote_path)
                sgf_game = sgf.Sgf_game.from_bytes(sgf_bytes)
                restored = restore_game_from_sgf_object(sgf_game)
                if restored:
                    # Use current_turn from SGF restoration (it's calculated from moves)
                    # Only use metadata as fallback if SGF restoration didn't provide it
                    if "current_turn" not in restored:
                        if "current_turn" in state_meta:
                            restored["current_turn"] = state_meta["current_turn"]
                            logger.warning(
                                f"Using current_turn from metadata ({state_meta['current_turn']}) "
                                f"because SGF restoration didn't provide it"
                            )
                    else:
                        # Log if there's a mismatch (for debugging)
                        if "current_turn" in state_meta:
                            sgf_turn = restored["current_turn"]
                            meta_turn = state_meta["current_turn"]
                            if sgf_turn != meta_turn:
                                logger.warning(
                                    f"current_turn mismatch: SGF={sgf_turn}, metadata={meta_turn}. "
                                    f"Using SGF value ({sgf_turn})"
                                )
                    logger.info(f"Restored game state for {target_id} from GCS SGF")
                    return restored
            except Exception as error:
                logger.warning(
                    f"Failed to restore from GCS SGF for {target_id}: {error}"
                )

    # Create new game
    game_id = await get_game_id(target_id)
    new_state = {
        "game": GoBoard(),
        "current_turn": 1,  # 1=黑, 2=白
        "sgf_game": sgf.Sgf_game(size=19),
    }
    logger.info(f"Created new game state for {target_id}")
    return new_state


def restore_game_from_sgf_object(sgf_game: sgf.Sgf_game) -> Optional[Dict[str, Any]]:
    """Restore game state from an SGF game object"""
    try:
        # Rebuild board state from SGF
        game = GoBoard()
        current_turn = 1  # Start with black
        last_move_coords = None

        # Check if SGF specifies who starts (PL property)
        root = sgf_game.get_root()
        if root.has_property("PL"):
            pl_value = root.get("PL")
            if isinstance(pl_value, (list, tuple)) and len(pl_value) > 0:
                pl_value = pl_value[0]
            if pl_value in ("B", "b"):
                current_turn = 1  # Black starts
            elif pl_value in ("W", "w"):
                current_turn = 2  # White starts
            logger.debug(
                f"SGF specifies PL={pl_value}, starting with {'black' if current_turn == 1 else 'white'}"
            )

        # Traverse SGF to rebuild board
        move_count = 0
        sequence = sgf_game.get_main_sequence()
        logger.debug(f"SGF main sequence has {len(sequence)} nodes")

        # Variables to store last move info
        last_move_info = None

        for node_idx, node in enumerate(sequence):
            color, move = node.get_move()

            # Log all nodes, even if they don't have moves
            if move is None:
                logger.debug(f"Node {node_idx}: no move (color={color}, move={move})")
                continue

            move_count += 1
            # move is (sgf_row, sgf_col), where sgf_row 0 is bottom
            sgf_r, sgf_c = move

            # Convert to engine coordinates (row 0 is top)
            r = 18 - sgf_r
            c = sgf_c

            last_move_coords = (r, c)

            # Validate color value - sgfmill returns "b" or "w" (lowercase)
            if color is None:
                logger.warning(
                    f"Move {move_count}: color is None, using expected turn (current_turn={current_turn})"
                )
                stone_val = current_turn
            elif color not in ("b", "w"):
                logger.warning(
                    f"Move {move_count}: Invalid color '{color}' in SGF, using expected turn (current_turn={current_turn})"
                )
                stone_val = current_turn
            else:
                stone_val = 1 if color == "b" else 2

            # Store last move info (will be logged after loop)
            last_move_info = {
                "move_count": move_count,
                "color": color,
                "stone_val": stone_val,
                "r": r,
                "c": c,
                "expected_turn": current_turn
            }

            # Check if position is already occupied (shouldn't happen in valid SGF, but handle it)
            if game.board[r][c] != 0:
                existing_stone = game.board[r][c]
                logger.warning(
                    f"Move {move_count}: Position ({r}, {c}) already occupied with stone_val={existing_stone}, "
                    f"attempting to place stone_val={stone_val}. This may indicate a problem in SGF."
                )
                # Continue anyway - overwrite (this might be intentional in some SGF formats)

            # Use the same logic as place_stone to ensure consistency
            # 1. Place stone temporarily
            game.board[r][c] = stone_val

            # 2. Check for captured opponent stones
            opponent = 2 if stone_val == 1 else 1
            captured_stones = set()  # Use set to avoid duplicates
            neighbors = [(r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)]
            for nr, nc in neighbors:
                if 0 <= nr < 19 and 0 <= nc < 19:
                    if game.board[nr][nc] == opponent:
                        group, libs = game.get_group_and_liberties(nr, nc)
                        if libs == 0:
                            # Add all stones in the captured group
                            captured_stones.update(group)
                            logger.debug(
                                f"Move {move_count}: Capturing {len(group)} stones at group starting from ({nr}, {nc})"
                            )

            # 3. Remove captured stones
            if captured_stones:
                logger.info(
                    f"Move {move_count}: Removing {len(captured_stones)} captured stones"
                )
            for cr, cc in captured_stones:
                game.board[cr][cc] = 0

            # 4. Check for suicide (shouldn't happen in valid SGF, but we check anyway)
            my_group, my_libs = game.get_group_and_liberties(r, c)
            if my_libs == 0 and len(captured_stones) == 0:
                # Suicide move - this shouldn't happen in valid SGF, but restore it anyway
                logger.warning(
                    f"Move {move_count}: Suicide move detected at ({r}, {c}) in SGF, keeping it for restoration"
                )

            # 5. Update ko point
            if len(captured_stones) == 1 and my_libs == 1:
                # Get the single captured stone position
                captured_pos = list(captured_stones)[0]
                game.ko_point = captured_pos
                logger.debug(f"Move {move_count}: Ko point set to {captured_pos}")
            else:
                game.ko_point = None

            # Switch turn for next move
            current_turn = 2 if stone_val == 1 else 1

        # Log only the last move
        if last_move_info:
            logger.info(
                f"Restoring move {last_move_info['move_count']}: color={last_move_info['color']}, "
                f"stone_val={last_move_info['stone_val']}, pos=({last_move_info['r']},{last_move_info['c']}), "
                f"expected_turn={last_move_info['expected_turn']}"
            )

        logger.info(
            f"Restored {move_count} moves from SGF. Final turn: {'black' if current_turn == 1 else 'white'}"
        )

        return {
            "game": game,
            "current_turn": current_turn,
            "sgf_game": sgf_game,
        }
    except Exception as error:
        logger.error(f"Failed to restore game from SGF object: {error}", exc_info=True)
        return None


def restore_game_from_sgf_file(sgf_path: str) -> Optional[Dict[str, Any]]:
    """Restore game state from a specific SGF file path"""
    try:
        # Load SGF file
        with open(sgf_path, "rb") as f:
            sgf_game = sgf.Sgf_game.from_bytes(f.read())

        # Use the helper function to restore from SGF object
        return restore_game_from_sgf_object(sgf_game)
    except Exception as error:
        logger.error(
            f"Failed to restore game from SGF file {sgf_path}: {error}", exc_info=True
        )
        return None


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


async def save_game_sgf(
    target_id: str, state: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """Save current game SGF to GCS
    Path structure: target_{target_id}/boards/{game_id}/game.sgf
    Updates the same SGF file for the same game session (same game_id)
    Also saves state metadata (game_id, current_turn) to GCS
    """
    if state is None:
        state = await get_game_state(target_id)

    sgf_game = state["sgf_game"]
    current_turn = state.get("current_turn", 1)

    try:
        from services.storage import upload_buffer

        # Get or create game ID
        game_id = await get_game_id(target_id)

        # Use fixed filename for the same game
        filename = "game.sgf"
        remote_path = f"target_{target_id}/boards/{game_id}/{filename}"

        # Serialize SGF and upload to GCS
        sgf_bytes = sgf_game.serialise()
        # 設定快取控制：no-cache 確保每次都要回源伺服器檢查，避免快取問題
        gcs_path = await upload_buffer(
            sgf_bytes,
            remote_path,
            content_type="application/x-go-sgf",
            cache_control="no-cache, max-age=0",
        )

        # Save state metadata to GCS, preserving existing fields like vs_ai_mode
        existing_state = await load_state_from_gcs(target_id)
        if existing_state is None:
            existing_state = {}
        existing_state["game_id"] = game_id
        existing_state["current_turn"] = current_turn
        await save_state_to_gcs(target_id, existing_state)

        logger.info(f"Saved/Updated game SGF to {gcs_path}")
        return gcs_path
    except Exception as error:
        logger.error(f"Failed to save game SGF: {error}", exc_info=True)
        return None


async def reset_game_state(target_id: str, reply_token: Optional[str] = None):
    """Reset game state for a target and create new game ID

    Args:
        target_id: The target ID (user/group/room)
        reply_token: Optional reply token (not used, kept for compatibility)
    """
    # Generate new game ID for new game
    new_game_id = f"game_{int(time.time())}"

    # Save new state metadata to GCS, preserving existing fields like vs_ai_mode
    existing_state = await load_state_from_gcs(target_id)
    if existing_state is None:
        existing_state = {}
    existing_state["game_id"] = new_game_id
    existing_state["current_turn"] = 1
    # Note: vs_ai_mode is preserved (not reset)
    await save_state_to_gcs(target_id, existing_state)

    # Save empty SGF to GCS
    new_sgf = sgf.Sgf_game(size=19)
    from services.storage import upload_buffer

    sgf_bytes = new_sgf.serialise()
    remote_path = f"target_{target_id}/boards/{new_game_id}/game.sgf"
    # 設定快取控制：no-cache 確保每次都要回源伺服器檢查，避免快取問題
    await upload_buffer(
        sgf_bytes,
        remote_path,
        content_type="application/x-go-sgf",
        cache_control="no-cache, max-age=0",
    )

    logger.info(f"Reset game state for {target_id}, new game ID: {new_game_id}")
