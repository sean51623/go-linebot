import asyncio
import re
import time
from typing import Optional, Dict, Any, List

from linebot.v3.messaging import ReplyMessageRequest
from linebot.v3.messaging.models import TextMessage

from config import config
from logger import logger

from handlers.line_sender import line_bot_api, blob_api, send_message, is_valid_https_url
from handlers.game_state import load_state_from_gcs
from handlers.game_commands import handle_board_move, handle_undo_move, handle_load_game_by_id
from handlers.review_commands import handle_review_command
from handlers.evaluation_commands import handle_evaluation_command
from handlers.ai_mode_commands import handle_vs_ai_status, handle_enable_vs_ai, handle_disable_vs_ai
from handlers.game_state_commands import handle_resign, handle_reset


# Bot info cache
_bot_display_name: Optional[str] = None


# Get Bot's own User ID
async def get_bot_user_id() -> Optional[str]:
    """Get bot user ID directly from LINE API"""
    try:
        bot_info = await asyncio.to_thread(line_bot_api.get_bot_info)
        bot_user_id = bot_info.user_id
        logger.debug(f"Bot User ID: {bot_user_id}")
        return bot_user_id
    except Exception as error:
        logger.error(f"Failed to get bot info: {error}", exc_info=True)
        return None


async def get_bot_display_name() -> Optional[str]:
    """Get bot display name directly from LINE API (cached)"""
    global _bot_display_name
    if _bot_display_name is not None:
        return _bot_display_name

    try:
        bot_info = await asyncio.to_thread(line_bot_api.get_bot_info)
        _bot_display_name = bot_info.display_name
        logger.debug(f"Bot Display Name: {_bot_display_name}")
        return _bot_display_name
    except Exception as error:
        logger.error(f"Failed to get bot info: {error}", exc_info=True)
        return None


HELP_MESSAGE = """歡迎使用圍棋 Line Bot！

📋 指令列表：
• help / 幫助 / 說明 - 顯示此說明

🎮 對局功能：
• 輸入座標（如 D4, Q16）- 落子並顯示棋盤
• 悔棋 / undo - 撤銷上一步
• 讀取 / load - 從存檔恢復當前遊戲
• 讀取 game_1234567890 / load game_1234567890 - 讀取指定 game_id 的棋譜
• 重置 / reset - 重置棋盤，開始新遊戲（會保存當前棋譜）
• 投子 - 認輸並結束本局（會先顯示勝負，再重置棋盤）
• 形勢 / 形式 / evaluation - 顯示當前盤面領地分布與目數差距

🤖 AI 對弈功能：
• 對弈 / vs - 查看目前對弈模式狀態
• 對弈 ai / vs ai - 開啟 AI 對弈模式（與 AI 對戰）
• 對弈 free / vs free - 關閉 AI 對弈模式（恢復一般對弈模式）

📊 覆盤分析功能：
• 覆盤 / review - 對最新上傳的棋譜執行 KataGo 覆盤分析

覆盤使用流程：
1️⃣ 上傳 SGF 棋譜檔案
2️⃣ 輸入「覆盤」開始分析
3️⃣ 等待約 5 分鐘獲得分析結果

覆盤分析結果包含：
• 🗺️ 全盤手順圖 - 顯示整局棋的所有手順
• 📈 勝率變化圖 - 顯示黑方勝率隨手數的變化曲線
• 🎬 關鍵手數 GIF 動畫 - 勝率差距最大的前 20 手動態演示
• 💬 ChatGPT 評論 - 針對關鍵手數的評論

技術規格：
• 分析引擎：KataGo AI（visits=5）
• 分析時間：KataGo 全盤分析約 1 分鐘
• 評論生成：ChatGPT 評論生成約 3 分鐘
• 動畫繪製：GIF 動畫繪製約 10 秒

注意事項：
• 覆盤功能每次消耗 4 個推播訊息 × 群組人數，每月訊息上限為 200 則，請注意使用頻率，超出上限將無法使用覆盤功能"""


