import asyncio
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any

from linebot.v3.messaging.models import TextMessage, ImageMessage
from linebot.v3.messaging import ReplyMessageRequest

from config import config
from logger import logger
from handlers.game_state import (
    game_states, game_ids,
    get_game_state, get_game_id, reset_game_state, save_game_sgf,
    restore_game_from_sgf_file, create_sgf_with_first_n_moves,
    is_vs_ai_mode, enable_vs_ai_mode, disable_vs_ai_mode,
)
from handlers.line_sender import line_bot_api, send_message, is_valid_https_url, encode_url_path
from handlers.board_visualizer import BoardVisualizer
from handlers.katago_handler import run_katago_analysis
from handlers.go_engine import GoBoard
from sgfmill import sgf


_current_file = Path(__file__)
_project_root = _current_file.parent.parent.parent
_assets_dir = _project_root / "assets"
visualizer = BoardVisualizer(assets_dir=str(_assets_dir))


async def handle_board_move(
    target_id: str, reply_token: Optional[str], coord_text: str, source: Dict[str, Any]
):
    """Handle board coordinate input and draw board"""
    try:
        # Get game state for this target
        state = get_game_state(target_id)
        game = state["game"]
        current_turn = state["current_turn"]
        sgf_game = state["sgf_game"]

        # Place stone
        success, msg = game.place_stone(coord_text, current_turn)

        if not success:
            # Failed to place stone, send error message
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=f"提示：{msg}")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        # Successfully placed stone
        coords = game.parse_coordinates(coord_text)

        # --- 1. Update SGF record ---
        node = sgf_game.get_last_node()
        new_node = node.new_child()

        color_code = "b" if current_turn == 1 else "w"

        # coords is (row, col), where row 0 is top
        # sgfmill thinks row 0 is bottom, so flip: (19 - 1 - row)
        sgf_row = 18 - coords[0]
        sgf_col = coords[1]

        new_node.set_move(color_code, (sgf_row, sgf_col))

        # Save SGF file
        sgf_path = save_game_sgf(target_id)
        if sgf_path:
            logger.info(f"Saved game SGF: {sgf_path}")

        # --- 2. Switch turn and draw board ---
        state["current_turn"] = 2 if current_turn == 1 else 1

        # Generate board image
        current_file = Path(__file__)
        project_root = current_file.parent.parent.parent
        static_dir = project_root / "static"

        # Get game ID and create game-specific folder
        game_id = get_game_id(target_id)
        game_dir = static_dir / game_id
        game_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time())
        filename = f"board_{target_id}_{timestamp}.png"
        output_path = game_dir / filename

        # Draw board with last move highlighted
        visualizer.draw_board(
            game.board, last_move=coords, output_filename=str(output_path)
        )

        # Get public URL for image
        public_url = config["server"]["public_url"]

        # Check if VS AI mode is enabled
        vs_ai_mode = is_vs_ai_mode(target_id)

        if public_url and is_valid_https_url(public_url):
            # Build image URL (game_id/filename)
            relative_path = f"static/{game_id}/{filename}"
            encoded_path = encode_url_path(relative_path)
            image_url = f"{public_url}/{encoded_path}"

            if is_valid_https_url(image_url):
                # If VS AI mode is enabled, don't reply immediately, wait for AI's move
                if vs_ai_mode:
                    # Call local KataGo GTP function asynchronously (non-blocking)
                    # Pass reply_token and user's board image URL so AI handler can send everything together
                    try:
                        # Get current turn (after user's move, it's AI's turn)
                        ai_current_turn = state["current_turn"]

                        # Spawn async task to get AI's next move
                        asyncio.create_task(
                            handle_ai_next_move(
                                target_id=target_id,
                                sgf_path=sgf_path,
                                current_turn=ai_current_turn,
                                reply_token=reply_token,
                                user_board_image_url=image_url,
                            )
                        )
                        logger.info(f"Spawned AI next move task: target_id={target_id}, current_turn={ai_current_turn}")
                        # Don't send reply here, wait for AI to respond
                        return
                    except Exception as ai_error:
                        logger.error(f"Error spawning AI next move task: {ai_error}", exc_info=True)
                        # If error, fall through to send user's move image

                # Send board image (non-VS AI mode, or error in VS AI mode)
                messages = [
                    ImageMessage(
                        original_content_url=image_url,
                        preview_image_url=image_url,
                    )
                ]
                request = ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=messages,
                )
                await asyncio.to_thread(line_bot_api.reply_message, request)
            else:
                logger.warning(f"Invalid image URL: {image_url}")
                request = ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[
                        TextMessage(
                            text=f"✅ {msg}\n\n⚠️ 圖片 URL 無效，請檢查 PUBLIC_URL 設定"
                        )
                    ],
                )
                await asyncio.to_thread(line_bot_api.reply_message, request)
        else:
            logger.warning(f"PUBLIC_URL not set or invalid: {public_url}")
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[
                    TextMessage(
                        text=f"✅ {msg}\n\n⚠️ 未設定有效的 PUBLIC_URL，無法顯示圖片"
                    )
                ],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)

    except Exception as error:
        logger.error(f"Error handling board move: {error}", exc_info=True)
        request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=f"❌ 處理落子時發生錯誤：{str(error)}")],
        )
        await asyncio.to_thread(line_bot_api.reply_message, request)


