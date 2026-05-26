import re
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List
from linebot.v3.messaging import (
    ReplyMessageRequest,
)
from linebot.v3.messaging.models import (
    TextMessage,
)

from config import config
from logger import logger
from handlers.game_state import (
    game_states, game_ids,
    get_game_id, get_game_state, restore_game_from_sgf_file, create_sgf_with_first_n_moves,
    restore_game_from_sgf, save_game_sgf,
)
from handlers.line_sender import (
    line_bot_api, blob_api,
    send_message,
)
from handlers.game_commands import (
    handle_board_move, handle_ai_next_move, handle_undo_move,
    handle_load_game, handle_load_game_by_id, handle_load_game_by_id_with_moves,
)
from handlers.review_commands import handle_review_command
from handlers.evaluation_commands import handle_evaluation_command
from handlers.ai_mode_commands import (
    handle_ai_mode_status, handle_enable_ai_mode, handle_disable_ai_mode,
)
from handlers.game_state_commands import (
    handle_resign_command, handle_reset_command,
)


bot_user_id: Optional[str] = None
bot_display_name: Optional[str] = None


# Get Bot's own User ID
async def init_bot_user_id():
    global bot_user_id, bot_display_name
    try:
        # Run synchronous call in thread pool
        # get_bot_info doesn't require a request object in v3 API
        bot_info = await asyncio.to_thread(line_bot_api.get_bot_info)
        bot_user_id = bot_info.user_id
        bot_display_name = bot_info.display_name
        logger.info(f"Bot User ID: {bot_user_id}, Display Name: {bot_display_name}")
    except Exception as error:
        logger.error(f"Failed to get bot info: {error}", exc_info=True)


async def get_bot_display_name() -> Optional[str]:
    """Get bot display name (cached, initialized by init_bot_user_id)"""
    global bot_display_name
    if bot_display_name is None:
        # If not initialized, try to get it
        try:
            bot_info = await asyncio.to_thread(line_bot_api.get_bot_info)
            bot_display_name = bot_info.display_name
            logger.debug(f"Bot Display Name: {bot_display_name}")
        except Exception as error:
            logger.error(f"Failed to get bot info: {error}", exc_info=True)
            return None
    return bot_display_name


def is_valid_https_url(url: str) -> bool:
    """Validate if URL is a valid HTTPS URL"""
    if not url or not isinstance(url, str):
        return False

    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.scheme == "https"
    except Exception:
        return False


def encode_url_path(path: str) -> str:
    """Encode URL path (preserve slashes, encode other special characters)"""
    from urllib.parse import quote

    return "/".join(quote(part, safe="") for part in path.split("/"))


def create_video_preview_bubble(
    move_number: int,
    color: str,
    played: str,
    comment: str,
    preview_image_url: str,
    video_url: str,
    winrate_before: Optional[float] = None,
    winrate_after: Optional[float] = None,
    score_loss: Optional[float] = None,
) -> Dict[str, Any]:
    """Create single Bubble content (for Carousel)"""
    color_text = "黑" if color == "B" else "白"

    # Limit comment length (LINE Flex Message has character limit)
    max_comment_length = 500
    truncated_comment = (
        comment[:max_comment_length] + "..."
        if len(comment) > max_comment_length
        else comment
    )

    # Build body contents
    body_contents = [
        {
            "type": "text",
            "text": f"📍 第 {move_number} 手（{color_text}）",
            "weight": "bold",
            "size": "lg",
            "color": "#1DB446",
        },
        {
            "type": "text",
            "text": f"落子位置：{played}",
            "size": "sm",
            "color": "#666666",
            "margin": "md",
        },
    ]

    # Add winrate change if available
    if winrate_before is not None and winrate_after is not None:
        winrate_diff = winrate_before - winrate_after
        winrate_text = f"勝率變化：{winrate_before:.1f}% → {winrate_after:.1f}%"
        if winrate_diff > 0:
            winrate_text += f" (↓{winrate_diff:.1f}%)"
        else:
            winrate_text += f" (↑{abs(winrate_diff):.1f}%)"

        body_contents.append(
            {
                "type": "text",
                "text": winrate_text,
                "size": "sm",
                "color": "#FF6B6B" if winrate_diff > 0 else "#4ECDC4",
                "margin": "sm",
            }
        )

    # Add score loss if available
    if score_loss is not None:
        body_contents.append(
            {
                "type": "text",
                "text": f"目差損失：{score_loss:.1f} 目",
                "size": "sm",
                "color": "#FF6B6B",
                "margin": "sm",
            }
        )

    body_contents.append({"type": "separator", "margin": "md"})
    body_contents.append(
        {
            "type": "text",
            "text": truncated_comment,
            "wrap": True,
            "size": "sm",
            "margin": "md",
            "color": "#333333",
        }
    )

    return {
        "type": "bubble",
        "hero": {
            "type": "image",
            "url": preview_image_url,
            "size": "full",
            "aspectRatio": "1:1",
            "aspectMode": "cover",
            "action": {"type": "uri", "uri": video_url, "label": "觀看動畫"},
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": body_contents,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "action": {
                        "type": "uri",
                        "label": "🎬 觀看動態棋譜",
                        "uri": video_url,
                    },
                    "color": "#1DB446",
                }
            ],
        },
    }


