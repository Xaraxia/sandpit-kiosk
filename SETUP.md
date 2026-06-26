# AI Portrait Kiosk — Setup Guide

Pipeline: **Animagine XL 3.1 + IPAdapter** (still image, identity-preserving
anime portrait generation). No video, no Flux, no LTX-Video — if you have
an older SETUP.md or kiosk_app checkout referencing those, discard it.

Target: a single A16 vGPU shard (the slice presented to Linux, not the
full physical card). Everything runs on localhost — no tunnelling needed
once ComfyUI and the kiosk app are both running on the same machine.

---

## 0. Before you start

You need:
- Internet access from the machine running ComfyUI, at least for initial
  model downloads (check with IT/HPC support if you're on a managed system
  and unsure)
- ~10–15GB free disk space for the checkpoint, IPAdapter weights, and
  CLIP-Vision encoder combined
- A webcam-equipped browser session (Chrome recommended) for the kiosk
  frontend

**Minimum requirement: 12GB VRAM.** A full A16 vGPU shard carries 16GB,
which comfortably covers Animagine XL 3.1 + IPAdapter + the CLIP-Vision
encoder + ComfyUI's own overhead. If you're assigned a smaller fractional
slice of an A16 (or any other GPU) below 12GB, this pipeline is not a
good fit for that hardware — don't try to make it work via aggressive
offloading or quantization tricks for a live kiosk; the speed and
stability cost isn't worth it for an event you can't babysit
indefinitely. Request a larger shard instead.

Confirm what you've actually been allocated before the event, don't
assume:

```bash
nvidia-smi --query-gpu=memory.total,memory.used,memory.free --format=csv
```

---

## 1. ComfyUI — install