async def save_sgf_file(
    file_buffer: bytes, original_file_name: str, target_id: str = None
) -> Dict[str, str]:
    """Save SGF file to GCS
    If target_id is provided, save to target_{target_id}/reviews/ folder
    Otherwise, save to sgf/ folder (for backward compatibility)
    """
    from services.storage import upload_buffer
    import time

    # Generate unique path for SGF file
    timestamp = int(time.time())
    if target_id:
        # Save to reviews folder for review processing
        remote_path = f"target_{target_id}/reviews/{original_file_name}_{timestamp}.sgf"

    # Upload to GCS
    gcs_path = await upload_buffer(file_buffer, remote_path)

    return {
        "fileName": original_file_name,
        "filePath": gcs_path,
        "remotePath": remote_path,
    }


async def handle_text_message(event: Dict[str, Any]):
    """Handle text message"""
    reply_token = event.get("replyToken")
    message = event.get("message", {})
    source = event.get("source", {})
    text = message.get("text", "").strip()

    # In group/room, only process mention messages
    if source.get("type") in ["group", "room"]:
        # First, check if text starts with "@{bot_display_name}" (text mention for desktop LINE)
        bot_display_name = await get_bot_display_name()
        text_mention_matched = False
        if bot_display_name:
            # Escape special regex characters in bot display name
            escaped_display_name = re.escape(bot_display_name)
            text_mention_pattern = rf"^@{escaped_display_name}\s+(.+)$"
            text_mention_match = re.match(text_mention_pattern, text, re.IGNORECASE)

            if text_mention_match:
                # Extract command after @{bot_display_name}
                text = text_mention_match.group(1).strip()
                text_mention_matched = True
        else:
            logger.error("Failed to get bot display_name, skipping text mention check")

        # Fallback to mention API (for mobile LINE) if text mention didn't match
        if not text_mention_matched:
            mention = message.get("mention")
            if (
                not mention
                or not mention.get("mentionees")
                or len(mention["mentionees"]) == 0
            ):
                # No mention and no text mention, ignore this message
                return

            # Check if mention includes bot itself
            mentions = mention["mentionees"]
            bot_user_id = await get_bot_user_id()
            is_bot_mentioned = (
                any(mentionee.get("userId") == bot_user_id for mentionee in mentions)
                if bot_user_id
                else False
            )

            if not is_bot_mentioned:
                # Mention is not bot, ignore this message
                return

            # Remove mention markers to get actual command
            clean_text = text
            # Sort mentions by index descending to avoid index position changes
            for mention_obj in sorted(
                mentions, key=lambda x: x.get("index", 0), reverse=True
            ):
                index = mention_obj.get("index", 0)
                length = mention_obj.get("length", 0)
                clean_text = clean_text[:index] + clean_text[index + length :]

            text = clean_text.strip()

    # Get target ID for game state management
    target_id = source.get("groupId") or source.get("roomId") or source.get("userId")

    if text in ["help", "幫助", "說明"]:
        request = ReplyMessageRequest(
            reply_token=reply_token, messages=[TextMessage(text=HELP_MESSAGE)]
        )
        await asyncio.to_thread(line_bot_api.reply_message, request)
        return

    if text == "覆盤" or text.lower() == "review":
        await handle_review_command(target_id, reply_token)
        return

    if text == "形勢" or text == "形式" or text.lower() == "evaluation":
        await handle_evaluation_command(target_id, reply_token)
        return

    if "悔棋" in text or "undo" in text.lower():
        await handle_undo_move(target_id, reply_token)
        return

    if "讀取" in text or "load" in text.lower():
        # Match "讀取 game_1234567890" or "讀取game_1234567890" or "load game_1234567890" or "loadgame_1234567890"
        # Ensure we match the full game_id format: game_ followed by digits
        read_match = re.match(r"(?:讀取|load)\s*(game_\d+)", text, re.IGNORECASE)
        if read_match:
            game_id = read_match.group(1).strip()
            if game_id:  # Make sure game_id is not empty
                # Load specific game by game_id
                await handle_load_game_by_id(target_id, reply_token, game_id)
                return

        # Load current game (no game_id specified)
        await handle_load_game_by_id(target_id, reply_token, None)
        return

    # Handle "對弈" to show current mode status
    if text.lower() in ["對弈", "vs"]:
        await handle_vs_ai_status(target_id, reply_token)
        return

    # Handle "對弈 ai" to enable VS AI mode
    if text.lower() in ["對弈 ai", "對弈ai", "vs ai", "vsai"]:
        await handle_enable_vs_ai(target_id, reply_token)
        return

    # Handle "對弈 free" to disable VS AI mode
    if text.lower() in ["對弈 free", "對弈free", "vs free", "vsfree"]:
        await handle_disable_vs_ai(target_id, reply_token)
        return

    if "投子" in text:
        await handle_resign(target_id, reply_token)
        return

    if "重置" in text or "reset" in text.lower():
        await handle_reset(target_id, reply_token)
        return

    # Check if input is a board coordinate (A-T, 1-19)
    # Pattern matches coordinates like "D4", "Q16", etc. (skips 'I')
    coord_pattern = r"^[A-HJ-T]([1-9]|1[0-9])$"
    user_text_upper = text.upper().strip()

    if re.match(coord_pattern, user_text_upper):
        # Handle board coordinate input
        await handle_board_move(target_id, reply_token, user_text_upper, source)
        return