def create_carousel_flex_message(
    bubbles: List[Dict[str, Any]], start_index: int = 1, total_count: int = None
) -> Dict[str, Any]:
    """Create Carousel Flex Message (combine multiple bubbles)"""
    if total_count is None:
        total_count = len(bubbles)

    return {
        "type": "flex",
        "altText": f"關鍵手數分析（{start_index}-{start_index + len(bubbles) - 1}/{total_count}）",
        "contents": {"type": "carousel", "contents": bubbles},
    }


HELP_MESSAGE = """歡迎使用圍棋 Line Bot！

📋 指令列表：
• help / 幫助 / 說明 - 顯示此說明

🎮 對局功能：
• 輸入座標（如 D4, Q16）- 落子並顯示棋盤
• 悔棋 / undo - 撤銷上一步
• 悔棋 10 / undo 10 - 撤銷指定手數
• 讀取 / load - 從存檔恢復當前遊戲
• 讀取 game_1234567890 / load game_1234567890 - 讀取指定 game_id 的棋譜
• 讀取 game_1234567890 10 / load game_1234567890 10 - 讀取指定 game_id 的前 N 手，並創建新對局
• 重置 / reset - 重置棋盤，開始新遊戲（會保存當前棋譜）
• 投子 - 認輸並結束本局（會先顯示勝負，再重置棋盤）
• 形勢 / 形式 / evaluation - 顯示當前盤面領地分布與目數差距

🤖 AI 對弈功能：
• 對弈 / vs - 查看目前對弈模式狀態
• 對弈 ai / vs ai - 開啟 AI 對弈模式（與 AI 對戰）
• 對弈 free / vs free - 關閉 AI 對弈模式（恢復一般對弈模式）

📊 覆盤分析功能：
• 覆盤 / review - 對最新上傳的棋譜執行 KataGo 覆盤分析
• review setting - 顯示目前關鍵手數挑選依據
• review setting winrate - 以勝率落差挑選前 20 手
• review setting score_loss - 以目差損失挑選前 20 手

覆盤使用流程：
1️⃣ 上傳 SGF 棋譜檔案
2️⃣ 輸入「覆盤」開始分析
3️⃣ 等待約 10 分鐘獲得分析結果

覆盤分析結果包含：
• 🗺️ 全盤手順圖 - 顯示整局棋的所有手順
• 📈 勝率變化圖 - 顯示黑方勝率隨手數的變化曲線
• 🎬 關鍵手數 GIF 動畫 - 依設定（勝率落差或目差損失）挑選前 20 手動態演示
• 💬 ChatGPT 評論 - 針對關鍵手數的評論

技術規格：
• 分析引擎：KataGo AI（visits=1000)
• 分析時間：KataGo 全盤分析約 6 分鐘
• 評論生成：ChatGPT 評論生成約 3 分鐘
• 動畫繪製：GIF 動畫繪製約 10 秒

注意事項：
• 覆盤功能每次消耗 4 個推播訊息 × 群組人數，每月訊息上限為 200 則，請注意使用頻率，超出上限將無法使用覆盤功能"""


