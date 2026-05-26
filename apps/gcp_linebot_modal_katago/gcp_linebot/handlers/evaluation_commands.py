import asyncio
import time
import tempfile
from pathlib import Path
from typing import Optional

from linebot.v3.messaging.models import TextMessage, ImageMessage

from config import config
from logger import logger
from handlers.game_state import get_game_state, save_game_sgf, get_game_id
from handlers.line_sender import send_message, is_valid_https_url
from handlers.game_commands import visualizer


async def handle_evaluation_command(target_id: str, reply_token: Optional[str]):
    """Handle shape evaluation command (形勢判斷 / evaluation)"""
    import modal

    try:
        # Check authentication only if AUTH_TOKEN is configured
        auth_token = config.get("auth", {}).get("token")
        if auth_token:
            from services.storage import check_auth

            is_authenticated = await check_auth(target_id, auth_token)
            if not is_authenticated:
                await send_message(
                    target_id,
                    reply_token,
                    [TextMessage(text="❌ 請先使用 'auth <token>' 指令進行認證，才可使用形勢判斷功能")],
                )
                return

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

        # Get Modal app name and function name from config
        modal_app_name = config.get("modal", {}).get("app_name")
        modal_function_evaluation = config.get("modal", {}).get("function_evaluation", "evaluation")

        if not modal_app_name:
            logger.error("MODAL_APP_NAME not configured")
            await send_message(
                target_id,
                reply_token,
                [TextMessage(text="❌ 系統配置錯誤：未設定 Modal 應用程式名稱")],
            )
            return

        # Call Modal function synchronously (wait for result)
        logger.info(f"Calling Modal function: {modal_app_name}.{modal_function_evaluation}")
        try:
            evaluation_function = modal.Function.from_name(
                modal_app_name, modal_function_evaluation
            )

            # Call the function synchronously (blocking)
            # visits = config.get("modal", {}).get("visits", 1000)
            result = evaluation_function.remote(
                sgf_gcs_path=sgf_gcs_path,
                current_turn=current_turn,
                # visits=visits,
            )
            logger.info(f"Successfully received evaluation result")

        except Exception as modal_error:
            logger.error(f"Error calling Modal function: {modal_error}", exc_info=True)
            await send_message(
                target_id,
                reply_token,
                [TextMessage(text=f"❌ 調用 Modal 函數時發生錯誤：{str(modal_error)}")],
            )
            return

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
            filename = f"evaluation_{int(time.time())}.png"
            output_path = temp_path / filename

            visualizer.draw_board(
                game.board,
                last_move=last_coords,
                output_filename=str(output_path),
                territory=territory,
            )

            # Upload image to GCS
            from services.storage import upload_buffer, get_public_url
            game_id = await get_game_id(target_id)
            remote_path = f"target_{target_id}/boards/{game_id}/{filename}"
            with open(output_path, "rb") as f:
                image_bytes = f.read()
            await upload_buffer(
                image_bytes,
                remote_path,
                content_type="image/png",
                cache_control="no-cache, max-age=0",
            )
            image_url = get_public_url(remote_path)
            if is_valid_https_url(image_url):
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
            logger.warning(f"Invalid image URL: {image_url}")

        # Fallback: text only
        await send_message(
            target_id,
            reply_token,
            [TextMessage(text=shape_text + "\n\n⚠️ 無法顯示棋盤圖片，請檢查 GCS 或 public URL 設定。")],
        )
    except Exception as error:
        logger.error(f"Error in 形勢判斷 command: {error}", exc_info=True)
        await send_message(
            target_id,
            reply_token,
            [TextMessage(text=f"❌ 執行形勢判斷時發生錯誤：{str(error)}")],
        )
