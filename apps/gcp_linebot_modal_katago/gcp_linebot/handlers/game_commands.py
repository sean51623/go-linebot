import asyncio
import os
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any

from linebot.v3.messaging.models import TextMessage, ImageMessage
from linebot.v3.messaging import ReplyMessageRequest

from config import config
from logger import logger
from handlers.game_state import (
    get_game_state, get_game_id, save_game_sgf,
    restore_game_from_sgf_object,
    is_vs_ai_mode,
    load_state_from_gcs, save_state_to_gcs,
    create_sgf_with_first_n_moves,
)
from handlers.line_sender import line_bot_api, send_message, is_valid_https_url
from handlers.board_visualizer import BoardVisualizer
from handlers.go_engine import GoBoard
from sgfmill import sgf


_current_file = Path(__file__)
_project_root = _current_file.parent.parent
_assets_dir = _project_root / "assets"
visualizer = BoardVisualizer(assets_dir=str(_assets_dir))


async def handle_board_move(
    target_id: str, reply_token: Optional[str], coord_text: str, source: Dict[str, Any]
):
    """Handle board coordinate input and draw board"""
    try:
        # Get game state for this target
        state = await get_game_state(target_id)
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

        # --- 2. Switch turn and update state ---
        state["current_turn"] = 2 if current_turn == 1 else 1

        # Save SGF file and state metadata
        sgf_path = await save_game_sgf(target_id, state)
        if sgf_path:
            logger.info(f"Saved game SGF: {sgf_path}")

        # Generate board image
        import tempfile
        from services.storage import upload_file, get_public_url

        # Get game ID
        game_id = await get_game_id(target_id)

        timestamp = int(time.time())
        filename = f"board_{timestamp}.png"

        # Draw board to temporary file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
            tmp_path = tmp_file.name

        visualizer.draw_board(game.board, last_move=coords, output_filename=tmp_path)

        # Upload to GCS
        remote_path = f"target_{target_id}/boards/{game_id}/{filename}"
        await upload_file(tmp_path, remote_path)

        # Get public URL
        image_url = get_public_url(remote_path)

        # Clean up temporary file
        try:
            os.unlink(tmp_path)
        except:
            pass

        # Check if VS AI mode is enabled
        vs_ai_mode = await is_vs_ai_mode(target_id)

        if is_valid_https_url(image_url):
            # If VS AI mode is enabled, don't reply immediately, wait for AI's move
            if vs_ai_mode:
                # Call Modal function asynchronously (non-blocking)
                # Pass reply_token and user's board image URL so callback can send everything together
                try:
                    import modal
                    modal_app_name = config.get("modal", {}).get("app_name")
                    modal_function_get_ai_next_move = config.get("modal", {}).get("function_get_ai_next_move")
                    callback_get_ai_next_move_url = config.get("cloud_run", {}).get("callback_get_ai_next_move_url")

                    if modal_app_name and modal_function_get_ai_next_move and callback_get_ai_next_move_url:
                        # Get SGF GCS path (save_game_sgf returns gs:// format)
                        sgf_gcs_path = sgf_path if sgf_path and sgf_path.startswith("gs://") else None

                        if not sgf_gcs_path:
                            logger.error(f"Invalid SGF path: {sgf_path}")
                        else:
                            # Get current turn (after user's move, it's AI's turn)
                            ai_current_turn = state["current_turn"]

                            # Spawn Modal function asynchronously
                            # Pass reply_token and user_board_image_url to callback
                            vs_ai_function = modal.Function.from_name(
                                modal_app_name, modal_function_get_ai_next_move
                            )
                            vs_ai_function.spawn(
                                sgf_gcs_path=sgf_gcs_path,
                                callback_url=callback_get_ai_next_move_url,
                                target_id=target_id,
                                current_turn=ai_current_turn,
                                reply_token=reply_token,  # Pass reply_token to callback
                                user_board_image_url=image_url,  # Pass user's board image URL
                            )
                            logger.info(f"Spawned Modal function for VS AI: target_id={target_id}, current_turn={ai_current_turn}")
                            # Don't send reply here, wait for AI callback to respond
                            return
                    else:
                        logger.error("Modal app_name, function_get_ai_next_move, or callback_get_ai_next_move_url not configured")
                except Exception as modal_error:
                    logger.error(f"Error calling Modal function for VS AI: {modal_error}", exc_info=True)
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
                        text=f"✅ {msg}\n\n⚠️ 圖片 URL 無效，請檢查 GCS_BUCKET_NAME 設定"
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


