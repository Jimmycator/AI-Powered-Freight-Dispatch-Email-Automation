"""
Pull new dispatch emails from Gmail (or a Google Workspace mailbox) with the Gmail API
and hand them to dispatch_extractor.py.

Auth: OAuth "desktop app" flow with the read-only Gmail scope. The first run opens a
browser so you can sign in and approve access, then saves token.json; later runs reuse it.
See the README for the one-time Google Cloud setup (about 10 minutes).

Usage:
    python gmail_ingest.py --since-hours 24                  # first run: last 24h into incoming/batch_*.csv
    python gmail_ingest.py --process                         # fetch new mail since last run, run the extractor
    python gmail_ingest.py --query "label:dispatch" --max 200 --process
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path("credentials.json")  # downloaded from Google Cloud Console
TOKEN_FILE = Path("token.json")              # created after your first sign-in
STATE_FILE = Path(".gmail_state.json")
INCOMING_DIR = Path("incoming")
LOCAL_TZ = ZoneInfo("America/Chicago")       # keep in sync with the timezone named in the extractor prompt
MAX_LIST = 5000                              # safety cap on how many message IDs one run will list
RETRIES = 5


def get_service():
    """Return an authenticated Gmail API client (signs in through the browser on first use)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise SystemExit(
                    "credentials.json not found. Create an OAuth client (Desktop app) in Google Cloud "
                    "Console and save the downloaded file as credentials.json (see README)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_message_ids(service, query: str) -> list[str]:
    """All message IDs matching the query (Gmail returns them newest first)."""
    ids: list[str] = []
    page_token = None
    while len(ids) < MAX_LIST:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=100, pageToken=page_token)
            .execute(num_retries=RETRIES)
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_new_messages(service, query: str, last_ms: int, max_messages: int) -> list[dict]:
    """Full messages newer than last_ms (epoch milliseconds), oldest first."""
    # Gmail's after: filter works in whole seconds, so back off one second and filter exactly below.
    full_query = f"{query} after:{last_ms // 1000 - 1}"
    ids = list_message_ids(service, full_query)
    ids.reverse()  # oldest first, so a capped run never skips older mail
    messages = []
    for message_id in ids[:max_messages]:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute(num_retries=RETRIES)
        )
        if int(msg["internalDate"]) > last_ms:
            messages.append(msg)
    messages.sort(key=lambda m: int(m["internalDate"]))
    return messages


def decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def extract_body(payload: dict) -> str:
    """Plain-text body if there is one, otherwise the HTML (the extractor strips the tags).
    Attachments are ignored."""
    plain: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and not part.get("filename"):
            if mime == "text/plain":
                plain.append(decode_part(data))
            elif mime == "text/html":
                html_parts.append(decode_part(data))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    return "\n".join(plain) if plain else "\n".join(html_parts)


def parse_message(msg: dict) -> dict | None:
    """Convert a Gmail message into the CSV schema dispatch_extractor.py expects."""
    labels = set(msg.get("labelIds", []))
    # Skip drafts and mail we only sent. Mail you send to yourself carries INBOX too, so it is kept.
    if "DRAFT" in labels or ("SENT" in labels and "INBOX" not in labels):
        return None
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    received_utc = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
    return {
        "email_id": msg["id"],
        "received": received_utc.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
        "sender": parseaddr(headers.get("from", ""))[1],
        "subject": headers.get("subject", ""),
        "body": extract_body(payload),
    }


def load_watermark(since_hours: int) -> int:
    if STATE_FILE.exists():
        return int(json.loads(STATE_FILE.read_text())["last_internal_ms"])
    first = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    return int(first.timestamp() * 1000)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--query", default="in:inbox",
                    help='Gmail search to select dispatch mail, for example "label:dispatch" (default: in:inbox)')
    ap.add_argument("--since-hours", type=int, default=24, help="lookback for the first run only")
    ap.add_argument("--max", type=int, default=200, help="maximum messages per run")
    ap.add_argument("--process", action="store_true", help="run dispatch_extractor.py on the new batch")
    args = ap.parse_args()

    service = get_service()
    last_ms = load_watermark(args.since_hours)
    messages = fetch_new_messages(service, args.query, last_ms, args.max)
    if not messages:
        print("No new messages")
        return

    rows = [r for r in (parse_message(m) for m in messages) if r]
    newest_ms = max(int(m["internalDate"]) for m in messages)
    print(f"Fetched {len(messages)} messages, {len(rows)} kept after filtering")

    if rows:
        INCOMING_DIR.mkdir(exist_ok=True)
        batch = INCOMING_DIR / f"batch_{datetime.now():%Y%m%d_%H%M%S}.csv"
        pd.DataFrame(rows).to_csv(batch, index=False)
        print(f"Wrote {batch}")
        if args.process:
            extractor = Path(__file__).with_name("dispatch_extractor.py")
            subprocess.run([sys.executable, str(extractor), "--input", str(batch), "--append"], check=True)

    # Advance the watermark only after the batch was written (and processed, if requested).
    # If the extractor crashes, the next run fetches these messages again; --append dedupes them.
    STATE_FILE.write_text(json.dumps({"last_internal_ms": newest_ms}))


if __name__ == "__main__":
    main()
