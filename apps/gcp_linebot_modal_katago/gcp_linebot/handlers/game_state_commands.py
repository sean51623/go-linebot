import asyncio
from typing import Optional

from linebot.v3.messaging import ReplyMessageRequest
from linebot.v3.messaging.models import TextMessage

from logger import logger
from handlers.game_state import reset_game_state, load_state_from_gcs
from handlers.line_sender import line_bot_api, is_valid_https_url, create_sgf_file_flex_message


async def handle_resign(target_id: str, reply_token: Optional[str]):
    """Handle resign command (投子)"""
    current_game_id = None
    current_sgf_url = None
    current_turn = 1

    try:
        state_meta = await load_state_from_gcs(target_id)
        if state_meta:
            current_turn = state_meta.get("current_turn", 1)
            if "game_id" in state_meta:
                current_game_id = state_meta["game_id"]
                from services.storage import file_exists, get_public_url

                sgf_remote_path = (
                    f"target_{target_id}/boards/{current_game_id}/game.sgf"
                )
                if await file_exists(sgf_remote_path):
                    current_sgf_url = get_public_url(sgf_remote_path)
    except Exception as error:
        logger.warning(f"Failed to get current SGF before 投子: {error}")

    resign_side = "黑" if current_turn == 1 else "白"
    winner_side = "白" if current_turn == 1 else "黑"
    resign_msg = f"{resign_side}方投子，{winner_side}方獲勝！"

    await reset_game_state(target_id, reply_token)

    messages = [TextMessage(text=resign_msg)]
    if current_sgf_url and is_valid_https_url(current_sgf_url) and current_game_id:
        sgf_flex_message = create_sgf_file_flex_message(
            current_sgf_url, current_game_id
        )
        messages.append(sgf_flex_message)
    messages.append(TextMessage(text="✅ 棋盤已重置，黑棋請下。"))

    request = ReplyMessageRequest(
        reply_token=reply_token,
        messages=messages,
    )
    await asyncio.to_thread(line_bot_api.reply_message, request)


async def handle_reset(target_id: str, reply_token: Optional[str]):
    """Handle reset command (重置/reset)"""
    # Get current game ID and SGF file before reset
    current_game_id = None
    current_sgf_url = None

    try:
        state_meta = await load_state_from_gcs(target_id)
        if state_meta and "game_id" in state_meta:
            current_game_id = state_meta["game_id"]
            from services.storage import file_exists, get_public_url

            sgf_remote_path = (
                f"target_{target_id}/boards/{current_game_id}/game.sgf"
            )
            if await file_exists(sgf_remote_path):
                current_sgf_url = get_public_url(sgf_remote_path)
    except Exception as error:
        logger.warning(f"Failed to get current SGF before reset: {error}")

    # Reset game state (preserving vs_ai_mode)
    await reset_game_state(target_id, reply_token)

    messages = []
    if current_sgf_url and is_valid_https_url(current_sgf_url) and current_game_id:
        # Send SGF file using Flex Message with download button
        sgf_flex_message = create_sgf_file_flex_message(
            current_sgf_url, current_game_id
        )
        messages.append(sgf_flex_message)

    messages.append(TextMessage(text="✅ 棋盤已重置，黑棋請下。"))

    request = ReplyMessageRequest(
        reply_token=reply_token,
        messages=messages,
    )
    await asyncio.to_thread(line_bot_api.reply_message, request)
