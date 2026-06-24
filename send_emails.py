"""
send_emails.py
--------------
Post-event script. Walks the outputs/ directory, matches UUID pairs
(.mp4 + .txt), and emails each guest their generated video.

Run AFTER the event, not during it.

Usage:
    python send_emails.py [--dry-run] [--delay 5]

Options:
    --dry-run       Print what would be sent without actually sending.
    --delay N       Seconds to wait between sends (default: 5).
                    Tune this to your SMTP server's rate limit.
    --outputs DIR   Path to outputs directory (default: ./outputs).
    --sent DIR      Path to archive sent files into (default: ./sent).
                    Processed pairs are moved here so re-runs don't re-send.

SMTP config:
    Set these environment variables before running, or edit the SMTP_*
    constants below:

        SMTP_HOST       e.g. smtp.your-university.edu.au
        SMTP_PORT       e.g. 587
        SMTP_USER       your sending address
        SMTP_PASSWORD   your SMTP password (or app password)
        SMTP_FROM       display name + address, e.g. "AI Kiosk <you@uni.edu.au>"
"""

import argparse
import logging
import os
import shutil
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("send_emails")

# ---------------------------------------------------------------------------
# SMTP config — override with environment variables
# ---------------------------------------------------------------------------

SMTP_HOST     = os.environ.get("SMTP_HOST",     "smtp.your-university.edu.au")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER",     "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM     = os.environ.get("SMTP_FROM",     "AI Kiosk <kiosk@your-university.edu.au>")

EMAIL_SUBJECT = "Your AI portrait video from today's event!"

EMAIL_BODY_TEMPLATE = """\
Hi {name},

Thanks for stopping by the AI portrait kiosk today!

Your personalised video is attached — a {field} researcher rendered in
cinematic fantasy anime style, complete with dramatic {field} background effects.

Feel free to share it however you like.

Cheers,
The Event Team
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_txt(txt_path: Path) -> dict:
    """Parse a key=value .txt file into a dict."""
    result = {}
    for line in txt_path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def send_one(name: str, email: str, field: str, video_path: Path, dry_run: bool) -> bool:
    """Send a single email with the video attached. Returns True on success."""
    body = EMAIL_BODY_TEMPLATE.format(name=name, field=field)

    msg = MIMEMultipart()
    msg["From"]    = SMTP_FROM
    msg["To"]      = email
    msg["Subject"] = EMAIL_SUBJECT
    msg.attach(MIMEText(body, "plain"))

    # Attach video
    with open(video_path, "rb") as f:
        part = MIMEBase("video", "mp4")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f'attachment; filename="{video_path.name}"'
    )
    msg.attach(part)

    if dry_run:
        logger.info("[DRY RUN] Would send to %s <%s> (%s)", name, email, video_path.name)
        return True

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            if SMTP_USER and SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [email], msg.as_string())
        logger.info("Sent to %s <%s>", name, email)
        return True
    except Exception as e:
        logger.error("Failed to send to %s <%s>: %s", name, email, e)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Post-event email sender.")
    parser.add_argument("--dry-run",  action="store_true", help="Print without sending.")
    parser.add_argument("--delay",    type=float, default=5.0,
                        help="Seconds between sends (default: 5).")
    parser.add_argument("--outputs",  default="./outputs",
                        help="Path to outputs directory.")
    parser.add_argument("--sent",     default="./sent",
                        help="Directory to archive processed pairs into.")
    args = parser.parse_args()

    outputs_dir = Path(args.outputs)
    sent_dir    = Path(args.sent)
    sent_dir.mkdir(exist_ok=True)

    # Find all .txt files in outputs — each represents one guest
    txt_files = sorted(outputs_dir.glob("*.txt"))
    if not txt_files:
        logger.info("No guest records found in %s. Nothing to send.", outputs_dir)
        return

    logger.info("Found %d guest record(s).", len(txt_files))
    if args.dry_run:
        logger.info("DRY RUN — no emails will actually be sent.")

    sent_count  = 0
    error_count = 0

    for txt_path in txt_files:
        save_id    = txt_path.stem
        video_path = outputs_dir / f"{save_id}.mp4"

        if not video_path.exists():
            logger.warning("No video found for %s — skipping.", save_id)
            continue

        try:
            guest = parse_txt(txt_path)
        except Exception as e:
            logger.error("Could not parse %s: %s — skipping.", txt_path.name, e)
            error_count += 1
            continue

        name  = guest.get("name",  "Guest")
        email = guest.get("email", "")
        field = guest.get("field", "research")

        if not email:
            logger.warning("No email in %s — skipping.", txt_path.name)
            error_count += 1
            continue

        success = send_one(name, email, field, video_path, args.dry_run)

        if success:
            sent_count += 1
            # Archive both files so re-runs don't re-send
            if not args.dry_run:
                shutil.move(str(txt_path),   sent_dir / txt_path.name)
                shutil.move(str(video_path), sent_dir / video_path.name)
        else:
            error_count += 1

        if args.delay > 0 and txt_path != txt_files[-1]:
            time.sleep(args.delay)

    logger.info(
        "Done. Sent: %d  Errors: %d  Total: %d",
        sent_count, error_count, len(txt_files)
    )


if __name__ == "__main__":
    main()
