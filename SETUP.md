# AI Portrait Kiosk — Supercomputer Setup Guide

Target: OpenOnDemand desktop session on an A100 40GB shard (or L40/L40S).
Everything runs on localhost — no tunnelling needed.

---

## 0. Before you start

You need:
- HuggingFace account with FLUX.1-dev license accepted at:
  https://huggingface.co/black-forest-labs/FLUX.1-dev
- HF token (Settings → Access Tokens → New token, "read" scope is enough)
- Internet access from the compute node (check with your HPC team if unsure)

Set your token now so all `huggingface-cli` calls below work:

```bash
export HF_TOKEN=hf_yourtoken
huggingface-cli login --token $HF_TOKEN
```

---

## 1. ComfyUI — update or install

If ComfyUI is already installed, pull latest and update dependencies:

```bash
cd ~/ComfyUI          # or wherever it lives on your system
git pull
pip install -r requirements.txt --upgrade
```

If it is not installed:

```bash
cd ~
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
pip install -r requirements.txt
```

Confirm it starts before going further:

```bash
python main.py --listen 127.0.0.1 --port 8188 &
# Should print: "To see the GUI go to: http://127.0.0.1:8188"
# Ctrl+C or kill %1 to stop it again
```

---

## 2. Custom nodes

```bash
cd ~/ComfyUI/custom_nodes
```

### 2a. PuLID for Flux (face identity — optional, not used in current pipeline)
Not needed for the LTX-Video i2v pipeline. Skip unless you later add
a still-image Flux step.

### 2b. ComfyUI-VideoHelperSuite (VHS) — required for video output
```bash
git clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git
cd ComfyUI-VideoHelperSuite
pip install -r requirements.txt
cd ..
```

### 2c. ComfyUI-LTXVideo — required for LTX-Video nodes
```bash
git clone https://github.com/Lightricks/ComfyUI-LTXVideo.git
cd ComfyUI-LTXVideo
pip install -r requirements.txt
cd ..
```

---

## 3. Models

All models go into subdirectories of `~/ComfyUI/models/`.

### 3a. LTX-Video 2.3 checkpoint
```bash
mkdir -p ~/ComfyUI/models/checkpoints
huggingface-cli download \
    Lightricks/LTX-Video \
    ltx-video-2b-v0.9.5.safetensors \
    --local-dir ~/ComfyUI/models/checkpoints
```

### 3b. LTX-Video text encoder (T5)
```bash
mkdir -p ~/ComfyUI/models/clip
huggingface-cli download \
    Lightricks/LTX-Video \
    text_encoder/model.safetensors \
    text_encoder/config.json \
    tokenizer/tokenizer.json \
    tokenizer/special_tokens_map.json \
    tokenizer/tokenizer_config.json \
    --local-dir ~/ComfyUI/models/clip/t5-ltx
```

### 3c. Fantasy Anime LoRA
```bash
mkdir -p ~/ComfyUI/models/loras
huggingface-cli download \
    vrgamedevgirl84/LTX_2.3_Fantasy_Anime_Style_LoRa \
    --local-dir ~/ComfyUI/models/loras
```

Check the filename that downloads — update `ltxvideo_workflow.json`
`"lora_name"` field to match exactly if it differs from:
`LTX_2.3_Fantasy_Anime_Style_LoRa.safetensors`

### 3d. Verify model directory structure
```
~/ComfyUI/models/
├── checkpoints/
│   └── ltx-video-2b-v0.9.5.safetensors
├── clip/
│   └── t5-ltx/
│       ├── model.safetensors
│       ├── config.json
│       └── tokenizer/
└── loras/
    └── LTX_2.3_Fantasy_Anime_Style_LoRa.safetensors
```

---

## 4. Kiosk app — install dependencies

```bash
cd ~/kiosk_app          # or wherever you put the kiosk files
python -m venv venv
source venv/bin/activate
pip install flask
```

That's it. The new pipeline.py has no diffusers/torch dependencies —
it just talks HTTP to ComfyUI.

---

## 5. Update pipeline.py config

Open `pipeline.py` and set `COMFYUI_OUTPUT_DIR` to wherever ComfyUI
saves its outputs on your system:

```python
COMFYUI_OUTPUT_DIR = Path.home() / "ComfyUI" / "output"
```

If ComfyUI is installed system-wide rather than in your home directory,
adjust accordingly. You can confirm the path by running ComfyUI once
and noting where it saves a test image.

---

## 6. Place kiosk files

Your kiosk directory should look like this:

```
~/kiosk_app/
├── app.py
├── pipeline.py
├── ltxvideo_workflow.json
├── send_emails.py
├── templates/
│   └── index.html
├── outputs/          (created automatically)
├── temp/             (created automatically)
└── sent/             (created by send_emails.py)
```

---

## 7. On the day — startup sequence

Open two terminals in your OpenOnDemand desktop session.

**Terminal 1 — ComfyUI:**
```bash
cd ~/ComfyUI
python main.py --listen 127.0.0.1 --port 8188
```
Wait until you see: `To see the GUI go to: http://127.0.0.1:8188`
Model loading takes 30–60 seconds on first run.

**Terminal 2 — Kiosk app:**
```bash
cd ~/kiosk_app
source venv/bin/activate
python app.py
```
Wait until you see: `Starting kiosk server on http://127.0.0.1:5000`

**Browser:**
Open http://127.0.0.1:5000 and run a test generation with your own
photo before guests arrive. Confirm:
- Camera access granted
- Photo uploads successfully
- Generation completes and video plays back
- outputs/ contains a .mp4 and matching .txt

For a kiosk feel, open Chrome in fullscreen (F11) pointed at
http://127.0.0.1:5000

---

## 8. After the event — send emails

```bash
cd ~/kiosk_app
source venv/bin/activate

# Dry run first — prints what would be sent without sending
python send_emails.py --dry-run

# Review the list, then actually send with a 5-second delay between emails
SMTP_HOST=smtp.your-university.edu.au \
SMTP_PORT=587 \
SMTP_USER=you@university.edu.au \
SMTP_PASSWORD=yourpassword \
SMTP_FROM="Research Portrait Kiosk <you@university.edu.au>" \
python send_emails.py --delay 5
```

Adjust `--delay` to match your SMTP rate limit.
Processed pairs are moved to `sent/` automatically so re-runs are safe.

---

## 9. Troubleshooting

**"Cannot reach ComfyUI at http://localhost:8188"**
ComfyUI isn't running or didn't start cleanly. Check Terminal 1 for errors.
Most common cause: missing model file. ComfyUI will print the missing path.

**Video generates but is black / corrupted**
Usually a codec issue with VHS. Try changing `format` in the workflow JSON
from `video/h264-mp4` to `image/gif` temporarily to confirm the frames
themselves are good, then troubleshoot ffmpeg availability.

**"Could not find output video in ComfyUI history"**
Check that `COMFYUI_OUTPUT_DIR` in pipeline.py points to the right place.
Also check that VHS_VideoCombine node has `save_output: true`.

**Generation takes more than 3 minutes**
Check GPU allocation — confirm you actually have the 40GB shard assigned
and that ComfyUI is using it. Run `nvidia-smi` in a third terminal.

**Camera permission denied in browser**
Chrome requires HTTPS or localhost for camera access. Since you're on
127.0.0.1 this should work; if using a hostname instead, either switch
to the IP or add the hostname to Chrome's insecure origins allowlist:
chrome://flags/#unsafely-treat-insecure-origin-as-secure