async def save_sgf_file(file_buffer: bytes, original_file_name: str) -> Dict[str, str]:
    """Save SGF file to static folder"""
    current_file = Path(__file__)
    project_root = current_file.parent.parent.parent
    static_dir = project_root / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    file_path = static_dir / original_file_name

    # Write file
    with open(file_path, "wb") as f:
        f.write(file_buffer)

    return {"fileName": original_file_name, "filePath": str(file_path)}



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

            # Initialize bot_user_id if not set
            if bot_user_id is None:
                logger.warning("bot_user_id is None, initializing...")
                await init_bot_user_id()

            # Check if bot is mentioned using userId or isSelf field
            is_bot_mentioned = False
            for mentionee in mentions:
                # Check by userId match
                if bot_user_id and mentionee.get("userId") == bot_user_id:
                    is_bot_mentioned = True
                    break
                # Also check isSelf field as fallback (when bot mentions itself)
                if mentionee.get("isSelf", False):
                    is_bot_mentioned = True
                    logger.info(f"Bot mentioned via isSelf field: {mentionee}")
                    break

            if not is_bot_mentioned:
                # Mention is not bot, ignore this message
                logger.warning(
                    f"Mention check failed: bot_user_id={bot_user_id}, "
                    f"mention_userIds={[m.get('userId') for m in mentions]}, "
                    f"isSelf_flags={[m.get('isSelf', False) for m in mentions]}"
                )
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

    if text in ["help", "幫助", "說明"]:
        request = ReplyMessageRequest(
            reply_token=reply_token, messages=[TextMessage(text=HELP_MESSAGE)]
        )
        await asyncio.to_thread(line_bot_api.reply_message, request)
        return

    review_setting_match = re.match(
        r"^(?:review\s+setting|覆盤設定)(?:\s+([a-zA-Z_]+))?$", text, re.IGNORECASE
    )
    if review_setting_match:
        target_id = (
            source.get("groupId") or source.get("roomId") or source.get("userId")
        )
        metric = review_setting_match.group(1)
        await handle_review_setting_command(target_id, reply_token, metric)
        return

    if text == "覆盤" or text.lower() == "review":
        # Get push target ID
        target_id = (
            source.get("groupId") or source.get("roomId") or source.get("userId")
        )
        # Pass replyToken for initial reply (reduce usage)
        await handle_review_command(target_id, reply_token)
        return

    if text == "形勢" or text == "形式" or text.lower() == "evaluation":
        target_id = (
            source.get("groupId") or source.get("roomId") or source.get("userId")
        )
        await handle_evaluation_command(target_id, reply_token)
        return

    # Handle One Line Kill Mode
    if text == "一線擺滿殺棋模式":
        target_id = (
            source.get("groupId") or source.get("roomId") or source.get("userId")
        )
        await handle_one_line_kill_mode(target_id, reply_token)
        return

    # Handle Guess First
    if text.startswith("猜先 "):
        parts = text.split()
        if len(parts) >= 3:
            # Join parts to handle names with potential issues, though simple split is requested
            # User request: "猜先 對局者一 對局者二"
            # We take index 1 and 2. 
            # If names have spaces, this simple split might be wrong, but "猜先" usually implies simple names.
            # Let's assume standard usage "猜先 Name1 Name2"
            player1 = parts[1]
            player2 = parts[2]
            target_id = source.get("groupId") or source.get("roomId") or source.get("userId")
            await handle_guess_first_command(target_id, reply_token, player1, player2)
            return

    # Get target ID for game state management
    target_id = source.get("groupId") or source.get("roomId") or source.get("userId")

    # Check if input is a board coordinate (A-T, 1-19)
    # Pattern matches coordinates like "D4", "Q16", etc. (skips 'I')
    coord_pattern = r"^[A-HJ-T]([1-9]|1[0-9])$"
    user_text_upper = text.upper().strip()

    if re.match(coord_pattern, user_text_upper):
        # Handle board coordinate input
        await handle_board_move(target_id, reply_token, user_text_upper, source)
        return

    # Handle "對弈" to show current mode status
    if text.lower() in ["對弈", "vs"]:
        await handle_ai_mode_status(target_id, reply_token)
        return

    # Handle "對弈 ai" to enable VS AI mode
    if text.lower() in ["對弈 ai", "對弈ai", "vs ai", "vsai"]:
        await handle_enable_ai_mode(target_id, reply_token)
        return

    # Handle "對弈 free" to disable VS AI mode
    if text.lower() in ["對弈 free", "對弈free", "vs free", "vsfree"]:
        await handle_disable_ai_mode(target_id, reply_token)
        return

    if "投子" in text:
        await handle_resign_command(target_id, reply_token)
        return

    if "重置" in text or "reset" in text.lower():
        await handle_reset_command(target_id, reply_token)
        return

    undo_match = re.match(r"^(?:悔棋|undo)(?:\s+(\d+))?$", text, re.IGNORECASE)
    if undo_match:
        undo_steps = int(undo_match.group(1)) if undo_match.group(1) else 1
        await handle_undo_move(target_id, reply_token, undo_steps=undo_steps)
        return

    if "讀取" in text or "load" in text.lower():
        # Match "讀取 game_1234567890 10" or "讀取 game_1234567890 10" or "load game_1234567890 10"
        # Pattern: (讀取|load) game_\d+ \d+
        read_with_moves_match = re.match(r"(?:讀取|load)\s+(game_\d+)\s+(\d+)", text, re.IGNORECASE)
        if read_with_moves_match:
            game_id = read_with_moves_match.group(1).strip()
            move_count_str = read_with_moves_match.group(2).strip()
            try:
                move_count = int(move_count_str)
                if move_count > 0:
                    await handle_load_game_by_id_with_moves(target_id, reply_token, game_id, move_count)
                    return
            except ValueError:
                pass  # Invalid move count, fall through to regular load
        
        # Match "讀取 game_1234567890" or "讀取game_1234567890" or "load game_1234567890"
        read_match = re.match(r"(?:讀取|load)\s*(game_\d+)", text, re.IGNORECASE)
        if read_match:
            game_id = read_match.group(1).strip()
            if game_id:
                await handle_load_game_by_id(target_id, reply_token, game_id)
                return
        
        # Load current game (no game_id specified)
        await handle_load_game(target_id, reply_token)
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

        # Save file to static folder
        saved_file = await save_sgf_file(file_buffer, file_name)
        import handlers.game_state as _game_state
        _game_state.current_sgf_files[target_id] = saved_file["fileName"]

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