async def handle_ai_next_move(
    target_id: str,
    sgf_path: str,
    current_turn: int,
    reply_token: Optional[str] = None,
    user_board_image_url: Optional[str] = None,
):
    """Handle AI's next move in VS AI mode (local execution)

    Args:
        target_id: Target ID
        sgf_path: Path to SGF file
        current_turn: Current turn (AI's turn)
        reply_token: Reply token from user's move (if available)
        user_board_image_url: User's board image URL (if available)
    """
    try:
        from handlers.katago_handler import run_katago_gtp_next_move

        logger.info(f"Getting AI's next move: target_id={target_id}, current_turn={current_turn}")

        # Run KataGo GTP to get next move
        result = await run_katago_gtp_next_move(
            sgf_path=sgf_path,
            current_turn=current_turn,
            visits=400,
        )

        if not result.get("success"):
            error = result.get("error", "Unknown error")
            logger.error(f"KataGo GTP failed: {error}")
            await send_message(
                target_id,
                None,
                [TextMessage(text=f"❌ AI 思考失敗：{error}")],
            )
            return

        # Get the move (in GTP format, e.g., "C15")
        move = result.get("move")
        if not move:
            error_msg = "No move returned from KataGo"
            logger.error(f"KataGo GTP error: {error_msg}")
            await send_message(
                target_id,
                None,
                [TextMessage(text="❌ AI 思考完成但無法取得落子位置")],
            )
            return

        logger.info(f"KataGo returned GTP move: {move}")

        # Get current game state
        state = get_game_state(target_id)
        game = state["game"]
        sgf_game = state["sgf_game"]

        # Parse coordinates first to check if valid
        coords = game.parse_coordinates(move)
        if not coords:
            error_msg = f"Invalid GTP coordinate format: {move}"
            logger.error(error_msg)
            await send_message(
                target_id,
                None,
                [TextMessage(text=f"❌ AI 落子失敗：座標格式錯誤 ({move})")],
            )
            return

        logger.info(f"Parsed GTP move {move} to coordinates: row={coords[0]}, col={coords[1]}")
        logger.info(f"Board state at ({coords[0]}, {coords[1]}): {game.board[coords[0]][coords[1]]}")

        # Place AI's stone (move is in GTP format, parse_coordinates will convert it)
        success, msg = game.place_stone(move, current_turn)

        if not success:
            error_msg = f"Failed to place AI's stone: {msg} (move: {move}, coords: {coords})"
            logger.error(error_msg)
            # Log current board state for debugging
            logger.error(f"Current board state around ({coords[0]}, {coords[1]}):")
            for r in range(max(0, coords[0]-1), min(19, coords[0]+2)):
                row_str = f"Row {r}: "
                for c in range(max(0, coords[1]-1), min(19, coords[1]+2)):
                    row_str += f"({r},{c})={game.board[r][c]} "
                logger.error(row_str)
            await send_message(
                target_id,
                None,
                [TextMessage(text=f"❌ AI 落子失敗：{msg}")],
            )
            return

        # Update SGF record
        node = sgf_game.get_last_node()
        new_node = node.new_child()

        color_code = "b" if current_turn == 1 else "w"

        # coords is (row, col), where row 0 is top
        # sgfmill thinks row 0 is bottom, so flip: (19 - 1 - row)
        sgf_row = 18 - coords[0]
        sgf_col = coords[1]

        new_node.set_move(color_code, (sgf_row, sgf_col))

        # Switch turn (AI's turn is done, now it's user's turn)
        state["current_turn"] = 2 if current_turn == 1 else 1

        # Save SGF file
        save_game_sgf(target_id)

        # Generate board image
        current_file = Path(__file__)
        project_root = current_file.parent.parent.parent
        static_dir = project_root / "static"

        game_id = get_game_id(target_id)
        game_dir = static_dir / game_id
        game_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time())
        filename = f"board_ai_{target_id}_{timestamp}.png"
        output_path = game_dir / filename

        visualizer.draw_board(
            game.board, last_move=coords, output_filename=str(output_path)
        )

        # Get public URL for image
        public_url = config["server"]["public_url"]

        # Send AI's move image and prompt for user's next move
        if public_url and is_valid_https_url(public_url):
            relative_path = f"static/{game_id}/{filename}"
            encoded_path = encode_url_path(relative_path)
            image_url = f"{public_url}/{encoded_path}"

            if is_valid_https_url(image_url):
                turn_text = "黑" if state["current_turn"] == 1 else "白"
                messages = []

                # If we have user's board image, include it first
                if user_board_image_url:
                    messages.append(
                        ImageMessage(
                            original_content_url=user_board_image_url,
                            preview_image_url=user_board_image_url,
                        )
                    )

                # Add AI's move
                messages.extend([
                    TextMessage(text=f"🤖 AI 下在 {move}"),
                    ImageMessage(
                        original_content_url=image_url,
                        preview_image_url=image_url,
                    ),
                    TextMessage(text=f"現在輪到您（{turn_text}）下棋。"),
                ])
                await send_message(target_id, reply_token, messages)
            else:
                logger.warning(f"Invalid image URL: {image_url}")
                turn_text = "黑" if state["current_turn"] == 1 else "白"
                await send_message(
                    target_id,
                    None,
                    [
                        TextMessage(
                            text=f"🤖 AI 下在 {move}\n\n現在輪到您（{turn_text}）下棋。\n\n⚠️ 圖片 URL 無效"
                        )
                    ],
                )
        else:
            turn_text = "黑" if state["current_turn"] == 1 else "白"
            await send_message(
                target_id,
                None,
                [
                    TextMessage(
                        text=f"🤖 AI 下在 {move}\n\n現在輪到您（{turn_text}）下棋。\n\n⚠️ 未設定有效的 PUBLIC_URL"
                    )
                ],
            )

    except Exception as error:
        logger.error(f"Error in handle_ai_next_move: {error}", exc_info=True)
        await send_message(
            target_id,
            None,
            [TextMessage(text=f"❌ AI 思考時發生錯誤：{str(error)}")],
        )


