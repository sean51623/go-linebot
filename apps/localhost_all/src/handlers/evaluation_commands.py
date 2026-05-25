import time
from pathlib import Path
from typing import Optional

from linebot.v3.messaging.models import TextMessage, ImageMessage

from config import config
from logger import logger
from handlers.game_state import get_game_state, get_game_id, save_game_sgf
from handlers.line_sender import send_message, is_valid_https_url, encode_url_path
from handlers.board_visualizer import BoardVisualizer
from handlers.katago_handler import run_katago_analysis_evaluation


_current_file = Path(__file__)
_project_root = _current_file.parent.parent.parent
_assets_dir = _project_root / "assets"
visualizer = BoardVisualizer(assets_dir=str(_assets_dir))


async def handle_evaluation_command(target_id: str, reply_token: Optional[str]):
    """Handle shape evaluation command (形勢判斷 / evaluation)"""
    try:
        state = get_game_state(target_id)
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
        sgf_path = save_game_sgf(target_id)
        if not sgf_path:
            await send_message(
                target_id,
                reply_token,
                [TextMessage(text="❌ 無法儲存目前棋局 SGF，請稍後再試。")],
            )
            return

        # 呼叫 KataGo analysis evaluation
        logger.info(
            f"Running KataGo evaluation for target_id={target_id}, sgf_path={sgf_path}"
        )
        result = await run_katago_analysis_evaluation(sgf_path, current_turn)

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

        current_file = Path(__file__)
        project_root = current_file.parent.parent.parent
        static_dir = project_root / "static"

        game_id = get_game_id(target_id)
        game_dir = static_dir / game_id
        game_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time())
        filename = f"evaluation_{target_id}_{timestamp}.png"
        output_path = game_dir / filename

        visualizer.draw_board(
            game.board,
            last_move=last_coords,
            output_filename=str(output_path),
            territory=territory,
        )

        public_url = config["server"]["public_url"]
        if public_url and is_valid_https_url(public_url):
            relative_path = f"static/{game_id}/{filename}"
            encoded_path = encode_url_path(relative_path)
            image_url = f"{public_url}/{encoded_path}"

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

        # 若 PUBLIC_URL 無效或圖片 URL 非 https，僅回文字
        await send_message(
            target_id,
            reply_token,
            [TextMessage(text=shape_text + "\n\n⚠️ 無法顯示棋盤圖片，請檢查 PUBLIC_URL 設定。")],
        )
    except Exception as error:
        logger.error(f"Error in 形勢判斷 command: {error}", exc_info=True)
        await send_message(
            target_id,
            reply_token,
            [TextMessage(text=f"❌ 執行形勢判斷時發生錯誤：{str(error)}")],
        )