**Python version:** 3.9 is the hard floor (below this, ComfyUI-Manager and
several custom nodes won't install at all), but for actual day-to-day
compatibility with the custom node ecosystem — including IPAdapter, used
here — **3.12 is the safer practical target**. 3.13 is the current
official recommendation and generally fine, but a meaningfully smaller
slice of custom nodes have reported issues on it than on 3.12. Check what
you've got:

```bash
python3 --version
```

**Use a venv.** Don't install ComfyUI's dependencies into system Python —
custom nodes pull in a lot of packages with specific version pins, and
keeping it isolated avoids fighting version conflicts with anything else
on the machine.

```bash
cd ~
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
python3 -m venv venv
source venv/bin/activate
```

You'll need to run `source ~/ComfyUI/venv/bin/activate` again in any new
terminal session before starting ComfyUI (Section 7 assumes this).

If ComfyUI is already installed on this machine, update it instead of
cloning fresh — still inside its venv:

```bash
cd ~/ComfyUI
source venv/bin/activate
git pull
pip install -r requirements.txt --upgrade
```

If it's not installed yet, continue from the venv you just created above:

```bash
pip install -r requirements.txt
```

Confirm it starts before going further:

```bash
python main.py --listen 127.0.0.1 --port 8188 &
# Should print: "To see the GUI go to: http://127.0.0.1:8188"
# Ctrl+C or `kill %1` to stop it again once confirmed
```

---

## 2. Custom nodes

```bash
cd ~/ComfyUI/custom_nodes
```

### 2a. ComfyUI_IPAdapter_plus — required, this is the identity-conditioning node

```bash
git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus.git
cd ..
```

That's the whole install step — this node pack has no `requirements.txt`;
dropping the cloned folder into `custom_nodes/` is sufficient. Restart
ComfyUI after cloning so it picks up the new node.

This pipeline uses the **`PLUS FACE (portraits)`** preset via the
`IPAdapterUnifiedLoader` node — not a FaceID model. That distinction
matters for setup: PLUS FACE runs on CLIP-Vision embeddings only and does
**not** require InsightFace as a system dependency. If you ever see an
error mentioning `insightface` or `antelopev2`, you've accidentally
selected a FaceID-family preset somewhere — switch back to `PLUS FACE
(portraits)` rather than going down the InsightFace install path, which
this setup doesn't need.

The Unified Loader auto-downloads its required IPAdapter weights and
CLIP-Vision encoder on first use, into:
- `~/ComfyUI/models/ipadapter/`
- `~/ComfyUI/models/clip_vision/`

No manual download step needed for these two — just make sure the node
pack above is installed, and the first real generation run will pull what
it needs. This does mean your *first* test generation will be slower than
normal (model download + load) — don't mistake that for a broken
pipeline.

---

## 3. Models

### 3a. Animagine XL 3.1 checkpoint

```bash
mkdir -p ~/ComfyUI/models/checkpoints
huggingface-cli download \
    cagliostrolab/animagine-xl-3.1 \
    animagine-xl-3.1.safetensors \
    --local-dir ~/ComfyUI/models/checkpoints
```

No HuggingFace token or license acceptance is required for this model —
unlike FLUX.1-dev (not used in this pipeline), Animagine XL is openly
licensed.

### 3b. Verify model directory structure

After the checkpoint download and one successful generation (which
triggers the IPAdapter auto-downloads from Section 2a), you should have:

```
~/ComfyUI/models/
├── checkpoints/
│   └── animagine-xl-3.1.safetensors
├── ipadapter/
│   └── (auto-downloaded on first IPAdapter use)
└── clip_vision/
    └── (auto-downloaded on first IPAdapter use)
```

### 3c. If you're under the 12GB minimum

`--lowvram` exists as a ComfyUI startup flag that offloads more
aggressively between GPU and system RAM, but treat it as a stopgap for
testing only, not a real fix for running the kiosk live:

```bash
python main.py --listen 127.0.0.1 --port 8188 --lowvram
```

If you find yourself needing this for the actual event, that's a sign the
shard allocation is wrong for this workload — raise it with whoever
assigned the shard rather than running the kiosk on a setting that trades
away speed and stability for headroom you shouldn't need to be fighting
for in the first place.

---

## 4. Kiosk app — install dependencies

The kiosk app needs its own venv too, same reasoning as Section 1 — keep
dependencies isolated. **It's fine for this to be the same venv as
ComfyUI's, or a separate one** — `pipeline.py` only needs Flask and talks
to ComfyUI purely over HTTP, so there's no dependency conflict either way.
Pick whichever is less hassle for how you're managing the machine.

**Separate venv** (if ComfyUI and the kiosk app live in different
directories and you'd rather not think about shared state):

```bash
cd ~/kiosk_app          # or wherever you've placed the kiosk files
python3 -m venv venv
source venv/bin/activate
pip install flask
```

**Same venv as ComfyUI** (if you'd rather manage one environment):

```bash
source ~/ComfyUI/venv/bin/activate
pip install flask
```

Either way, remember to activate the correct venv in Terminal 2 before
running `python app.py` in Section 7.

That's the only dependency the kiosk app needs. `pipeline.py` talks to
ComfyUI purely over HTTP — no torch, diffusers, or model libraries needed
in the kiosk app's own environment regardless of which venv you use.

---

## 5. Configure pipeline.py

Open `pipeline.py` and confirm `COMFYUI_OUTPUT_DIR` points to where
ComfyUI actually saves images on this machine:

```python
COMFYUI_OUTPUT_DIR = Path.home() / "ComfyUI" / "output"
```

This is the default ComfyUI output location. If ComfyUI is installed
somewhere other than your home directory, update this path to match —
run ComfyUI once, generate a test image directly in its own web UI
(`http://127.0.0.1:8188`), and check where the file actually lands if
you're unsure.

---

## 6. Place kiosk files

Your kiosk directory should look like this:

```
~/kiosk_app/
├── app.py
├── pipeline.py
├── animagine_workflow_api.json
├── send_emails.py
├── templates/
│   └── index.html
├── outputs/          (created automatically on first run)
├── temp/             (created automatically on first run)
└── sent/             (created automatically by send_emails.py)
```

`animagine_workflow_api.json` must sit directly next to `pipeline.py` —
it's the API-format export of the tuned ComfyUI graph and is loaded by
path at startup. `index.html` belongs inside `templates/`, not next to
`app.py` — Flask's `render_template()` looks there by default.

---

## 7. On the day — startup sequence

Two terminals.

**Terminal 1 — ComfyUI:**
```bash
cd ~/ComfyUI
source venv/bin/activate
python main.py --listen 127.0.0.1 --port 8188
```
Wait for: `To see the GUI go to: http://127.0.0.1:8188`
Model loading takes roughly 30–60 seconds on first run after a restart.

**Terminal 2 — Kiosk app:**
```bash
cd ~/kiosk_app
source venv/bin/activate    # or ~/ComfyUI/venv/bin/activate if you used the same venv
python app.py
```
Wait for: `Starting kiosk server on http://127.0.0.1:5000`

If this fails immediately with a workflow/placeholder error, that's
`pipeline.py`'s own startup validation catching a bad
`animagine_workflow_api.json` export before the event starts — read the
error message, it names exactly which placeholder or node is missing.

**Browser:**
Open `http://127.0.0.1:5000` and run a full test generation with your own
photo before guests arrive. Confirm:
- Camera access granted, live preview shows correctly
- Photo captures and uploads without error
- A theme selection completes generation within roughly 30–60 seconds
- `outputs/` contains a matching `.png` and `.txt` pair for that run
- The result image actually resembles you and matches the chosen theme

For a kiosk feel, open the browser in fullscreen (F11) pointed at
`http://127.0.0.1:5000`.

---

## 8. After the event — send emails

```bash
cd ~/kiosk_app
source venv/bin/activate    # or ~/ComfyUI/venv/bin/activate if you used the same venv

# Dry run first — prints what would be sent without sending anything
python send_emails.py --dry-run

# Review the dry-run output, then send for real with a delay between sends
SMTP_HOST=smtp.your-university.edu.au \
SMTP_PORT=587 \
SMTP_USER=you@university.edu.au \
SMTP_PASSWORD=yourpassword \
SMTP_FROM="Anime Yourself <you@university.edu.au>" \
python send_emails.py --delay 5
```

Adjust `--delay` to your SMTP server's actual rate limit if sends start
failing partway through a batch. Successfully sent pairs are moved into
`sent/` automatically, so re-running the script is safe and won't
double-send.

---

## 9. Troubleshooting

**"Cannot reach ComfyUI at http://localhost:8188"**
ComfyUI isn't running, or didn't finish starting. Check Terminal 1 for
errors — most commonly a missing model file, which ComfyUI will name
explicitly in its own startup log.

**"Workflow file not found" / "missing expected placeholder(s)"**
This is `pipeline.py`'s own startup check catching a problem with
`animagine_workflow_api.json` before you'd discover it mid-event instead.
Re-export the workflow from ComfyUI's UI in API format (Workflow → Export
(API Format)) and confirm the three placeholder tokens
(`__INPUT_IMAGE_PATH__`, `__POSITIVE_PROMPT__`, `__OUTPUT_FILENAME_PREFIX__`)
are present in the LoadImage, CLIPTextEncode, and SaveImage nodes
respectively.

**Generation completes but identity looks wrong / doesn't resemble the
guest**
This is a graph-tuning problem, not a setup problem — work it through in
the ComfyUI UI directly (IPAdapter weight, sampler/scheduler, denoise),
not by editing this setup or the Flask app.

**CUDA out of memory**
See Section 3c. If you're hitting this on the actual kiosk machine (not
just while testing), the shard doesn't meet the 12GB minimum for
SDXL+IPAdapter — that's a shard allocation problem to escalate, not
something to solve with `--lowvram` on the day.

**Generation takes more than ~2 minutes**
Check actual GPU utilisation with `nvidia-smi` in a third terminal while
a generation is running — confirm the workload is actually landing on the
GPU and isn't silently falling back to CPU.

**Camera permission denied in browser**
Browsers require HTTPS or `localhost`/`127.0.0.1` for camera access. Since
this setup runs entirely on `127.0.0.1`, this should work out of the box;
if you're instead pointing the browser at a hostname, either switch to the
IP address or add the hostname to your browser's insecure-origin allowlist
for local testing only.
