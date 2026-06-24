"""
pipeline.py
-----------
Talks to a locally-running ComfyUI instance (default http://localhost:8188)
to generate a short anime-style video from a webcam photo using
LTX-Video 2.3 image-to-video + Fantasy Anime LoRA.

Replaces the previous InstantID/SDXL diffusers pipeline entirely.
ComfyUI handles all model loading, VRAM management, and generation.
This module is a thin API client + file manager.

Public interface (unchanged from the previous pipeline.py so app.py
doesn't need to know about the swap):
    load_pipeline()          -- verify ComfyUI is reachable, warm it up
    get_status()             -- health check dict for /api/status
    generate_portrait()      -- run generation, return Path to output mp4
    KioskPipelineError       -- raised on known-bad conditions
"""

import json
import logging
import time
import uuid
import shutil
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger("kiosk.pipeline")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COMFYUI_URL        = "http://localhost:8188"
WORKFLOW_PATH      = Path(__file__).resolve().parent / "ltxvideo_workflow.json"
OUTPUT_DIR         = Path(__file__).resolve().parent / "outputs"
TEMP_DIR           = Path(__file__).resolve().parent / "temp"

# ComfyUI saves generated videos under its own output dir; we copy them here.
# Adjust if your ComfyUI installation uses a non-default output path.
COMFYUI_OUTPUT_DIR = Path.home() / "ComfyUI" / "output"

# How long to wait for a generation before giving up (seconds).
GENERATION_TIMEOUT = 180

# Delay between status polls (seconds).
POLL_INTERVAL = 2.0

# ---------------------------------------------------------------------------
# Research field -> background motif prompts
# ---------------------------------------------------------------------------

FIELD_MOTIFS = {
    "physics":               "glowing particle accelerator rings swirling in background, floating luminous equations, electric plasma arcs, quantum energy fields",
    "biology":               "bioluminescent spores drifting upward, slowly rotating DNA helices glowing softly, cell membrane patterns, organic light trails",
    "computer science":      "cascading streams of glowing code, circuit board light patterns, holographic data structures, binary constellations",
    "medicine":              "radiant caduceus symbols, soft pulse waveforms, glowing cellular structures, anatomical light diagrams",
    "chemistry":             "crystalline molecular bonds forming and dissolving, glowing periodic element symbols, chemical reaction light bursts, prism refractions",
    "engineering":           "blueprint grid dissolving into golden light, structural framework glowing, gear mechanisms rendered in light, architectural light lines",
    "astronomy":             "deep space nebula clouds drifting, star field with comet trails, galactic spiral arms glowing, aurora ribbons",
    "psychology":            "synaptic light connections forming, soft neural network patterns, abstract mind-map light trails, calm aurora waves",
    "mathematics":           "golden ratio spiral unwinding in light, floating geometric proofs, fractal patterns blooming, luminous number fields",
    "environmental science": "aurora borealis ribbons, soft leaf and wind particle trails, bioluminescent ocean wave patterns, earth light from orbit",
    "other":                 "radiant abstract energy patterns, swirling light particles, soft geometric forms, dramatic volumetric light rays",
}

# Positive prompt template. Field motif is substituted at runtime.
PROMPT_TEMPLATE = (
    "f4nt4sy4n1m6, cinematic fantasy anime cel-shaded illustration, "
    "person standing in triumphant heroic salute pose, one fist raised "
    "toward camera, confident expression, looking directly at viewer, "
    "dramatic upward lighting, volumetric god rays from above, "
    "{field_motif}, "
    "particles and energy trails swirling around figure, "
    "wind motion in hair and clothing, epic cinematic atmosphere, "
    "high quality, detailed, vibrant colors, expressive"
)

NEGATIVE_PROMPT = (
    "blurry, low quality, distorted face, extra limbs, bad anatomy, "
    "text, watermark, duplicate, ugly, deformed, out of frame, "
    "multiple people, crowd, sitting, slouching"
)

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_state = {"ready": False, "workflow": None}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class KioskPipelineError(Exception):
    """Raised for known-bad conditions that should surface to the guest UI."""


# ---------------------------------------------------------------------------
# ComfyUI API helpers
# ---------------------------------------------------------------------------