async def handle_undo_move(target_id: str, reply_token: Optional[str]):
    """Handle undo move (悔棋)"""
    try:
        # Get game state
        state = await get_game_state(target_id)
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

            # Restore game state directly from updated SGF object
            restored = restore_game_from_sgf_object(sgf_game)
            if restored:
                state = restored
            else:
                # If restore failed, reset to empty board
                logger.warning(
                    f"Failed to restore game from SGF after undo, resetting to empty board"
                )
                state = {
                    "game": GoBoard(),
                    "current_turn": 1,
                    "sgf_game": sgf.Sgf_game(size=19),
                }

            # Save updated SGF to GCS after restoring state
            await save_game_sgf(target_id, state)

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
            import tempfile
            from services.storage import upload_file, get_public_url

            game_id = await get_game_id(target_id)
            timestamp = int(time.time())
            filename = f"board_undo_{timestamp}.png"

            # Draw board to temporary file
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                tmp_path = tmp_file.name

            visualizer.draw_board(
                game.board, last_move=last_coords, output_filename=tmp_path
            )

            # Upload to GCS
            remote_path = f"target_{target_id}/boards/{game_id}/{filename}"
            await upload_file(tmp_path, remote_path)

            # Get public URL
            image_url = get_public_url(remote_path)

            # Clean up temporary file
            try:
                os.unlink(tmp_path)
            except:
                pass

            turn_text = "黑" if current_turn == 1 else "白"

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