async def handle_undo_move(target_id: str, reply_token: Optional[str]):
    """Handle undo move (悔棋)"""
    try:
        if target_id not in game_states:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="目前沒有進行中的對局，無法悔棋。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        state = game_states[target_id]
        sgf_game = state["sgf_game"]

        # Get last node
        last_node = sgf_game.get_last_node()
        parent_node = last_node.parent

        # Check if it's root node (can't undo)
        if parent_node is None:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="目前是初始狀態，無法悔棋。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        try:
            # Delete last move from SGF
            last_node.delete()

            # Save updated SGF
            save_game_sgf(target_id)

            # Restore game state from updated SGF
            game_id = get_game_id(target_id)
            current_file = Path(__file__)
            project_root = current_file.parent.parent.parent
            static_dir = project_root / "static"
            sgf_path = static_dir / game_id / f"game_{target_id}.sgf"

            if sgf_path.exists():
                restored = restore_game_from_sgf_file(str(sgf_path))
                if restored:
                    game_states[target_id] = restored
                    state = restored
                else:
                    # If restore failed, reset to empty board
                    game_states[target_id] = {
                        "game": GoBoard(),
                        "current_turn": 1,
                        "sgf_game": sgf.Sgf_game(size=19),
                    }
                    state = game_states[target_id]
            else:
                # If SGF doesn't exist, reset to empty board
                game_states[target_id] = {
                    "game": GoBoard(),
                    "current_turn": 1,
                    "sgf_game": sgf.Sgf_game(size=19),
                }
                state = game_states[target_id]

            game = state["game"]
            current_turn = state["current_turn"]

            # Find last move coordinates for highlighting from SGF sequence
            last_coords = None
            sgf_game = state["sgf_game"]
            sequence = sgf_game.get_main_sequence()
            # Traverse sequence backwards to find the last move
            for node in reversed(sequence):
                color, move = node.get_move()
                if move is not None:
                    # move is (sgf_row, sgf_col), where sgf_row 0 is bottom
                    sgf_r, sgf_c = move
                    # Convert to engine coordinates (row 0 is top)
                    r = 18 - sgf_r
                    c = sgf_c
                    last_coords = (r, c)
                    break  # Found the last move, exit loop

            # Draw board
            game_id = get_game_id(target_id)
            game_dir = static_dir / game_id
            game_dir.mkdir(parents=True, exist_ok=True)

            timestamp = int(time.time())
            filename = f"board_undo_{target_id}_{timestamp}.png"
            output_path = game_dir / filename

            visualizer.draw_board(
                game.board, last_move=last_coords, output_filename=str(output_path)
            )

            # Send board image
            public_url = config["server"]["public_url"]
            turn_text = "黑" if current_turn == 1 else "白"

            if public_url and is_valid_https_url(public_url):
                relative_path = f"static/{game_id}/{filename}"
                encoded_path = encode_url_path(relative_path)
                image_url = f"{public_url}/{encoded_path}"

                if is_valid_https_url(image_url):
                    request = ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[
                            TextMessage(text=f"↩️ 已悔棋一步。\n現在輪到：{turn_text}"),
                            ImageMessage(
                                original_content_url=image_url,
                                preview_image_url=image_url,
                            ),
                        ],
                    )
                    await asyncio.to_thread(line_bot_api.reply_message, request)
                else:
                    request = ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[
                            TextMessage(
                                text=f"↩️ 已悔棋一步。\n現在輪到：{turn_text}\n\n⚠️ 圖片 URL 無效"
                            )
                        ],
                    )
                    await asyncio.to_thread(line_bot_api.reply_message, request)
            else:
                request = ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[
                        TextMessage(
                            text=f"↩️ 已悔棋一步。\n現在輪到：{turn_text}\n\n⚠️ 未設定有效的 PUBLIC_URL"
                        )
                    ],
                )
                await asyncio.to_thread(line_bot_api.reply_message, request)

        except Exception as e:
            logger.error(f"Error undoing move: {e}", exc_info=True)
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=f"悔棋失敗：{str(e)}")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)

    except Exception as error:
        logger.error(f"Error handling undo move: {error}", exc_info=True)
        request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=f"❌ 處理悔棋時發生錯誤：{str(error)}")],
        )
        await asyncio.to_thread(line_bot_api.reply_message, request)


