import asyncio
import os
import re
from typing import Optional

from linebot.v3.messaging.models import TextMessage

from config import config
from logger import logger
from handlers.line_sender import send_message


async def handle_review_command(target_id: str, reply_token: Optional[str]):
    """Handle review command - POST to localhost service for review"""
    import httpx
    import uuid

    used_reply_token = False

    try:
        # Get latest SGF file from reviews folder
        from services.storage import list_files, storage_client, bucket

        reviews_prefix = f"target_{target_id}/reviews/"
        all_files = await list_files(reviews_prefix)

        # Filter only SGF files
        sgf_files = [f for f in all_files if f.lower().endswith(".sgf")]

        if not sgf_files:
            used_reply_token = await send_message(
                target_id,
                reply_token,
                [TextMessage(text="❌ 找不到棋譜，請先上傳棋譜。")],
            )
            return

        # Get the latest SGF file by time created
        def get_latest_sgf():
            sgf_blobs = [bucket.blob(f) for f in sgf_files]
            # Reload to get time_created metadata
            for blob in sgf_blobs:
                blob.reload()
            # Sort by time created (newest first) and get the latest
            latest_blob = max(sgf_blobs, key=lambda b: b.time_created)
            return latest_blob.name

        latest_sgf_path = await asyncio.to_thread(get_latest_sgf)

        # Ensure it's a GCS path
        if not latest_sgf_path.startswith("gs://"):
            sgf_gcs_path = f"gs://{config['gcs']['bucket_name']}/{latest_sgf_path}"
        else:
            sgf_gcs_path = latest_sgf_path

        # Extract timestamp from latest_sgf_path as task_id
        # Path format: target_{target_id}/reviews/filename_timestamp.sgf
        # Extract timestamp from the filename
        filename = os.path.basename(latest_sgf_path)
        # Match pattern: name_timestamp.sgf where timestamp is digits
        timestamp_match = re.search(r"_(\d+)\.sgf$", filename)
        if timestamp_match:
            task_id = timestamp_match.group(1)
        else:
            # Fallback to UUID if timestamp not found
            task_id = str(uuid.uuid4())
            logger.warning(
                f"Could not extract timestamp from {latest_sgf_path}, using UUID: {task_id}"
            )

        # Get localhost URL and callback URL from config
        localhost_url = config.get("localhost_katago", {}).get("url")
        if localhost_url:
            # Ensure it ends with /review endpoint
            if not localhost_url.endswith("/review"):
                localhost_url = f"{localhost_url.rstrip('/')}/review"

        callback_review_url = config.get("cloud_run", {}).get("callback_review_url")

        if not localhost_url:
            logger.error("LOCALHOST_KATAGO_URL not configured")
            await send_message(
                target_id,
                reply_token,
                [TextMessage(text="❌ 系統配置錯誤：未設定本地 KataGo 服務 URL")],
            )
            return

        if not callback_review_url:
            logger.error("CLOUD_RUN_CALLBACK_REVIEW_URL not configured")
            await send_message(
                target_id,
                reply_token,
                [TextMessage(text="❌ 系統配置錯誤：未設定回調 URL")],
            )
            return

        # Notify start of review (use replyMessage if available)
        sgf_file_name = os.path.basename(sgf_gcs_path)
        # Only process SGF files, ignore other file types (e.g., JSON files)
        if sgf_file_name.lower().endswith(".sgf"):
            # Remove timestamp from filename (format: name_timestamp.sgf -> name.sgf)
            # Match pattern: name_timestamp.sgf where timestamp is digits
            sgf_file_name = re.sub(r"_(\d+)\.sgf$", r".sgf", sgf_file_name)
            # Remove .sgf extension for display
            sgf_file_name = sgf_file_name[:-4]
        else:
            # If not SGF file, use filename as-is (should not happen, but handle gracefully)
            logger.warning(f"Expected SGF file but got: {sgf_file_name}")
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

        # POST review request to localhost service
        logger.info(f"Posting review request to localhost: {localhost_url}")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                localhost_url,
                json={
                    "task_id": task_id,
                    "sgf_gcs_path": sgf_gcs_path,
                    "callback_url": callback_review_url,
                    "target_id": target_id,
                    "visits": 5,
                },
            )
            response.raise_for_status()
            logger.info(f"Successfully posted review request: {response.status_code}")

        # Review will continue asynchronously via callback
        # No need to wait here
    except Exception as error:
        logger.error(f"Error in 覆盤 command: {error}", exc_info=True)
        await send_message(
            target_id,
            None,
            [TextMessage(text=f"❌ 執行覆盤時發生錯誤：{str(error)}")],
        )