def _comfy_get(path: str) -> dict:
    url = f"{COMFYUI_URL}{path}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def _comfy_post(path: str, payload: dict) -> dict:
    url = f"{COMFYUI_URL}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _upload_image(image_path: Path) -> str:
    """Upload a local image to ComfyUI's /upload/image endpoint.
    Returns the filename ComfyUI assigned to it."""
    import mimetypes
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"

    with open(image_path, "rb") as f:
        file_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{image_path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{COMFYUI_URL}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result["name"]


def _wait_for_job(prompt_id: str) -> dict:
    """Poll /history until the job completes or times out."""
    deadline = time.time() + GENERATION_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            history = _comfy_get(f"/history/{prompt_id}")
        except Exception:
            continue
        if prompt_id in history:
            entry = history[prompt_id]
            if entry.get("status", {}).get("status_str") == "error":
                msgs = entry.get("status", {}).get("messages", [])
                raise KioskPipelineError(f"ComfyUI generation failed: {msgs}")
            if entry.get("outputs"):
                return entry
    raise KioskPipelineError(
        "Generation timed out. The GPU may be under load — please try again."
    )


def _extract_video_path(history_entry: dict) -> Path:
    """Pull the output video path from a ComfyUI history entry."""
    outputs = history_entry.get("outputs", {})
    for node_output in outputs.values():
        for key in ("gifs", "videos"):
            if key in node_output:
                for item in node_output[key]:
                    filename = item.get("filename")
                    subfolder = item.get("subfolder", "")
                    if filename:
                        candidate = COMFYUI_OUTPUT_DIR / subfolder / filename
                        if candidate.exists():
                            return candidate
    raise KioskPipelineError(
        "Could not find output video in ComfyUI history. "
        "Check that VHS_VideoCombine node is saving to the output directory."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_pipeline():
    """Verify ComfyUI is reachable and load the workflow template."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    TEMP_DIR.mkdir(exist_ok=True)

    if not WORKFLOW_PATH.exists():
        raise RuntimeError(
            f"Workflow file not found: {WORKFLOW_PATH}\n"
            "Place ltxvideo_workflow.json next to pipeline.py."
        )

    with open(WORKFLOW_PATH) as f:
        _state["workflow"] = json.load(f)

    try:
        _comfy_get("/system_stats")
        logger.info("ComfyUI is reachable at %s", COMFYUI_URL)
    except Exception as e:
        raise RuntimeError(
            f"Cannot reach ComfyUI at {COMFYUI_URL}: {e}\n"
            "Start ComfyUI with: python main.py --listen 127.0.0.1 --port 8188"
        ) from e

    queue = _comfy_get("/queue")
    running = len(queue.get("queue_running", []))
    pending = len(queue.get("queue_pending", []))
    logger.info("ComfyUI queue: %d running, %d pending", running, pending)

    _state["ready"] = True
    logger.info("Pipeline ready.")


def get_status() -> dict:
    if not _state["ready"]:
        return {"ready": False, "backend": "comfyui"}
    try:
        stats = _comfy_get("/system_stats")
        return {"ready": True, "backend": "comfyui", "devices": stats.get("devices", [])}
    except Exception:
        return {"ready": False, "backend": "comfyui", "error": "ComfyUI unreachable"}


def generate_portrait(photo_path: Path, research_field: str, save_id: str) -> Path:
    """
    Run LTX-Video i2v generation for a single guest.

    Args:
        photo_path:     Path to the webcam capture JPEG.
        research_field: Guest's field (used to select background motif prompt).
        save_id:        UUID string used as the output filename stem.
                        Caller writes the matching .txt file with name/email.

    Returns:
        Path to the generated .mp4 in OUTPUT_DIR.

    Raises:
        KioskPipelineError on any known-bad condition.
    """
    if not _state["ready"]:
        raise KioskPipelineError("Pipeline not ready. Please wait a moment and try again.")
    if not photo_path.exists():
        raise KioskPipelineError("Photo missing. Please retake your photo.")

    # Build prompt
    field_key = research_field.strip().lower()
    motif = FIELD_MOTIFS.get(field_key, FIELD_MOTIFS["other"])
    positive_prompt = PROMPT_TEMPLATE.format(field_motif=motif)
    logger.info("[%s] Prompt: %.120s...", save_id, positive_prompt)

    # Upload image to ComfyUI, then delete temp file
    try:
        uploaded_name = _upload_image(photo_path)
        logger.info("[%s] Uploaded to ComfyUI as: %s", save_id, uploaded_name)
    except Exception as e:
        raise KioskPipelineError(f"Failed to upload photo to ComfyUI: {e}") from e
    finally:
        try:
            photo_path.unlink()
        except Exception:
            pass

    # Deep-copy workflow and substitute placeholders
    workflow = json.loads(json.dumps(_state["workflow"]))
    for node in workflow.values():
        inputs = node.get("inputs", {})
        for k, v in inputs.items():
            if v == "__INPUT_IMAGE_PATH__":
                inputs[k] = uploaded_name
            elif v == "__POSITIVE_PROMPT__":
                inputs[k] = positive_prompt
            elif v == "__OUTPUT_FILENAME_PREFIX__":
                inputs[k] = save_id
        if node.get("class_type") == "KSampler" and inputs.get("seed") == -1:
            inputs["seed"] = int(uuid.uuid4().int % (2 ** 32))

    # Queue the job
    try:
        result = _comfy_post("/prompt", {"prompt": workflow})
        prompt_id = result["prompt_id"]
        logger.info("[%s] Queued as ComfyUI prompt_id=%s", save_id, prompt_id)
    except Exception as e:
        raise KioskPipelineError(f"Failed to queue generation: {e}") from e

    # Wait for completion
    logger.info("[%s] Waiting (timeout=%ds)...", save_id, GENERATION_TIMEOUT)
    history_entry = _wait_for_job(prompt_id)

    # Retrieve and copy video into our outputs dir
    comfy_video_path = _extract_video_path(history_entry)
    dest = OUTPUT_DIR / f"{save_id}.mp4"
    shutil.copy2(comfy_video_path, dest)
    logger.info("[%s] Saved: %s", save_id, dest)
    return dest
