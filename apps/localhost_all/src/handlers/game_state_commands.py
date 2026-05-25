"""Game state command handlers (resign, reset)"""
import asyncio
from typing import Any, Dict
from linebot.v3.messaging import ReplyMessageRequest
from linebot.v3.messaging.models import TextMessage

from handlers.line_sender import line_bot_api
from handlers.game_state import get_game_state, reset_game_state


async def handle_resign_command(target_id: str, reply_token: str):
    """Handle 投子 command to resign"""
    state = get_game_state(target_id)
    current_turn = state.get("current_turn", 1)
    resign_side = "黑" if current_turn == 1 else "白"
    winner_side = "白" if current_turn == 1 else "黑"
    resign_msg = f"{resign_side}方投子，{winner_side}方獲勝！"
    reset_game_state(target_id)
    request = ReplyMessageRequest(
        reply_token=reply_token,
        messages=[
            TextMessage(text=resign_msg),
            TextMessage(text="棋盤已重置，黑棋請下。"),
        ],
    )
    await asyncio.to_thread(line_bot_api.reply_message, request)


async def handle_reset_command(target_id: str, reply_token: str):
    """Handle 重置/reset command"""
    reset_game_state(target_id)
    request = ReplyMessageRequest(
        reply_token=reply_token,
        messages=[TextMessage(text="棋盤已重置，黑棋請下。")],
    )
    await asyncio.to_thread(line_bot_api.reply_message, request)
