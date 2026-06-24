"""
app.py
------
Flask kiosk server for the AI portrait video booth.
Accepts webcam photos + guest details, queues generation via ComfyUI,
saves UUID-paired output files, and returns the video to the browser.

Endpoints:
    GET  /                  -> kiosk page
    POST /api/capture       -> save webcam frame as working photo
    POST /api/generate      -> run generation, save output pair, return video URL
    POST /api/retake        -> discard current working photo
    GET  /api/fields        -> research field list for dropdown
    GET  /outputs/<file>    -> serve a generated video
    GET  /api/status        -> health check
"""

import logging
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, render_template

import pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("kiosk.app")

app = Flask(__name__)

BASE_DIR   = Path(__file__).resolve().parent
TEMP_DIR   = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "outputs"

RESEARCH_FIELDS = [
    "Physics", "Biology", "Computer Science", "Medicine", "Chemistry",
    "Engineering", "Astronomy", "Psychology", "Mathematics",
    "Environmental Science",
]

# ---------------------------------------------------------------------------
# In-memory session state
# One guest at a time; no database needed for a kiosk.
# ---------------------------------------------------------------------------

_sessions      = {}
_sessions_lock = threading.Lock()


def _session_photo_path(session_id: str) -> Path:
    return TEMP_DIR / f"capture_{session_id}.jpg"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", fields=RESEARCH_FIELDS)


@app.route("/api/fields")
def api_fields():
    return jsonify({"fields": RESEARCH_FIELDS})


@app.route("/api/status")
def api_status():
    return jsonify(pipeline.get_status())


@app.route("/api/capture", methods=["POST"])
def api_capture():
    """Receive a base64 JPEG from the browser webcam and store it."""
    import base64
    data       = request.get_json(silent=True) or {}
    image_b64  = data.get("image")
    session_id = data.get("session_id") or uuid.uuid4().hex[:12]

    if not image_b64:
        return jsonify({"ok": False, "error": "No image data received."}), 400

    try:
        _, encoded = image_b64.split(",", 1) if "," in image_b64 else ("", image_b64)
        raw = base64.b64decode(encoded)
    except Exception:
        return jsonify({"ok": False, "error": "Could not decode image data."}), 400

    photo_path = _session_photo_path(session_id)
    photo_path.write_bytes(raw)

    with _sessions_lock:
        _sessions[session_id] = {"photo_path": str(photo_path)}

    logger.info("[%s] Photo captured.", session_id)
    return jsonify({"ok": True, "session_id": session_id})


@app.route("/api/retake", methods=["POST"])
def api_retake():
    """Discard the current working photo so the guest can try again."""
    data       = request.get_json(silent=True) or {}
    session_id = data.get("session_id")

    if session_id:
        with _sessions_lock:
            session = _sessions.pop(session_id, None)
        if session:
            photo_path = Path(session["photo_path"])
            if photo_path.exists():
                photo_path.unlink()
            logger.info("[%s] Photo discarded (retake).", session_id)

    return jsonify({"ok": True})


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """
    Run generation for the current session.

    Expects JSON body:
        session_id      str   -- from /api/capture
        research_field  str   -- selected field
        name            str   -- guest's name (for email dispatch)
        email           str   -- guest's email address

    On success, saves:
        outputs/<save_id>.mp4   -- the generated video
        outputs/<save_id>.txt   -- name + email for post-event dispatch

    Returns:
        { ok: true, video_url: "/outputs/<save_id>.mp4", save_id: "..." }
    """
    data           = request.get_json(silent=True) or {}
    session_id     = data.get("session_id")
    research_field = (data.get("research_field") or "").strip()
    name           = (data.get("name") or "").strip()
    email          = (data.get("email") or "").strip()

    if not session_id:
        return jsonify({"ok": False, "error": "Missing session. Please retake your photo."}), 400

    with _sessions_lock:
        session = _sessions.get(session_id)
    if not session:
        return jsonify({"ok": False, "error": "Session expired. Please retake your photo."}), 400

    if not research_field:
        return jsonify({"ok": False, "error": "Please choose or enter a research field."}), 400

    if not name:
        return jsonify({"ok": False, "error": "Please enter your name."}), 400

    if not email or "@" not in email:
        return jsonify({"ok": False, "error": "Please enter a valid email address."}), 400

    photo_path = Path(session["photo_path"])
    if not photo_path.exists():
        return jsonify({"ok": False, "error": "Photo missing. Please retake your photo."}), 400

    # Generate a UUID for this guest's saved files
    save_id = uuid.uuid4().hex

    try:
        out_video = pipeline.generate_portrait(photo_path, research_field, save_id)
    except pipeline.KioskPipelineError as e:
        logger.warning("[%s] Generation failed: %s", session_id, e)
        return jsonify({"ok": False, "error": str(e)}), 422
    except Exception:
        logger.exception("[%s] Unexpected generation error", session_id)
        return jsonify({
            "ok": False,
            "error": "Something went wrong generating your video. Please try again."
        }), 500
    finally:
        # Drop session reference regardless of outcome.
        # The photo itself is deleted inside pipeline.generate_portrait.
        with _sessions_lock:
            _sessions.pop(session_id, None)

    # Save the guest info alongside the video for post-event email dispatch
    txt_path = OUTPUT_DIR / f"{save_id}.txt"
    txt_path.write_text(
        f"name={name}\n"
        f"email={email}\n"
        f"field={research_field}\n"
        f"video={out_video.name}\n",
        encoding="utf-8"
    )

    logger.info(
        "[%s] Done. save_id=%s name=%s email=%s",
        session_id, save_id, name, email
    )
    return jsonify({
        "ok":       True,
        "video_url": f"/outputs/{out_video.name}",
        "save_id":  save_id,
    })


@app.route("/outputs/<path:filename>")
def serve_output(filename):
    return send_from_directory(OUTPUT_DIR, filename)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    TEMP_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    logger.info("Connecting to ComfyUI and verifying pipeline...")
    pipeline.load_pipeline()
    logger.info("Starting kiosk server on http://127.0.0.1:5000")

    # threaded=False: one generation at a time matches our single-GPU setup.
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=False)
