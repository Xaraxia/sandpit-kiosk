"""
send_emails.py
--------------
Post-event script. Walks the outputs/ directory, matches UUID pairs
(.png + .txt), and emails each guest their generated portrait.

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
from email.mime.image import MIMEImage
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

EMAIL_SUBJECT = "Your Anime Yourself portrait from today's event!"

EMAIL_BODY_TEMPLATE = """\
Hi {name},

Thanks for stopping by Anime Yourself at the AI Sandpit today!

Your personalised portrait is attached — rendered in cinematic anime
style with a "{field}" theme.

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


def send_one(name: str, email: str, field: str, image_path: Path, dry_run: bool) -> bool:
    """Send a single email with the portrait attached. Returns True on success."""
    body = EMAIL_BODY_TEMPLATE.format(name=name, field=field)

    msg = MIMEMultipart()
    msg["From"]    = SMTP_FROM
    msg["To"]      = email
    msg["Subject"] = EMAIL_SUBJECT
    msg.attach(MIMEText(body, "plain"))

    # Attach portrait image
    with open(image_path, "rb") as f:
        img_part = MIMEImage(f.read(), _subtype="png")
    img_part.add_header(
        "Content-Disposition",
        f'attachment; filename="{image_path.name}"'
    )
    msg.attach(img_part)

    if dry_run:
        logger.info("[DRY RUN] Would send to %s <%s> (%s)", name, email, image_path.name)
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
        image_path = outputs_dir / f"{save_id}.png"

        if not image_path.exists():
            logger.warning("No image found for %s — skipping.", save_id)
            continue

        try:
            guest = parse_txt(txt_path)
        except Exception as e:
            logger.error("Could not parse %s: %s — skipping.", txt_path.name, e)
            error_count += 1
            continue

        name  = guest.get("name",  "Guest")
        email = guest.get("email", "")
        field = guest.get("field", "theme")

        if not email:
            logger.warning("No email in %s — skipping.", txt_path.name)
            error_count += 1
            continue

        success = send_one(name, email, field, image_path, args.dry_run)

        if success:
            sent_count += 1
            # Archive both files so re-runs don't re-send
            if not args.dry_run:
                shutil.move(str(txt_path),   sent_dir / txt_path.name)
                shutil.move(str(image_path), sent_dir / image_path.name)
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
