import asyncio
import time
import tempfile
from pathlib import Path
from typing import Optional

from linebot.v3.messaging.models import TextMessage, ImageMessage

from config import config
from logger import logger
from handlers.game_state import get_game_state, save_game_sgf, get_game_id
from handlers.line_sender import send_message
from handlers.game_commands import visualizer


async def handle_evaluation_command(target_id: str, reply_token: Optional[str]):
    """Handle shape evaluation command (形勢判斷 / evaluation)"""
    import httpx

    try:
        state = await get_game_state(target_id)
        game = state["game"]
        current_turn = state.get("current_turn", 1)
        sgf_game = state["sgf_game"]

        # 檢查是否有任何落子
        has_stone = any(
            stone != 0 for row in game.board for stone in row
        )
        if not has_stone:
            await send_message(
                target_id,
                reply_token,
                [TextMessage(text="目前盤面沒有進行中的對局，無法進行形勢判斷。")],
            )
            return

        # 確保 SGF 已保存
        sgf_gcs_path = await save_game_sgf(target_id, state)
        if not sgf_gcs_path:
            await send_message(
                target_id,
                reply_token,
                [TextMessage(text="❌ 無法儲存目前棋局 SGF，請稍後再試。")],
            )
            return

        # Get localhost URL from config
        localhost_url = config.get("localhost_katago", {}).get("url")
        if not localhost_url:
            logger.error("LOCALHOST_KATAGO_URL not configured")
            await send_message(
                target_id,
                reply_token,
                [TextMessage(text="❌ 系統配置錯誤：未設定本地 KataGo 服務 URL")],
            )
            return

        # Ensure it ends with /evaluation endpoint
        if not localhost_url.endswith("/evaluation"):
            localhost_url = f"{localhost_url.rstrip('/')}/evaluation"

        # POST evaluation request to localhost service
        logger.info(f"Posting evaluation request to localhost: {localhost_url}")
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                localhost_url,
                json={
                    "sgf_gcs_path": sgf_gcs_path,
                    "current_turn": current_turn,
                    "visits": 1000,
                },
            )
            response.raise_for_status()
            result = response.json()
            logger.info(f"Successfully received evaluation result")

        if not result.get("success"):
            error = result.get("error", "Unknown error")
            logger.error(f"KataGo evaluation failed: {error}")
            await send_message(
                target_id,
                reply_token,
                [TextMessage(text=f"❌ 形勢判斷失敗：{error}")],
            )
            return

        territory = result.get("territory")
        score_lead = result.get("scoreLead")

        # 組形勢文字
        if score_lead is None:
            shape_text = "目前無法可靠判斷形勢。"
        else:
            try:
                score_lead_val = float(score_lead)
            except (TypeError, ValueError):
                score_lead_val = 0.0

            if abs(score_lead_val) < 0.05:
                shape_text = "目前形勢：雙方大致均勢（約 0 目）。"
            else:
                # score_lead 一律為黑棋領先的目數（正=黑領先，負=白領先）
                if score_lead_val > 0:
                    leader = "黑"
                    lead = score_lead_val
                else:
                    leader = "白"
                    lead = -score_lead_val
                lead_rounded = round(lead * 2) / 2.0
                shape_text = f"目前形勢：{leader} +{lead_rounded:.1f} 目。"

        # 從 SGF 找最後一手座標，保持 last move 高亮
        last_coords = None
        sequence = sgf_game.get_main_sequence()
        for node in sequence:
            color, move = node.get_move()
            if move is not None:
                sgf_r, sgf_c = move
                r = 18 - sgf_r
                c = sgf_c
                last_coords = (r, c)

        # Draw board with territory overlay
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            filename = f"evaluation_{target_id}_{int(time.time())}.png"
            output_path = temp_path / filename

            visualizer.draw_board(
                game.board,
                last_move=last_coords,
                output_filename=str(output_path),
                territory=territory,
            )

            # Upload image to GCS
            from services.storage import upload_buffer
            game_id = await get_game_id(target_id)
            remote_path = f"target_{target_id}/boards/{game_id}/{filename}"

            with open(output_path, "rb") as f:
                image_bytes = f.read()

            gcs_path = await upload_buffer(
                image_bytes,
                remote_path,
                content_type="image/png",
                cache_control="no-cache, max-age=0",
            )

            # Get public URL for image
            public_url = config.get("server", {}).get("public_url")
            if public_url and gcs_path:
                # Extract path from gs://bucket/path
                if gcs_path.startswith("gs://"):
                    parts = gcs_path[5:].split("/", 1)
                    image_path = parts[1] if len(parts) > 1 else ""
                else:
                    image_path = gcs_path

                # Construct public URL (assuming GCS public URL structure)
                # This depends on your GCS bucket configuration
                bucket_name = config.get("gcs", {}).get("bucket_name")
                if bucket_name:
                    image_url = f"{public_url}/{image_path}"
                    messages = [
                        TextMessage(text=shape_text),
                        TextMessage(text="下圖勢力範圍僅供參考"),
                        ImageMessage(
                            original_content_url=image_url,
                            preview_image_url=image_url,
                        ),
                    ]
                    await send_message(target_id, reply_token, messages)
                    return

        # Fallback: text only
        await send_message(
            target_id,
            reply_token,
            [TextMessage(text=shape_text + "\n\n⚠️ 無法顯示棋盤圖片，請檢查配置。")],
        )
    except Exception as error:
        logger.error(f"Error in 形勢判斷 command: {error}", exc_info=True)
        await send_message(
            target_id,
            reply_token,
            [TextMessage(text=f"❌ 執行形勢判斷時發生錯誤：{str(error)}")],
        )
