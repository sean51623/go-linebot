import asyncio
import json
import os
import re
from pathlib import Path
from typing import Optional

from linebot.v3.messaging.models import TextMessage, ImageMessage, FlexMessage, FlexContainer

from config import config
from logger import logger
import handlers.game_state as _game_state
from handlers.game_state import get_game_id
from handlers.line_sender import (
    send_message, is_valid_https_url, encode_url_path,
    create_video_preview_bubble, create_carousel_flex_message,
)
from handlers.katago_handler import run_katago_analysis
from handlers.sgf_handler import get_top_winrate_diff_moves
from handlers.draw_handler import draw_all_moves_gif
from LLM.providers.openai_provider import call_openai


async def handle_review_command(target_id: str, reply_token: Optional[str]):
    """Handle review command"""
    current_file = Path(__file__)
    project_root = current_file.parent.parent.parent
    static_dir = project_root / "static"
    used_reply_token = False

    try:
        sgf_file_name = _game_state.current_sgf_file_name
        if not sgf_file_name:
            used_reply_token = await send_message(
                target_id,
                reply_token,
                [TextMessage(text="❌ 找不到棋譜，請先上傳棋譜。")],
            )
            return

        sgf_path = static_dir / sgf_file_name

        # Notify start of analysis (use replyMessage if available)
        used_reply_token = await send_message(
            target_id,
            reply_token,
            [
                TextMessage(
                    text=f"✅ 開始對棋譜：{sgf_file_name} 進行覆盤分析，完成大約需要 5 分鐘...，請稍後再回來查看分析結果。"
                )
            ],
        )

        # After using replyToken, set to None, subsequent messages use pushMessage
        if used_reply_token:
            reply_token = None

        # Execute KataGo analysis
        print(f"Starting KataGo analysis for: {sgf_path}")
        result = await run_katago_analysis(str(sgf_path), visits=5)

        # Check if analysis was successful
        if not result.get("success"):
            await send_message(
                target_id,
                None,  # replyToken already used or doesn't exist
                [
                    TextMessage(
                        text=f"❌ KataGo 分析失敗：{result.get('stderr', '未知錯誤')}"
                    )
                ],
            )
            return

        # Check if moveStats exists
        if not result.get("moveStats"):
            await send_message(
                target_id, None, [TextMessage(text="❌ 分析完成但無法轉換結果數據")]
            )
            return

        # Analysis successful, notify user
        await send_message(
            target_id,
            None,
            [
                TextMessage(
                    text=f"""✅ KataGo 全盤分析完成！

📊 分析結果：
• 檔案：{sgf_file_name}
• 總手數：{len(result['moveStats']['moves'])}

🤖 接續使用 ChatGPT 分析 20 筆關鍵手數並生成評論，大約需要 1 分鐘...，請稍後再回來查看評論結果。"""
                )
            ],
        )

        # Filter top 20 critical points
        # critical_moves = filter_critical_moves(result["moveStats"]["moves"])
        top_score_loss_moves = get_top_winrate_diff_moves(
            result["moveStats"]["moves"], 20
        )

        logger.info("Preparing to call OpenAI...")

        # Call LLM to get comments
        llm_comments = await call_openai(top_score_loss_moves)
        # llm_comments = []
        logger.info(f"LLM generated {len(llm_comments)} comments")

        # Use result.jsonPath (full path) instead of result.jsonFilename
        json_file_path = result.get("jsonPath")
        if not json_file_path:
            logger.warning(f"KataGo analysis result: {result}")
            await send_message(
                target_id,
                None,
                [TextMessage(text="❌ 無法取得 KataGo 分析結果檔案路徑")],
            )
            return

        # Extract filename from full path (without extension)
        json_filename = os.path.basename(json_file_path).replace(".json", "")
        output_dir = project_root / "draw" / "outputs" / json_filename

        logger.info(f"JSON file path: {json_file_path}")
        logger.info(f"Output directory: {output_dir}")

        gif_paths = await draw_all_moves_gif(json_file_path, str(output_dir))
        logger.info(f"Generated {len(gif_paths)} GIFs")

        # Create comment mapping (move number -> comment)
        comment_map = {item["move"]: item["comment"] for item in llm_comments}

        # Create GIF mapping (move number -> gif path)
        gif_map = {}
        for path in gif_paths:
            filename = os.path.basename(path)
            match = re.search(r"move_(\d+)\.gif", filename)
            if match:
                gif_map[int(match.group(1))] = path

        # First send global_board.png to let user see full board sequence
        global_board_path = output_dir / "global_board.png"
        public_url = config["server"]["public_url"]

        try:
            if public_url and is_valid_https_url(public_url):
                # Build public URL for full board image
                relative_path = str(global_board_path).split("/draw/outputs/")[1]
                # Encode path to handle spaces and special characters
                encoded_path = encode_url_path(relative_path)
                global_board_url = f"{public_url}/draw/outputs/{encoded_path}"

                # Validate built URL is valid
                if is_valid_https_url(global_board_url):
                    # Check if winrate chart exists
                    winrate_chart_path = output_dir / "winrate_chart.png"
                    winrate_chart_url = None
                    if winrate_chart_path.exists():
                        relative_path = str(winrate_chart_path).split("/draw/outputs/")[1]
                        encoded_path = encode_url_path(relative_path)
                        winrate_chart_url = f"{public_url}/draw/outputs/{encoded_path}"
                        if not is_valid_https_url(winrate_chart_url):
                            winrate_chart_url = None

                    # Build messages array
                    messages = [
                        TextMessage(text="🗺️ 全盤手順圖："),
                        ImageMessage(
                            original_content_url=global_board_url,
                            preview_image_url=global_board_url,
                        ),
                    ]

                    # Add winrate chart if available
                    if winrate_chart_url:
                        messages.extend([
                            TextMessage(text="📈 勝率變化圖："),
                            ImageMessage(
                                original_content_url=winrate_chart_url,
                                preview_image_url=winrate_chart_url,
                            ),
                        ])

                    # Send all messages in one call
                    await send_message(target_id, None, messages)
                else:
                    logger.warning(
                        f"Invalid HTTPS URL for global board: {global_board_url}"
                    )
                    await send_message(
                        target_id,
                        None,
                        [
                            TextMessage(
                                text="🗺️ 全盤手順圖已生成\n\n⚠️ 圖片 URL 無效（必須使用 HTTPS）\n請檢查 PUBLIC_URL 環境變數設定"
                            )
                        ],
                    )
            else:
                logger.warning(f"PUBLIC_URL not set or not HTTPS: {public_url}")
                await send_message(
                    target_id,
                    None,
                    [
                        TextMessage(
                            text="🗺️ 全盤手順圖已生成\n\n⚠️ 未設定有效的 PUBLIC_URL（必須使用 HTTPS）\n請在環境變數中設定 PUBLIC_URL"
                        )
                    ],
                )

            # Wait 1 second before starting to send each move's comment
            await asyncio.sleep(1)
        except Exception as global_board_error:
            print(f"Error sending global board image: {global_board_error}")
            # Even if full board image send fails, continue sending other content

        # Collect all critical moves' bubbles (for merging into Carousel)
        all_bubbles = []
        fallback_messages = (
            []
        )  # Messages that can't generate bubbles (e.g., invalid URL)

        for i, move in enumerate(top_score_loss_moves):
            move_number = move["move"]
            comment = comment_map.get(move_number, "無評論")
            gif_path = gif_map.get(move_number)

            # If there's a GIF, try to create bubble
            if gif_path:
                try:
                    if public_url and is_valid_https_url(public_url):
                        relative_path = gif_path.split("/draw/outputs/")[1]
                        encoded_path = encode_url_path(relative_path)

                        # Replace .gif with .mp4
                        mp4_path = encoded_path.replace(".gif", ".mp4")
                        mp4_url = f"{public_url}/draw/outputs/{mp4_path}"

                        # GIF as preview image
                        gif_url = f"{public_url}/draw/outputs/{encoded_path}"

                        # Validate built URLs are valid
                        if is_valid_https_url(mp4_url) and is_valid_https_url(gif_url):
                            logger.info(f"Creating bubble for move {move_number}")

                            # Create bubble (for Carousel)
                            bubble = create_video_preview_bubble(
                                move_number,
                                move["color"],
                                move["played"],
                                comment,
                                gif_url,
                                mp4_url,
                                winrate_before=move.get("winrate_before"),
                                winrate_after=move.get("winrate_after"),
                                score_loss=move.get("score_loss"),
                            )

                            all_bubbles.append(bubble)
                        else:
                            logger.warning(
                                f"Invalid HTTPS URL for move {move_number}: {mp4_url}"
                            )
                            # If URL invalid, record as fallback message
                            fallback_messages.append(
                                {
                                    "moveNumber": move_number,
                                    "text": f"📍 第 {move_number} 手（{'黑' if move['color'] == 'B' else '白'}）- {move['played']}\n\n{comment}\n\n⚠️ 影片連結無效",
                                }
                            )
                    else:
                        # If no valid PUBLIC_URL, record as fallback message
                        fallback_messages.append(
                            {
                                "moveNumber": move_number,
                                "text": f"📍 第 {move_number} 手（{'黑' if move['color'] == 'B' else '白'}）- {move['played']}\n\n{comment}",
                            }
                        )
                except Exception as flex_error:
                    logger.error(
                        f"Error preparing bubble for move {move_number}: {flex_error}",
                        exc_info=True,
                    )
                    # On error, record as fallback message
                    fallback_messages.append(
                        {
                            "moveNumber": move_number,
                            "text": f"📍 第 {move_number} 手（{'黑' if move['color'] == 'B' else '白'}）- {move['played']}\n\n{comment}",
                        }
                    )
            else:
                # If no GIF, record as fallback message
                fallback_messages.append(
                    {
                        "moveNumber": move_number,
                        "text": f"📍 第 {move_number} 手（{'黑' if move['color'] == 'B' else '白'}）- {move['played']}\n\n{comment}",
                    }
                )

        # Send Carousel in batches (LINE limits each group to max 12 bubbles, set to 10 for stability)
        MAX_BUBBLES_PER_CAROUSEL = 10
        total_bubbles = len(all_bubbles)

        if total_bubbles > 0:
            logger.info(f"Sending {total_bubbles} bubbles in Carousel format")

            # Process in batches
            for i in range(0, len(all_bubbles), MAX_BUBBLES_PER_CAROUSEL):
                batch = all_bubbles[i : i + MAX_BUBBLES_PER_CAROUSEL]
                start_index = i + 1
                end_index = min(i + len(batch), total_bubbles)

                try:
                    # Create Carousel Flex Message
                    carousel_message = create_carousel_flex_message(
                        batch, start_index, total_bubbles
                    )

                    # Create FlexMessage from carousel_message dict
                    # carousel_message is already in the correct format for FlexMessage
                    # Use from_json to create FlexContainer from the carousel contents
                    carousel_contents = carousel_message["contents"]
                    flex_container = FlexContainer.from_json(
                        json.dumps(carousel_contents)
                    )
                    flex_message = FlexMessage(
                        alt_text=carousel_message["altText"], contents=flex_container
                    )
                    await send_message(target_id, None, [flex_message])

                    logger.info(
                        f"Sent Carousel {i // MAX_BUBBLES_PER_CAROUSEL + 1} (moves {start_index}-{end_index})"
                    )

                    # Avoid sending too fast, wait 1 second
                    if i + MAX_BUBBLES_PER_CAROUSEL < len(all_bubbles):
                        await asyncio.sleep(1)
                except Exception as carousel_error:
                    logger.error(
                        f"Error sending Carousel (moves {start_index}-{end_index}): {carousel_error}",
                        exc_info=True,
                    )

        # Send fallback messages that can't generate bubbles (if any)
        if fallback_messages:
            logger.info(f"Sending {len(fallback_messages)} fallback text messages")
            for fallback in fallback_messages:
                try:
                    await send_message(
                        target_id, None, [TextMessage(text=fallback["text"])]
                    )
                    await asyncio.sleep(0.5)
                except Exception as fallback_error:
                    logger.error(
                        f"Error sending fallback message for move {fallback['moveNumber']}: {fallback_error}",
                        exc_info=True,
                    )
    except Exception as error:
        logger.error(f"Error in 覆盤 command: {error}", exc_info=True)
        await send_message(
            target_id,
            None,
            [TextMessage(text=f"❌ 執行覆盤時發生錯誤：{str(error)}")],
        )