async def handle_load_game_by_id(target_id: str, reply_token: Optional[str], game_id: str):
    """Handle load game by game ID (讀取 {gameid})"""
    try:
        current_file = Path(__file__)
        project_root = current_file.parent.parent.parent
        static_dir = project_root / "static"

        if not static_dir.exists():
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="找不到存檔。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        # Find SGF file for this game_id
        sgf_path = static_dir / game_id / f"game_{target_id}.sgf"

        if not sgf_path.exists():
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=f"找不到 game_id 為 {game_id} 的棋譜。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        # Restore game state
        restored = restore_game_from_sgf_file(str(sgf_path))
        if not restored:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="讀取失敗：無法解析棋譜檔案。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        # Update game_id
        game_ids[target_id] = game_id

        game_states[target_id] = restored
        state = restored
        game = state["game"]
        current_turn = state["current_turn"]

        # Preserve vs_ai_mode state (it's stored separately in vs_ai_modes dict)
        # vs_ai_mode state is already in memory, no need to restore it
        # The state will remain as it was before loading the game

        # Find last move coordinates for highlighting and build move_numbers dict
        last_coords = None
        move_numbers = {}  # {(row, col): move_number}
        sgf_game = state["sgf_game"]
        sequence = sgf_game.get_main_sequence()
        move_num = 0

        # Traverse sequence to build move_numbers and find last move
        for node in sequence:
            color, move = node.get_move()
            if move is not None:
                move_num += 1
                # move is (sgf_row, sgf_col), where sgf_row 0 is bottom
                sgf_r, sgf_c = move
                # Convert to engine coordinates (row 0 is top)
                r = 18 - sgf_r
                c = sgf_c
                move_numbers[(r, c)] = move_num
                last_coords = (r, c)  # Last move will be the final one

        # Draw board
        game_dir = static_dir / game_id
        timestamp = int(time.time())
        filename = f"board_restored_{target_id}_{timestamp}.png"
        output_path = game_dir / filename

        visualizer.draw_board(
            game.board, last_move=last_coords, output_filename=str(output_path), move_numbers=move_numbers
        )

        # Send board image
        public_url = config["server"]["public_url"]
        turn_text = "黑" if current_turn == 1 else "白"
        total_moves = len(move_numbers)
        total_moves_text = f"總手數：{total_moves} 手"

        if public_url and is_valid_https_url(public_url):
            relative_path = f"static/{game_id}/{filename}"
            encoded_path = encode_url_path(relative_path)
            image_url = f"{public_url}/{encoded_path}"

            if is_valid_https_url(image_url):
                request = ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[
                        TextMessage(text=f"📂 已讀取棋譜 (game_id: {game_id})！\n{total_moves_text}\n目前輪到：{turn_text}"),
                        ImageMessage(
                            original_content_url=image_url,
                            preview_image_url=image_url,
                        ),
                    ],
                )
                await asyncio.to_thread(line_bot_api.reply_message, request)
            else:
                request = ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[
                        TextMessage(
                            text=f"📂 已讀取棋譜 (game_id: {game_id})！\n{total_moves_text}\n目前輪到：{turn_text}\n\n⚠️ 圖片 URL 無效"
                        )
                    ],
                )
                await asyncio.to_thread(line_bot_api.reply_message, request)
        else:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[
                    TextMessage(
                        text=f"📂 已讀取棋譜 (game_id: {game_id})！\n{total_moves_text}\n目前輪到：{turn_text}\n\n⚠️ 未設定有效的 PUBLIC_URL"
                    )
                ],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)

    except Exception as error:
        logger.error(f"Error handling load game by ID: {error}", exc_info=True)
        request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=f"讀取失敗：{str(error)}")],
        )
        await asyncio.to_thread(line_bot_api.reply_message, request)


