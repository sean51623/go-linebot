import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from config import config
from logger import logger

# Import handlers
from handlers.line_handler import handle_text_message, handle_file_message
from handlers.sgf_handler import (
    parse_sgf,
    filter_critical_moves,
    get_top_winrate_diff_moves,
)
from handlers.katago_handler import run_katago_analysis
from handlers.draw_handler import draw_all_moves_gif
from LLM.providers.openai_provider import call_openai
from cleanup import cleanup_old_images


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for startup and shutdown"""
    # Startup
    from handlers.line_handler import init_bot_user_id

    # Initialize bot user ID
    await init_bot_user_id()

    # Cleanup stale board images
    static_dir = PROJECT_ROOT / "static"
    if static_dir.exists():
        deleted = cleanup_old_images(str(static_dir))
        logger.info(f"Startup cleanup: deleted {deleted} stale board images")

    yield

    # Shutdown
    pass


app = FastAPI(title="Go Line Bot API", lifespan=lifespan)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Get project root directory
PROJECT_ROOT = Path(__file__).parent.parent

# Static file serving
static_dir = PROJECT_ROOT / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

draw_outputs_dir = PROJECT_ROOT / "draw" / "outputs"
if draw_outputs_dir.exists():
    app.mount(
        "/draw/outputs",
        StaticFiles(directory=str(draw_outputs_dir)),
        name="draw_outputs",
    )


@app.post(config["server"]["webhook_path"])
async def webhook(request: Request):
    """LINE Webhook handler"""
    try:
        body = await request.json()
        events = body.get("events", [])
        print("events", events)

        for event in events:
            # Handle message events (support 1-on-1, group, room)
            if event.get("type") == "message":
                # Ensure there's a valid source and corresponding ID
                source = event.get("source", {})
                has_valid_source = (
                    (source.get("type") == "user" and source.get("userId"))
                    or (source.get("type") == "group" and source.get("groupId"))
                    or (source.get("type") == "room" and source.get("roomId"))
                )

                if has_valid_source:
                    message = event.get("message", {})
                    if message.get("type") == "text":
                        await handle_text_message(event)
                    elif message.get("type") == "file":
                        await handle_file_message(event)

        return JSONResponse(content="OK", status_code=200)
    except Exception as error:
        logger.error(f"Webhook error: {error}", exc_info=True)
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)


@app.get("/health")
async def health():
    """Health check endpoint"""
    from datetime import datetime

    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/parse/sample-katrain")
async def parse_sample_katrain():
    """Parse sample-katrain SGF file"""
    try:
        static_dir = PROJECT_ROOT / "static"

        # Read all files in static directory
        if not static_dir.exists():
            raise HTTPException(status_code=404, detail="Static directory not found")

        files = [f.name for f in static_dir.iterdir() if f.is_file()]

        # Find katago-comment file
        katago_comment_file = next(
            (f for f in files if f.endswith(".sgf") and "sample-katrain" in f), None
        )

        if not katago_comment_file:
            raise HTTPException(
                status_code=404,
                detail="No katago-comment SGF file found in static directory",
            )

        # Read katago-comment SGF file
        sgf_path = static_dir / katago_comment_file
        sgf_content = sgf_path.read_text(encoding="utf-8")

        # Parse SGF content
        parsed_data = parse_sgf(sgf_content)
        # critical_moves = filter_critical_moves(parsed_data["moves"])
        top_score_loss_moves = get_top_winrate_diff_moves(parsed_data["moves"])

        # Return JSON
        return {
            "filename": katago_comment_file,
            "moves": top_score_loss_moves,
            "totalMoves": parsed_data["totalMoves"],
        }
    except Exception as error:
        logger.error(f"Error reading/parsing SGF file: {error}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to read or parse SGF file: {str(error)}"
        )


@app.get("/katago")
async def katago_analysis():
    """Execute KataGo analysis and return statistics"""
    try:
        # Build example-original.sgf file path
        static_dir = PROJECT_ROOT / "static"
        sgf_path = static_dir / "sample-raw.sgf"

        # Execute KataGo analysis
        logger.info(f"Starting KataGo analysis for: {sgf_path}")
        result = await run_katago_analysis(str(sgf_path), visits=5)

        # Check if analysis was successful
        if not result.get("success"):
            raise HTTPException(
                status_code=500,
                detail=f"KataGo analysis failed: {result.get('stderr', 'Unknown error')}",
            )

        # Check if moveStats exists
        if not result.get("moveStats"):
            raise HTTPException(
                status_code=500, detail="Failed to convert JSONL to move stats"
            )

        # Return moveStats
        return result["moveStats"]
    except Exception as error:
        logger.error(f"Error in /katago route: {error}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to run KataGo analysis: {str(error)}"
        )


@app.get("/katago/results/{filename}")
async def get_katago_result(filename: str):
    """Read .json file from katago/results"""
    try:
        results_dir = PROJECT_ROOT / "katago" / "results"
        file_path = results_dir / filename

        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")

        import json

        file_content = file_path.read_text(encoding="utf-8")
        result = json.loads(file_content)

        # critical_moves = filter_critical_moves(result["moves"])
        top_score_loss_moves = get_top_winrate_diff_moves(result["moves"])

        # Return JSON
        return {
            "filename": filename,
            "moves": top_score_loss_moves,
            "totalMoves": len(result["moves"]),
        }
    except Exception as error:
        logger.error(f"Error reading result file: {error}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to read result file: {str(error)}"
        )


@app.get("/katago/draw/{filename}")
async def katago_draw(filename: str):
    """Generate GIFs for KataGo analysis results"""
    try:
        results_dir = PROJECT_ROOT / "katago" / "results"
        json_file_path = results_dir / filename

        if not json_file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")

        file_content = json_file_path.read_text(encoding="utf-8")
        import json

        result = json.loads(file_content)

        # critical_moves = filter_critical_moves(result["moves"])
        top_score_loss_moves = get_top_winrate_diff_moves(result["moves"])

        # Generate all GIFs
        output_dir = PROJECT_ROOT / "draw" / "outputs" / filename.replace(".json", "")
        gif_paths = await draw_all_moves_gif(str(json_file_path), str(output_dir))

        # Return results
        return {
            "filename": filename,
            "moves": top_score_loss_moves,
            "totalMoves": len(result["moves"]),
            "gifs": [
                (
                    path.replace(str(PROJECT_ROOT), "").lstrip("/")
                    if not path.startswith("/")
                    else path.replace(str(PROJECT_ROOT), "")
                )
                for path in gif_paths
            ],
        }
    except Exception as error:
        logger.error(f"Error generating GIFs: {error}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to generate GIFs: {str(error)}"
        )


@app.get("/llm/{filename}")
async def llm_analysis(filename: str):
    """Read katago/results/*.json and call OpenAI"""
    try:
        results_dir = PROJECT_ROOT / "katago" / "results"
        json_file_path = results_dir / filename

        if not json_file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")

        # Read JSON file
        file_content = json_file_path.read_text(encoding="utf-8")
        import json

        katago_data = json.loads(file_content)

        # Filter critical moves
        # critical_moves = filter_critical_moves(katago_data["moves"])
        top_score_loss_moves = get_top_winrate_diff_moves(katago_data["moves"])

        # Call OpenAI
        response = await call_openai(top_score_loss_moves)

        # Return result
        return {"filename": filename, "llmResponse": response}
    except Exception as error:
        logger.error(f"Error calling OpenAI: {error}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to call OpenAI: {str(error)}"
        )


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=config["server"]["port"], reload=True)