async def handle_file_message(event: Dict[str, Any]):
    """Handle file message"""
    reply_token = event.get("replyToken")
    message = event.get("message", {})
    source = event.get("source", {})

    # Get push target ID (based on source type)
    target_id = source.get("groupId") or source.get("roomId") or source.get("userId")
    # Get user ID (for task tracking)
    user_id = source.get("userId") or target_id

    try:
        # Get file content
        content_id = message.get("id")
        # Run synchronous call in thread pool
        file_content = await asyncio.to_thread(blob_api.get_message_content, content_id)

        # Convert payload to bytes
        if isinstance(file_content, bytes):
            file_buffer = file_content
        elif hasattr(file_content, "data"):
            file_buffer = file_content.data
        elif hasattr(file_content, "body"):
            file_buffer = file_content.body
        elif hasattr(file_content, "read"):
            file_buffer = file_content.read()
        elif hasattr(file_content, "iter_content"):
            file_buffer = b"".join(chunk for chunk in file_content.iter_content())
        else:
            raise ValueError("Unsupported LINE blob response format")

        # Check file type
        file_name = message.get("fileName", "game.sgf")
        if not file_name.lower().endswith(".sgf"):
            return

        # Remove .sgf extension (case-insensitive) before passing to save_sgf_file
        file_name_lower = file_name.lower()
        if file_name_lower.endswith(".sgf"):
            # Remove the extension, preserving original case for the base name
            ext_length = len(".sgf")
            file_name_without_ext = file_name[:-ext_length]
        else:
            file_name_without_ext = file_name

        # Save file to GCS in reviews folder
        saved_file = await save_sgf_file(file_buffer, file_name_without_ext, target_id)

        # Notify user file is saved (use replyMessage to reduce usage)
        request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[
                TextMessage(
                    text=f"""✅ 棋譜已保存！

📁 檔案: {file_name}

棋譜已保存到伺服器，後續可執行 "覆盤" 或 "review" 指令進行分析..."""
                )
            ],
        )
        await asyncio.to_thread(line_bot_api.reply_message, request)
    except Exception as error:
        logger.error(f"Error handling file message: {error}", exc_info=True)
        request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=f"❌ 儲存棋譜時發生錯誤：{str(error)}")],
        )
        await asyncio.to_thread(line_bot_api.reply_message, request)