async def handle_load_game_by_id_with_moves(
    target_id: str, reply_token: Optional[str], source_game_id: str, move_count: int
):
    """Handle load game by game ID with move count (讀取 {gameid} {手數})

    This function:
    1. Loads the SGF file for the specified game_id
    2. Extracts only the first N moves
    3. Creates a new game_id
    4. Saves the truncated SGF file
    5. Updates state to the new game_id
    """
    try:
        current_file = Path(__file__)
        project_root = current_file.parent.parent.parent
        static_dir = project_root / "static"

        if not static_dir.exists():
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="找不到存檔。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        # Find SGF file for the source game_id
        source_sgf_path = static_dir / source_game_id / f"game_{target_id}.sgf"

        if not source_sgf_path.exists():
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=f"找不到 game_id 為 {source_game_id} 的棋譜。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        # Load source SGF file
        with open(source_sgf_path, "rb") as f:
            source_sgf_game = sgf.Sgf_game.from_bytes(f.read())

        # Get main sequence to count total moves
        sequence = source_sgf_game.get_main_sequence()
        total_moves = sum(1 for node in sequence if node.get_move()[1] is not None)

        if move_count > total_moves:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=f"該棋譜只有 {total_moves} 手，無法讀取到第 {move_count} 手。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        # Create new SGF with only first N moves
        truncated_sgf = create_sgf_with_first_n_moves(source_sgf_game, move_count)

        # Create new game_id for the truncated game
        new_game_id = f"game_{int(time.time())}"
        game_ids[target_id] = new_game_id

        # Save truncated SGF to new game_id folder
        new_game_dir = static_dir / new_game_id
        new_game_dir.mkdir(parents=True, exist_ok=True)
        new_sgf_path = new_game_dir / f"game_{target_id}.sgf"

        with open(new_sgf_path, "wb") as f:
            f.write(truncated_sgf.serialise())

        logger.info(f"Created truncated SGF with {move_count} moves: {new_sgf_path}")

        # Restore game state from truncated SGF
        restored = restore_game_from_sgf_file(str(new_sgf_path))
        if not restored:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="讀取失敗：無法解析棋譜檔案。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        game_states[target_id] = restored
        state = restored
        game = state["game"]
        current_turn = state["current_turn"]

        # Preserve vs_ai_mode state
        # vs_ai_mode state is already in memory, no need to restore it

        # Find last move coordinates for highlighting and build move_numbers dict
        last_coords = None
        move_numbers = {}  # {(row, col): move_number}
        sgf_game = state["sgf_game"]
        sequence = sgf_game.get_main_sequence()
        move_num = 0

        # Traverse sequence to build move_numbers and find last move
        for node in sequence:
            color, move = node.get_move()
            if move is not None:
                move_num += 1
                # move is (sgf_row, sgf_col), where sgf_row 0 is bottom
                sgf_r, sgf_c = move
                # Convert to engine coordinates (row 0 is top)
                r = 18 - sgf_r
                c = sgf_c
                move_numbers[(r, c)] = move_num
                last_coords = (r, c)  # Last move will be the final one

        # Draw board
        timestamp = int(time.time())
        filename = f"board_restored_{target_id}_{timestamp}.png"
        output_path = new_game_dir / filename

        visualizer.draw_board(
            game.board, last_move=last_coords, output_filename=str(output_path), move_numbers=move_numbers
        )

        # Send board image
        public_url = config["server"]["public_url"]
        turn_text = "黑" if current_turn == 1 else "白"
        total_moves_text = f"總手數：{move_count} 手"

        if public_url and is_valid_https_url(public_url):
            relative_path = f"static/{new_game_id}/{filename}"
            encoded_path = encode_url_path(relative_path)
            image_url = f"{public_url}/{encoded_path}"

            if is_valid_https_url(image_url):
                request = ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[
                        TextMessage(
                            text=f"📂 已讀取棋譜 (game_id: {source_game_id}) 前 {move_count} 手！\n新對局 game_id: {new_game_id}\n{total_moves_text}\n目前輪到：{turn_text}"
                        ),
                        ImageMessage(
                            original_content_url=image_url,
                            preview_image_url=image_url,
                        ),
                    ],
                )
                await asyncio.to_thread(line_bot_api.reply_message, request)
            else:
                request = ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[
                        TextMessage(
                            text=f"📂 已讀取棋譜 (game_id: {source_game_id}) 前 {move_count} 手！\n新對局 game_id: {new_game_id}\n{total_moves_text}\n目前輪到：{turn_text}\n\n⚠️ 圖片 URL 無效"
                        )
                    ],
                )
                await asyncio.to_thread(line_bot_api.reply_message, request)
        else:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[
                    TextMessage(
                        text=f"📂 已讀取棋譜 (game_id: {source_game_id}) 前 {move_count} 手！\n新對局 game_id: {new_game_id}\n{total_moves_text}\n目前輪到：{turn_text}\n\n⚠️ 未設定有效的 PUBLIC_URL"
                    )
                ],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)

    except Exception as error:
        logger.error(f"Error handling load game by ID with moves: {error}", exc_info=True)
        request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=f"讀取失敗：{str(error)}")],
        )
        await asyncio.to_thread(line_bot_api.reply_message, request)