async def handle_load_game_by_id(
    target_id: str, reply_token: Optional[str], game_id: Optional[str] = None
):
    """Handle load game by game ID (讀取 {gameid}) - Load specific game by game_id
    If game_id is None, loads the current game from state metadata
    """
    try:
        # If game_id is not provided, get it from state metadata
        state_meta = None
        if game_id is None:
            state_meta = await load_state_from_gcs(target_id)
            if not state_meta or "game_id" not in state_meta:
                request = ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text="找不到存檔。")],
                )
                await asyncio.to_thread(line_bot_api.reply_message, request)
                return
            game_id = state_meta["game_id"]

        # Load SGF from GCS using the game_id
        from services.storage import download_file, file_exists, get_public_url

        sgf_remote_path = f"target_{target_id}/boards/{game_id}/game.sgf"
        if not await file_exists(sgf_remote_path):
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=f"找不到 game_id 為 {game_id} 的棋譜。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        # Download and restore game state
        sgf_bytes = await download_file(sgf_remote_path)
        sgf_game = sgf.Sgf_game.from_bytes(sgf_bytes)
        restored = restore_game_from_sgf_object(sgf_game)

        if not restored:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="讀取失敗：無法解析棋譜檔案。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        state = restored
        game = state["game"]
        current_turn = state["current_turn"]

        # Always update state.json with restored state from SGF when loading any game
        # This ensures state.json reflects the actual state from SGF, not the old cached value
        # If loading a historical game, this will switch the current game to that historical game
        # Preserve vs_ai_mode from existing state if it exists
        existing_state = await load_state_from_gcs(target_id)
        vs_ai_mode = existing_state.get("vs_ai_mode", False) if existing_state else False

        await save_state_to_gcs(
            target_id,
            {
                "game_id": game_id,
                "current_turn": current_turn,
                "vs_ai_mode": vs_ai_mode,  # Preserve vs_ai_mode state
            },
        )
        logger.info(
            f"Updated state.json for {target_id} with restored state from SGF: game_id={game_id}, current_turn={current_turn}"
        )

        # Find last move coordinates for highlighting and build move_numbers dict
        # Get the last move from SGF sequence and build move_numbers
        last_coords = None
        move_numbers = {}  # {(row, col): move_number}
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
        import tempfile
        from services.storage import upload_file

        timestamp = int(time.time())
        filename = f"board_restored_{timestamp}.png"

        # Draw board to temporary file with move numbers
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
            tmp_path = tmp_file.name

        visualizer.draw_board(
            game.board, last_move=last_coords, output_filename=tmp_path, move_numbers=move_numbers
        )

        # Upload to GCS
        remote_path = f"target_{target_id}/boards/{game_id}/{filename}"
        await upload_file(tmp_path, remote_path)

        # Get public URL
        image_url = get_public_url(remote_path)

        # Clean up temporary file
        try:
            os.unlink(tmp_path)
        except:
            pass

        turn_text = "黑" if current_turn == 1 else "白"
        total_moves = len(move_numbers)
        total_moves_text = f"總手數：{total_moves} 手"

        # Format message text based on whether game_id was provided
        if game_id:
            message_text = f"📂 已讀取棋譜 (game_id: {game_id})！\n{total_moves_text}\n目前輪到：{turn_text}"
        else:
            message_text = f"📂 已讀取棋譜！\n{total_moves_text}\n目前輪到：{turn_text}"

        if is_valid_https_url(image_url):
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[
                    TextMessage(text=message_text),
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
                messages=[TextMessage(text=f"{message_text}\n\n⚠️ 圖片 URL 無效")],
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
    1. Loads the SGF file for the specified game_id from GCS
    2. Extracts only the first N moves
    3. Creates a new game_id
    4. Saves the truncated SGF file to GCS
    5. Updates state to the new game_id
    """
    try:
        # Load SGF from GCS using the source game_id
        from services.storage import download_file, file_exists, upload_buffer, get_public_url

        source_sgf_remote_path = f"target_{target_id}/boards/{source_game_id}/game.sgf"
        if not await file_exists(source_sgf_remote_path):
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=f"找不到 game_id 為 {source_game_id} 的棋譜。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        # Download source SGF
        sgf_bytes = await download_file(source_sgf_remote_path)
        source_sgf_game = sgf.Sgf_game.from_bytes(sgf_bytes)

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

        # Save truncated SGF to GCS
        new_sgf_remote_path = f"target_{target_id}/boards/{new_game_id}/game.sgf"
        truncated_sgf_bytes = truncated_sgf.serialise()

        await upload_buffer(
            truncated_sgf_bytes,
            new_sgf_remote_path,
            content_type="application/x-go-sgf",
            cache_control="no-cache, max-age=0",
        )

        logger.info(f"Created truncated SGF with {move_count} moves: {new_sgf_remote_path}")

        # Restore game state from truncated SGF
        restored = restore_game_from_sgf_object(truncated_sgf)
        if not restored:
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="讀取失敗：無法解析棋譜檔案。")],
            )
            await asyncio.to_thread(line_bot_api.reply_message, request)
            return

        state = restored
        game = state["game"]
        current_turn = state["current_turn"]

        # Always update state.json with restored state from truncated SGF
        # Preserve vs_ai_mode from existing state if it exists
        existing_state = await load_state_from_gcs(target_id)
        vs_ai_mode = existing_state.get("vs_ai_mode", False) if existing_state else False

        await save_state_to_gcs(
            target_id,
            {
                "game_id": new_game_id,
                "current_turn": current_turn,
                "vs_ai_mode": vs_ai_mode,  # Preserve vs_ai_mode state
            },
        )
        logger.info(
            f"Updated state.json for {target_id} with truncated game: game_id={new_game_id}, current_turn={current_turn}, moves={move_count}"
        )

        # Find last move coordinates for highlighting and build move_numbers dict
        last_coords = None
        move_numbers = {}  # {(row, col): move_number}
        sequence = truncated_sgf.get_main_sequence()
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
        import tempfile
        from services.storage import upload_file

        timestamp = int(time.time())
        filename = f"board_restored_{timestamp}.png"

        # Draw board to temporary file with move numbers
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
            tmp_path = tmp_file.name

        visualizer.draw_board(
            game.board, last_move=last_coords, output_filename=tmp_path, move_numbers=move_numbers
        )

        # Upload to GCS
        remote_path = f"target_{target_id}/boards/{new_game_id}/{filename}"
        await upload_file(tmp_path, remote_path)

        # Get public URL
        image_url = get_public_url(remote_path)

        # Clean up temporary file
        try:
            os.unlink(tmp_path)
        except:
            pass

        # Send board image
        turn_text = "黑" if current_turn == 1 else "白"
        total_moves_text = f"總手數：{move_count} 手"

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
            logger.warning(f"Invalid image URL: {image_url}")
            request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[
                    TextMessage(
                        text=f"📂 已讀取棋譜 (game_id: {source_game_id}) 前 {move_count} 手！\n新對局 game_id: {new_game_id}\n{total_moves_text}\n目前輪到：{turn_text}\n\n⚠️ 圖片 URL 無效"
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
