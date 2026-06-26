"""
pipeline.py
-----------
Talks to a locally-running ComfyUI instance (default http://localhost:8188)
to generate an anime-style portrait using Animagine XL 3.1 + IPAdapter for
identity-preserving face conditioning.

Public interface:
    load_pipeline()          -- verify ComfyUI is reachable, load workflow template
    get_status()             -- health check dict for /api/status
    generate_portrait()      -- run generation, return Path to output image
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
WORKFLOW_PATH      = Path(__file__).resolve().parent / "animagine_workflow_api.json"
OUTPUT_DIR         = Path(__file__).resolve().parent / "outputs"
TEMP_DIR           = Path(__file__).resolve().parent / "temp"
COMFYUI_OUTPUT_DIR = Path.home() / "ComfyUI" / "output"

GENERATION_TIMEOUT = 180
POLL_INTERVAL      = 2.0

# Placeholder tokens expected inside the workflow JSON template.
PLACEHOLDER_IMAGE   = "__INPUT_IMAGE_PATH__"
PLACEHOLDER_PROMPT  = "__POSITIVE_PROMPT__"
PLACEHOLDER_PREFIX  = "__OUTPUT_FILENAME_PREFIX__"

# ---------------------------------------------------------------------------
# Theme -> (setting, costume/prop, motif) bundle
#
# Categories are aesthetic, not discipline — chosen deliberately so every
# option renders visually distinct from every other, rather than trying to
# make e.g. "Physics" and "Engineering" look different when they don't.
# Pose is intentionally NOT varied per theme: IPAdapter's identity
# conditioning fights pose changes hard enough that it isn't worth chasing
# per-theme poses — see conversation history. Only setting/costume/motif
# vary; the base pose phrase stays fixed in PROMPT_TEMPLATE.
#
# Keys are matched case-insensitively against whatever the frontend sends
# as research_field (see app.py / index.html theme dropdown).
# ---------------------------------------------------------------------------

THEMES = {
    "in the lab": {
        "setting": "bright high-tech laboratory, glass beakers and glowing vials in the background",
        "costume": "white lab coat, safety goggles pushed up on forehead",
        "motif":   "crystalline molecular bonds glowing softly, faint chemical reaction sparkles",
    },
    "computing": {
        "setting": "dark server room lit by screen glow, floating holographic data panels",
        "costume": "casual modern tech-wear, hoodie or smart-casual jacket, no lab coat",
        "motif":   "cascading streams of glowing code, translucent data structures suspended in air",
    },
    "health": {
        "setting": "clean clinical space, soft cool lighting, calm atmosphere",
        "costume": "medical scrubs or white coat with stethoscope",
        "motif":   "gentle pulse waveforms glowing blue, soft radiant caduceus light accents",
    },
    "engineering": {
        "setting": "industrial workshop or construction site, structural beams and scaffolding",
        "costume": "hard hat, tool belt, rolled-up sleeves",
        "motif":   "blueprint grid lines dissolving into golden light, glowing gear mechanisms",
    },
    "star-gazing": {
        "setting": "open night sky observatory, telescope silhouette nearby",
        "costume": "warm jacket, casual outdoor wear, no lab coat",
        "motif":   "deep space nebula clouds, star field with comet trails, galactic spiral arms glowing",
    },
    "bookworm": {
        "setting": "cozy towering library, warm lamplight, stacks of books",
        "costume": "knit sweater or cardigan, glasses, holding an open book",
        "motif":   "softly glowing dust motes in lamplight, faint golden page-light spilling from books",
    },
    "performer": {
        "setting": "dramatic stage with spotlight, dark background, faint audience silhouette",
        "costume": "stylish stage outfit, microphone or instrument nearby",
        "motif":   "warm spotlight beams, soft glowing stage haze, dynamic light trails",
    },
    "villain": {
        "setting": "dark dramatic lair, storm clouds or shadowy architecture",
        "costume": "sleek dark coat, dramatic silhouette",
        "motif":   "deep purple and red ambient glow, electric energy crackling at the edges",
    },
    "animal lover": {
        "setting": "sunlit meadow or cozy nature scene",
        "costume": "casual outdoor wear",
        "motif":   "a friendly Australian ibis standing calmly nearby (a wombat or possum is also acceptable), soft natural lighting, gentle warm tones",
    },
    # Fallback if the frontend ever sends an unrecognised value. Never errors —
    # generic celebratory motif, no specific setting/costume claims.
    "other": {
        "setting": "an inspiring, light-filled space",
        "costume": "smart casual outfit",
        "motif":   "radiant abstract energy patterns, swirling light particles, dramatic volumetric light rays",
    },
}

# Tag-style prompt (Animagine/SDXL convention), not instruction-style.
# {setting}/{costume}/{motif} are substituted per guest's theme choice.
# Pose phrase ("dynamic confident pose") stays fixed across all themes —
# IPAdapter overrides pose attempts regardless of what's requested here,
# so this is kept simple rather than chasing a per-theme pose that won't render.
PROMPT_TEMPLATE = (
    "masterpiece, best quality, year 2024, crisp anime style, 1person, solo, "
    "detailed face, identity-preserving, {costume}, dynamic confident pose, "
    "{setting}, {motif}, vibrant colors, realistic skintone"
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
            "Export the tuned animagine_workflow.json graph in API format "
            "(ComfyUI menu: Workflow > Export (API Format)) and save it as "
            f"{WORKFLOW_PATH.name} next to pipeline.py."
        )
    with open(WORKFLOW_PATH) as f:
        workflow = json.load(f)

    # Fail fast and loud if the expected placeholders aren't present —
    # better to catch a bad export now than at the kiosk mid-event.
    found = {PLACEHOLDER_IMAGE: False, PLACEHOLDER_PROMPT: False, PLACEHOLDER_PREFIX: False}
    has_ksampler = False
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") == "KSampler":
            has_ksampler = True
        for v in node.get("inputs", {}).values():
            # inputs can be plain values or [node_id, output_index] link
            # references — only strings are eligible placeholder matches.
            if isinstance(v, str) and v in found:
                found[v] = True

    missing = [k for k, present in found.items() if not present]
    if missing:
        raise RuntimeError(
            f"Workflow template is missing expected placeholder(s): {missing}. "
            f"Check the LoadImage, CLIPTextEncode, and SaveImage nodes in {WORKFLOW_PATH.name}."
        )
    if not has_ksampler:
        raise RuntimeError(f"No KSampler node found in {WORKFLOW_PATH.name}.")

    _state["workflow"] = workflow

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
    Generate an anime-style portrait with Animagine XL 3.1 + IPAdapter
    face conditioning.

    Args:
        photo_path:     Path to webcam capture JPEG (deleted after upload).
        research_field: Guest's chosen theme key (selects setting/costume/
                         motif bundle from THEMES). Falls back to a generic
                         theme if unrecognised — never errors on this field.
        save_id:        UUID stem for output filename.

    Returns:
        Path to generated image in OUTPUT_DIR.
    """
    if not _state["ready"]:
        raise KioskPipelineError("Pipeline not ready. Please wait and try again.")
    if not photo_path.exists():
        raise KioskPipelineError("Photo missing. Please retake your photo.")

    # Build prompt from the chosen theme's (setting, costume, motif) bundle
    theme  = THEMES.get(research_field.strip().lower(), THEMES["other"])
    prompt = PROMPT_TEMPLATE.format(
        setting=theme["setting"],
        costume=theme["costume"],
        motif=theme["motif"],
    )
    logger.info("[%s] Theme: %s | Prompt: %.120s...", save_id, research_field, prompt)

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

    # Substitute placeholders into a fresh copy of the workflow template
    workflow = json.loads(json.dumps(_state["workflow"]))
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {})
        for k, v in inputs.items():
            if v == PLACEHOLDER_IMAGE:
                inputs[k] = uploaded_name
            elif v == PLACEHOLDER_PROMPT:
                inputs[k] = prompt
            elif v == PLACEHOLDER_PREFIX:
                inputs[k] = save_id
        # Always randomise the seed per guest. The graph itself is kept on
        # a fixed seed for repeatable A/B tuning in the ComfyUI UI — this
        # is the one place that fixed value must NOT survive into a live run.
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