async def handle_load_game(target_id: str, reply_token: Optional[str]):
    """Handle load game (讀取)"""
    try:
        current_file = Path(__file__)
        project_root = current_file.parent.parent.parent
        static_dir = project_root / "static"

        if not static_dir.exists():
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="找不到存檔。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        # Find latest SGF file for this target
        pattern = f"**/game_{target_id}.sgf"
        sgf_files = list(static_dir.glob(pattern))

        if not sgf_files:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="找不到存檔。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        # Get the latest file
        latest_sgf = max(sgf_files, key=lambda p: p.stat().st_mtime)

        # Extract game_id from path
        game_id = latest_sgf.parent.name
        game_ids[target_id] = game_id

        # Restore game state
        restored = restore_game_from_sgf_file(str(latest_sgf))
        if not restored:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="讀取失敗：無法解析棋譜檔案。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        game_states[target_id] = restored
        state = restored
        game = state["game"]
        current_turn = state["current_turn"]

        # Preserve vs_ai_mode state (it's stored separately in vs_ai_modes dict)
        # vs_ai_mode state is already in memory, no need to restore it
        # The state will remain as it was before loading the game

        # Find last move coordinates for highlighting and build move_numbers dict
        last_coords = None
        move_numbers = {}  # {(row, col): move_number}
        sgf_game = state["sgf_game"]
        sequence = sgf_game.get_main_sequence()
        move_num = 0

        # Traverse sequence to build move_numbers and find last move
        for node in sequence:
            color, move = node.get_move()
            if move is not None:
                move_num += 1
                # move is (sgf_row, sgf_col), where sgf_row 0 is bottom
                sgf_r, sgf_c = move
                # Convert to engine coordinates (row 0 is top)
                r = 18 - sgf_r
                c = sgf_c
                move_numbers[(r, c)] = move_num
                last_coords = (r, c)  # Last move will be the final one

        # Draw board
        game_dir = static_dir / game_id
        timestamp = int(time.time())
        filename = f"board_restored_{target_id}_{timestamp}.png"
        output_path = game_dir / filename

        visualizer.draw_board(
            game.board, last_move=last_coords, output_filename=str(output_path), move_numbers=move_numbers
        )

        # Send board image
        public_url = config["server"]["public_url"]
        turn_text = "黑" if current_turn == 1 else "白"
        total_moves = len(move_numbers)
        total_moves_text = f"總手數：{total_moves} 手"

        if public_url and is_valid_https_url(public_url):
            relative_path = f"static/{game_id}/{filename}"
            encoded_path = encode_url_path(relative_path)
            image_url = f"{public_url}/{encoded_path}"

            if is_valid_https_url(image_url):
                request = ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[
                        TextMessage(text=f"📂 已讀取棋譜！\n{total_moves_text}\n目前輪到：{turn_text}"),
                        ImageMessage(
                            original_content_url=image_url,
                            preview_image_url=image_url,
                        ),
                    ],
                )
                await asyncio.to_thread(line_bot_api.reply_message, request)
            else:
                request = ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[
                        TextMessage(
                            text=f"📂 已讀取棋譜！\n{total_moves_text}\n目前輪到：{turn_text}\n\n⚠️ 圖片 URL 無效"
                        )
                    ],
                )
                await asyncio.to_thread(line_bot_api.reply_message, request)
        else:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[
                    TextMessage(
                        text=f"📂 已讀取棋譜！\n{total_moves_text}\n目前輪到：{turn_text}\n\n⚠️ 未設定有效的 PUBLIC_URL"
                    )
                ],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)

    except Exception as error:
        logger.error(f"Error handling load game: {error}", exc_info=True)
        request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=f"讀取失敗：{str(error)}")],
        )
        await asyncio.to_thread(line_bot_api.reply_message, request)
