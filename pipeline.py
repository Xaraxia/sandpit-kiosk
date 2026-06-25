"""
pipeline.py
-----------
Talks to a locally-running ComfyUI instance (default http://localhost:8188)
to generate an anime-style portrait using Flux.1-dev fp8 + PuLID for
identity-preserving face conditioning.

Public interface:
    load_pipeline()          -- verify ComfyUI is reachable
    get_status()             -- health check dict for /api/status
    generate_portrait()      -- run generation, return Path to output png
    KioskPipelineError       -- raised on known-bad conditions
"""

import json
import logging
import time
import uuid
import shutil
import urllib.request
from pathlib import Path

logger = logging.getLogger("kiosk.pipeline")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COMFYUI_URL        = "http://localhost:8188"
WORKFLOW_PATH      = Path(__file__).resolve().parent / "flux_pulid_workflow.json"
OUTPUT_DIR         = Path(__file__).resolve().parent / "outputs"
TEMP_DIR           = Path(__file__).resolve().parent / "temp"
COMFYUI_OUTPUT_DIR = Path.home() / "ComfyUI" / "output"

GENERATION_TIMEOUT = 180
POLL_INTERVAL      = 2.0

# ---------------------------------------------------------------------------
# Research field -> background/scene prompt elements
# ---------------------------------------------------------------------------

FIELD_MOTIFS = {
    "physics":               "glowing particle accelerator rings, floating luminous equations, electric plasma arcs",
    "biology":               "bioluminescent spores, softly glowing DNA helices, organic light trails",
    "computer science":      "cascading streams of glowing code, holographic data structures, circuit light patterns",
    "medicine":              "radiant caduceus symbols, soft pulse waveforms, glowing cellular structures",
    "chemistry":             "crystalline molecular bonds, glowing periodic element symbols, prism refractions",
    "engineering":           "blueprint grid dissolving into golden light, structural framework glowing, gear mechanisms in light",
    "astronomy":             "deep space nebula clouds, star field with comet trails, galactic spiral arms glowing",
    "psychology":            "synaptic light connections, soft neural network patterns, calm aurora waves",
    "mathematics":           "golden ratio spiral in light, floating geometric proofs, fractal patterns blooming",
    "environmental science": "aurora borealis ribbons, bioluminescent ocean wave patterns, soft leaf and wind particles",
    "other":                 "radiant abstract energy patterns, swirling light particles, dramatic volumetric light rays",
}

PROMPT_TEMPLATE = (
    "anime style portrait, cel shaded illustration, expressive anime eyes, "
    "clean linework, vibrant colors, person in triumphant heroic pose "
    "with one fist raised, confident warm expression, looking at viewer, "
    "dramatic upward lighting, {field_motif} in background, "
    "high quality anime art, detailed face, studio lighting"
)

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_state = {"ready": False, "workflow": None}


class KioskPipelineError(Exception):
    pass


# ---------------------------------------------------------------------------
# ComfyUI API helpers
# ---------------------------------------------------------------------------

def _comfy_get(path: str) -> dict:
    with urllib.request.urlopen(f"{COMFYUI_URL}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def _comfy_post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        f"{COMFYUI_URL}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _upload_image(image_path: Path) -> str:
    import mimetypes
    boundary = uuid.uuid4().hex
    mime     = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    with open(image_path, "rb") as f:
        file_data = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{image_path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{COMFYUI_URL}/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["name"]


def _wait_for_job(prompt_id: str) -> dict:
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


def _extract_image_path(history_entry: dict) -> Path:
    outputs = history_entry.get("outputs", {})
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for item in node_output.get("images", []):
            filename  = item.get("filename")
            subfolder = item.get("subfolder", "")
            if filename:
                candidate = COMFYUI_OUTPUT_DIR / subfolder / filename
                if candidate.exists():
                    return candidate
    raise KioskPipelineError(
        "Could not find output image in ComfyUI history. "
        "Check that SaveImage node is saving to the output directory."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_pipeline():
    OUTPUT_DIR.mkdir(exist_ok=True)
    TEMP_DIR.mkdir(exist_ok=True)

    if not WORKFLOW_PATH.exists():
        raise RuntimeError(
            f"Workflow file not found: {WORKFLOW_PATH}\n"
            "Place flux_pulid_workflow.json next to pipeline.py."
        )
    with open(WORKFLOW_PATH) as f:
        _state["workflow"] = json.load(f)

    try:
        _comfy_get("/system_stats")
        logger.info("ComfyUI reachable at %s", COMFYUI_URL)
    except Exception as e:
        raise RuntimeError(
            f"Cannot reach ComfyUI at {COMFYUI_URL}: {e}\n"
            "Start ComfyUI with: python main.py --listen 127.0.0.1 --port 8188"
        ) from e

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
    Generate an anime-style portrait with Flux + PuLID face conditioning.

    Args:
        photo_path:     Path to webcam capture JPEG (deleted after upload).
        research_field: Guest's field (selects background motif).
        save_id:        UUID stem for output filename.

    Returns:
        Path to generated .png in OUTPUT_DIR.
    """
    if not _state["ready"]:
        raise KioskPipelineError("Pipeline not ready. Please wait and try again.")
    if not photo_path.exists():
        raise KioskPipelineError("Photo missing. Please retake your photo.")

    # Build prompt
    motif   = FIELD_MOTIFS.get(research_field.strip().lower(), FIELD_MOTIFS["other"])
    prompt  = PROMPT_TEMPLATE.format(field_motif=motif)
    logger.info("[%s] Prompt: %.120s...", save_id, prompt)

    # Upload photo
    try:
        uploaded_name = _upload_image(photo_path)
        logger.info("[%s] Uploaded as: %s", save_id, uploaded_name)
    except Exception as e:
        raise KioskPipelineError(f"Failed to upload photo: {e}") from e
    finally:
        try:
            photo_path.unlink()
        except Exception:
            pass

    # Substitute placeholders into workflow
    workflow = json.loads(json.dumps(_state["workflow"]))
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        for k, v in inputs.items():
            if v == "__INPUT_IMAGE_PATH__":
                inputs[k] = uploaded_name
            elif v == "__POSITIVE_PROMPT__":
                inputs[k] = prompt
            elif v == "__OUTPUT_FILENAME_PREFIX__":
                inputs[k] = save_id
        # Always randomise KSampler seed
        if node.get("class_type") == "KSampler":
            inputs["seed"] = int(uuid.uuid4().int % (2 ** 32))

    # Queue job
    try:
        result    = _comfy_post("/prompt", {"prompt": workflow})
        prompt_id = result["prompt_id"]
        logger.info("[%s] Queued as prompt_id=%s", save_id, prompt_id)
    except Exception as e:
        raise KioskPipelineError(f"Failed to queue generation: {e}") from e

    # Wait
    logger.info("[%s] Waiting (timeout=%ds)...", save_id, GENERATION_TIMEOUT)
    history_entry = _wait_for_job(prompt_id)

    # Copy output
    comfy_image = _extract_image_path(history_entry)
    dest        = OUTPUT_DIR / f"{save_id}.png"
    shutil.copy2(comfy_image, dest)
    logger.info("[%s] Saved: %s", save_id, dest)
    return dest
