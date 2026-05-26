import asyncio
from typing import Optional

from linebot.v3.messaging import ReplyMessageRequest
from linebot.v3.messaging.models import TextMessage

from logger import logger
from handlers.game_state import (
    is_vs_ai_mode, enable_vs_ai_mode, disable_vs_ai_mode,
    load_state_from_gcs,
)
from handlers.line_sender import line_bot_api


async def handle_vs_ai_status(target_id: str, reply_token: Optional[str]):
    """Handle 'vs' / '對弈' command - show current VS AI mode status"""
    vs_ai_mode = await is_vs_ai_mode(target_id)
    state_meta = await load_state_from_gcs(target_id)
    current_turn = state_meta.get("current_turn", 1) if state_meta else 1

    if vs_ai_mode:
        mode_text = "AI 對弈模式"
        ai_color = "黑" if current_turn == 1 else "白"
        user_color = "白" if current_turn == 1 else "黑"
        status_message = f"""📊 目前模式：{mode_text}

您執{user_color}，AI 執{ai_color}。

🤖 AI 對弈模式：
• 您下完一手後，AI 會自動思考並下下一手
• 適合與 AI 對戰練習

🆓 一般對弈模式：
• 一人一手棋，輪流下棋
• 適合與朋友對戰或自己練習

💡 切換模式：
• 輸入「對弈 ai」開啟 AI 對弈模式
• 輸入「對弈 free」切換為一般對弈模式"""
    else:
        mode_text = "一般對弈模式"
        status_message = f"""📊 目前模式：{mode_text}

🆓 一般對弈模式：
• 一人一手棋，輪流下棋
• 適合與朋友對戰或自己練習

🤖 AI 對弈模式：
• 您下完一手後，AI 會自動思考並下下一手
• 適合與 AI 對戰練習

💡 切換模式：
• 輸入「對弈 ai」開啟 AI 對弈模式
• 輸入「對弈 free」切換為一般對弈模式"""

    request = ReplyMessageRequest(
        reply_token=reply_token,
        messages=[TextMessage(text=status_message)],
    )
    await asyncio.to_thread(line_bot_api.reply_message, request)


async def handle_enable_vs_ai(target_id: str, reply_token: Optional[str]):
    """Handle 'vs ai' / '對弈 ai' command - enable VS AI mode"""
    success = await enable_vs_ai_mode(target_id)
    if success:
        # Get current turn to determine AI color
        state_meta = await load_state_from_gcs(target_id)
        current_turn = state_meta.get("current_turn", 1) if state_meta else 1
        user_color = "黑" if current_turn == 1 else "白"
        ai_color = "白" if current_turn == 1 else "黑"

        request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[
                TextMessage(
                    text=f"✅ 已開啟 AI 對弈模式！\n\n您執{user_color}，AI 執{ai_color}。\n請開始下棋（例如：D4）。"
                )
            ],
        )
    else:
        request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text="❌ 開啟對弈模式失敗，請稍後再試。")],
        )
    await asyncio.to_thread(line_bot_api.reply_message, request)


async def handle_disable_vs_ai(target_id: str, reply_token: Optional[str]):
    """Handle 'vs free' / '對弈 free' command - disable VS AI mode"""
    success = await disable_vs_ai_mode(target_id)
    if success:
        request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[
                TextMessage(
                    text="✅ 已關閉 AI 對弈模式！\n\n現在恢復為一般對弈模式（一人一手棋）。"
                )
            ],
        )
    else:
        request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text="❌ 關閉對弈模式失敗，請稍後再試。")],
        )
    await asyncio.to_thread(line_bot_api.reply_message, request)
