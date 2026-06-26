# Anime Yourself

A kiosk for Research & Innovation Week's AI Sandpit. Guests take a photo,
pick a theme, and get back an identity-preserving anime portrait of
themselves a minute or so later — sent to their email after the event.

**Pipeline:** Animagine XL 3.1 (SDXL) + IPAdapter, running on ComfyUI.
Still images only — no video.

---

## Quick start

Full install instructions, including GPU/VRAM requirements, are in
**[SETUP.md](./SETUP.md)**. Start there if you're setting this up on a
new machine.

Short version, once everything's installed:

```bash
# Terminal 1
cd ~/ComfyUI && source venv/bin/activate
python main.py --listen 127.0.0.1 --port 8188

# Terminal 2
cd ~/kiosk_app && source venv/bin/activate
python app.py
```

Then open `http://127.0.0.1:5000`.

---

## How it works

1. Guest takes a photo at the kiosk (browser webcam capture, cropped
   client-side to match the on-screen framing).
2. Guest picks a **theme** — these are visual aesthetics, not academic
   disciplines, chosen specifically so every option renders distinctly
   from the others:

   | Theme | Vibe |
   |---|---|
   | In the Lab | lab coat, glassware, chemical glow |
   | Research Computing | dark server room, holographic code |
   | Health | clinical, calm, soft blue pulse light |
   | Engineering | hard hat, workshop, blueprint glow |
   | Star-gazing | observatory, nebula, comet trails |
   | Bookworm | cozy library, warm lamplight |
   | Performer | stage spotlight, dramatic haze |
   | Villain | dark lair, purple/red ambient glow |
   | Animal Lover | sunny meadow, a friendly ibis nearby |

3. `app.py` hands the photo + theme to `pipeline.py`, which fills in the
   ComfyUI workflow template and queues the generation job over HTTP.
4. ComfyUI runs Animagine XL 3.1 with IPAdapter conditioning the face
   against the guest's photo, and saves a `.png`.
5. The guest sees their portrait immediately. Name/email are stashed in a
   `.txt` sidecar file for post-event sending.
6. After the event, `send_emails.py` walks `outputs/`, matches each
   `.png` + `.txt` pair, and emails the portrait to each guest.

### Why pose and clothing don't change per theme

IPAdapter's identity conditioning is strong enough to preserve facial
likeness, but it also drags the *pose and clothing from the source photo*
through regardless of what the prompt asks for. Themes only vary
**setting, costume/prop, and background motif** in the prompt
(`pipeline.py`'s `THEMES` dict) — pose is left alone deliberately, because
fighting IPAdapter on it doesn't win and just produces a worse result.
This is why the capture screen tells guests to strike a pose themselves
rather than promising the system will pose them.

---

## File layout

```
~/kiosk_app/
├── app.py                       Flask server — routes, session handling
├── pipeline.py                  Talks to ComfyUI, builds prompts per theme
├── animagine_workflow_api.json  ComfyUI workflow template (API format)
├── send_emails.py                Post-event script — sends portraits
├── templates/
│   └── index.html               Kiosk frontend (camera, theme picker, result)
├── outputs/                      Generated .png + .txt pairs (created at runtime)
├── temp/                         In-progress captures (created at runtime)
└── sent/                         Archived after send_emails.py runs (created at runtime)
```

---

## Tuning the actual ComfyUI graph

If portrait quality or identity likeness needs adjusting, do it in the
ComfyUI UI directly against `animagine_workflow_api.json`'s source graph
— not by editing `pipeline.py`'s Python code. Current tuned settings
(sampler, CFG, IPAdapter weight, etc.) reflect a fair amount of trial and
error; see the graph itself for the live values. After changing anything,
re-export in **API format** (Workflow → Export (API Format)) and replace
`animagine_workflow_api.json` — `pipeline.py` validates on startup that
the required placeholders are present, so a bad export fails loudly
rather than silently at the kiosk.

## Adding or changing themes

Themes live in two places that must stay in sync:
- `pipeline.py`'s `THEMES` dict (setting/costume/motif bundle, lowercase key)
- `templates/index.html`'s `<select id="input-field">` dropdown (same
  lowercase key as the `value`, display label as the visible text)

`app.py`'s `RESEARCH_FIELDS` list is a separate, human-readable copy used
only by the `/api/fields` endpoint — update it too for consistency, but
nothing breaks if you forget, since the frontend doesn't read from it.

---

## After the event

```bash
cd ~/kiosk_app && source venv/bin/activate

python send_emails.py --dry-run     # check first, sends nothing

SMTP_HOST=... SMTP_USER=... SMTP_PASSWORD=... SMTP_FROM="Anime Yourself <...>" \
python send_emails.py --delay 5
```

See SETUP.md Section 8 for full details.
