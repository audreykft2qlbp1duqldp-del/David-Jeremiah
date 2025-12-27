# -*- coding: utf-8 -*-
"""
Sync newest Turning Point Radio (Dr. David Jeremiah) episodes from TWR360 to Google Drive.

Key fix:
- DO NOT scrape "Listen" (action,audio) page for mediahit. It often doesn't contain download URL.
- Scrape the PROGRAM VIEW page and extract Downloads -> Audio (mediahit), then decode base64 to get direct MP3 URL.

State:
- state.json is stored in the same Drive folder (GDRIVE_FOLDER_ID). No git push needed.
"""

import os
import re
import io
import json
import time
import base64
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials


# ================== CONFIG ==================
MINISTRY_URL = os.environ.get(
    "MINISTRY_URL",
    "https://www.twr360.org/ministry/23/turning-point-radio/?lang=1"
).strip()

RSS_URL = os.environ.get(
    "RSS_URL",
    "https://www.twr360.org/programs/rss/ministry_id,23"
).strip()

OUT_DIR = Path(os.environ.get("OUT_DIR", "out")).resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "10"))
SLEEP_SECONDS = int(os.environ.get("SLEEP_SECONDS", "2"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "40"))

GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
STATE_DRIVE_NAME = os.environ.get("STATE_DRIVE_NAME", "state.json").strip()

# A reasonably “real” UA reduces weird server responses
HTTP_UA = os.environ.get(
    "HTTP_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
).strip()


# ================== DATA ==================
@dataclass
class Episode:
    program_id: str
    program_url: str
    title_hint: str = ""


# ================== HTTP ==================
def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": HTTP_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    return s


def fetch_text(s: requests.Session, url: str) -> Tuple[int, str]:
    r = s.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
    return r.status_code, r.text


def fetch_bytes(s: requests.Session, url: str) -> Tuple[int, bytes, str]:
    r = s.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True, stream=True)
    ctype = r.headers.get("content-type", "")
    return r.status_code, r.content, ctype


# ================== PARSE LIST ==================
def uniq_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def parse_program_ids_from_ministry(html: str) -> List[Episode]:
    """
    Parse program IDs from ministry page (Recent list).
    We prefer program view URLs (not action,audio).
    """
    # Grab all programs/view links
    # Examples:
    #   /programs/view/id%2C1146101/lang%2C1
    #   https://www.twr360.org/programs/view/id,1146101/
    pattern = re.compile(r"""href=["']([^"']*?/programs/view/id(?:%2C|,)(\d+)[^"']*)["']""", re.I)
    matches = pattern.findall(html)

    eps: List[Episode] = []
    for href, pid in matches:
        # Ignore "action,audio" pages; we want the main program view page
        if re.search(r"(?:action(?:%2C|,))audio", href, re.I):
            continue

        full_url = href
        if full_url.startswith("/"):
            full_url = urljoin("https://www.twr360.org", full_url)
        elif full_url.startswith("www."):
            full_url = "https://" + full_url

        # Ensure English lang=1 if missing (optional, but stable)
        if "lang" not in full_url:
            # safest: append trailing slash then lang
            if not full_url.endswith("/"):
                full_url += "/"
            full_url = urljoin(full_url, "lang,1")

        eps.append(Episode(program_id=str(pid), program_url=full_url))

    # Keep order, unique by program_id
    seen = set()
    out: List[Episode] = []
    for e in eps:
        if e.program_id in seen:
            continue
        seen.add(e.program_id)
        out.append(e)

    return out


def parse_program_ids_from_rss(raw: bytes) -> List[Episode]:
    """
    RSS is sometimes served with odd content-type, but body includes item links.
    We extract program view URLs from it.
    """
    text = raw.decode("utf-8", errors="ignore")

    # Extract program URLs like https://www.twr360.org/programs/view/id,1146101/
    urls = re.findall(r"https?://www\.twr360\.org/programs/view/id,\d+/?", text, flags=re.I)
    urls = uniq_keep_order(urls)

    eps: List[Episode] = []
    for u in urls:
        m = re.search(r"id,(\d+)", u)
        if not m:
            continue
        pid = m.group(1)
        # add lang=1 for consistency
        if not u.endswith("/"):
            u += "/"
        u = urljoin(u, "lang,1")
        eps.append(Episode(program_id=pid, program_url=u))
    return eps


def get_latest_episodes(s: requests.Session) -> List[Episode]:
    # Try RSS first (usually most stable)
    try:
        st, raw, ctype = fetch_bytes(s, RSS_URL)
        print(f"[RSS] status={st} bytes={len(raw)} ctype={ctype}")
        if st == 200 and len(raw) > 500:
            eps = parse_program_ids_from_rss(raw)
            if eps:
                print(f"[RSS] Parsed {len(eps)} episode(s).")
                return eps
    except Exception as e:
        print(f"[RSS] Failed: {e}")

    # Fallback: ministry page
    st, html = fetch_text(s, MINISTRY_URL)
    print(f"[Fetch] {MINISTRY_URL}")
    print(f"[Fetch] status={st} bytes={len(html)}")
    if st != 200 or len(html) < 1000:
        raise RuntimeError(f"Cannot fetch ministry page (status={st}, bytes={len(html)})")
    eps = parse_program_ids_from_ministry(html)
    print(f"[Parse] Found {len(eps)} episode(s) from ministry page.")
    return eps


# ================== PARSE PROGRAM PAGE ==================
def strip_tags(x: str) -> str:
    x = re.sub(r"<[^>]+>", " ", x)
    x = re.sub(r"\s+", " ", x).strip()
    return x


def extract_title_and_date(html: str, fallback_title: str = "") -> Tuple[str, str]:
    """
    Return (title, date_yyyy_mm_dd or "").
    Date appears in page text like 'December 27, 2025'.
    """
    title = ""
    # <h1 ...>Title</h1>
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.I | re.S)
    if m:
        title = strip_tags(m.group(1))

    if not title:
        # meta og:title
        m2 = re.search(r"""property=["']og:title["']\s+content=["']([^"']+)["']""", html, flags=re.I)
        if m2:
            title = m2.group(1).strip()

    if not title:
        title = fallback_title.strip() or "episode"

    # Date like "December 27, 2025"
    date_txt = ""
    md = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b", html)
    if md:
        date_txt = md.group(0)

    yyyy_mm_dd = ""
    if date_txt:
        try:
            d = dt.datetime.strptime(date_txt, "%B %d, %Y").date()
            yyyy_mm_dd = d.isoformat()
        except Exception:
            yyyy_mm_dd = ""

    return title, yyyy_mm_dd


def find_mediahit_links(html: str, base_url: str) -> List[str]:
    """
    Find candidate mediahit URLs in raw HTML attributes.
    """
    links = re.findall(r"""href\s*=\s*["']([^"']*mediahit[^"']+)["']""", html, flags=re.I)
    links += re.findall(r"""data-href\s*=\s*["']([^"']*mediahit[^"']+)["']""", html, flags=re.I)
    links = [x.strip() for x in links if x.strip()]

    out = []
    for x in links:
        if x.startswith("/"):
            out.append(urljoin(base_url, x))
        elif x.startswith("http://") or x.startswith("https://"):
            out.append(x)
        else:
            out.append(urljoin(base_url, x))
    return uniq_keep_order(out)


def decode_mediahit_to_direct_url(mediahit_url: str) -> Optional[str]:
    """
    mediahit contains ".../url%2C<base64>..." or after unquote: ".../url,<base64>..."
    Some use "~" in place of "=" padding.
    """
    try:
        u = unquote(mediahit_url)
        m = re.search(r"/url,([^/?#]+)", u)
        if not m:
            # sometimes still encoded in strange ways
            m = re.search(r"url%2C([^/?#]+)", mediahit_url, flags=re.I)
            if not m:
                return None
            b64 = m.group(1)
        else:
            b64 = m.group(1)

        b64 = b64.replace("~", "=")

        # pad base64
        pad = (-len(b64)) % 4
        if pad:
            b64 += "=" * pad

        raw = base64.b64decode(b64)
        direct = raw.decode("utf-8", errors="ignore").strip()
        if direct.startswith("http://") or direct.startswith("https://"):
            return direct
        return None
    except Exception:
        return None


def choose_best_audio_url(candidates: List[str]) -> Optional[str]:
    """
    Prefer mp3 audio links.
    """
    aud = []
    for u in candidates:
        if not u:
            continue
        low = u.lower()
        if ".mp3" in low or "audio" in low:
            aud.append(u)
    if aud:
        return aud[0]
    return candidates[0] if candidates else None


def sanitize_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180] if len(name) > 180 else name


def download_audio(s: requests.Session, audio_url: str, out_path: Path) -> None:
    with s.get(audio_url, timeout=HTTP_TIMEOUT, stream=True, allow_redirects=True) as r:
        r.raise_for_status()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


# ================== DRIVE ==================
DEFAULT_SCOPES = ["https://www.googleapis.com/auth/drive"]


def load_oauth_from_env() -> Optional[Credentials]:
    tok = os.environ.get("GDRIVE_OAUTH_TOKEN_JSON", "").strip()
    if not tok:
        return None
    try:
        info = json.loads(tok)

        # IMPORTANT: avoid invalid_scope by respecting token's own scopes if present
        scopes = info.get("scopes") or info.get("scope")
        if isinstance(scopes, str):
            scopes_list = scopes.split()
        elif isinstance(scopes, list):
            scopes_list = scopes
        else:
            scopes_list = DEFAULT_SCOPES

        return Credentials.from_authorized_user_info(info, scopes_list)
    except Exception as e:
        print(f"[Drive] OAuth token JSON invalid: {e}")
        return None


def init_drive_service():
    creds = load_oauth_from_env()
    if not creds:
        print("[Drive] Missing GDRIVE_OAUTH_TOKEN_JSON. Skip Drive upload.")
        return None
    return build("drive", "v3", credentials=creds)


def ensure_folder(service, folder_id: str) -> Optional[str]:
    if not service or not folder_id:
        return None
    try:
        meta = service.files().get(
            fileId=folder_id,
            fields="id,name",
            supportsAllDrives=True
        ).execute()
        print(f"[Drive] Folder OK: {meta.get('name')} ({meta.get('id')})")
        return meta["id"]
    except HttpError as e:
        print(f"[Drive] Cannot access folder_id={folder_id}: {e}")
        return None


def drive_escape_q_value(s: str) -> str:
    # Google Drive query uses single quotes; escape backslash + single quote
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "\\'")
    return s


def drive_find_by_name(service, folder_id: str, name: str) -> Optional[str]:
    safe_name = drive_escape_q_value(name)
    q = f"name='{safe_name}' and '{folder_id}' in parents and trashed=false"
    res = service.files().list(
        q=q,
        spaces="drive",
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        pageSize=5
    ).execute()
    files = res.get("files", []) or []
    return files[0]["id"] if files else None


def drive_download_to_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()


def drive_upload_file(service, folder_id: str, local_path: Path, mimetype: str, drive_name: Optional[str] = None) -> str:
    name = drive_name or local_path.name
    existing_id = drive_find_by_name(service, folder_id, name)

    media = MediaFileUpload(str(local_path), mimetype=mimetype, resumable=True)

    if existing_id:
        updated = service.files().update(
            fileId=existing_id,
            media_body=media,
            fields="id",
            supportsAllDrives=True
        ).execute()
        return updated["id"]

    created = service.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True
    ).execute()
    return created["id"]


# ================== STATE ==================
def load_state_from_drive(service, folder_id: str, state_name: str) -> Dict:
    state = {"seen_program_ids": []}
    try:
        sid = drive_find_by_name(service, folder_id, state_name)
        if not sid:
            print("[State] No state.json on Drive yet. Start fresh.")
            return state
        raw = drive_download_to_bytes(service, sid)
        state = json.loads(raw.decode("utf-8", errors="ignore"))
        if "seen_program_ids" not in state or not isinstance(state["seen_program_ids"], list):
            state["seen_program_ids"] = []
        print(f"[State] Loaded from Drive: seen={len(state['seen_program_ids'])}")
        return state
    except Exception as e:
        print(f"[State] Load failed, start fresh: {e}")
        return {"seen_program_ids": []}


def save_state_to_drive(service, folder_id: str, state_name: str, state: Dict) -> None:
    tmp = OUT_DIR / state_name
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    fid = drive_upload_file(service, folder_id, tmp, mimetype="application/json", drive_name=state_name)
    print(f"[State] Uploaded {state_name} to Drive (fileId={fid}).")


# ================== MAIN ==================
def main():
    s = http_session()

    drive = init_drive_service()
    folder_id = ensure_folder(drive, GDRIVE_FOLDER_ID) if drive else None
    if not drive or not folder_id:
        raise RuntimeError("Drive not configured (GDRIVE_FOLDER_ID / GDRIVE_OAUTH_TOKEN_JSON).")

    state = load_state_from_drive(drive, folder_id, STATE_DRIVE_NAME)
    seen = set(str(x) for x in state.get("seen_program_ids", []))

    eps = get_latest_episodes(s)
    # newest first; only pick ones not seen
    new_eps = [e for e in eps if e.program_id not in seen]
    plan = new_eps[:MAX_PER_RUN]
    print(f"[Plan] New episodes this run: {len(plan)} (max {MAX_PER_RUN}).")

    uploaded = 0
    failed = 0

    for idx, ep in enumerate(plan, 1):
        print(f"[{idx}/{len(plan)}] program_id={ep.program_id}")
        try:
            st, html = fetch_text(s, ep.program_url)
            if st != 200 or len(html) < 2000:
                raise RuntimeError(f"Program page fetch failed (status={st}, bytes={len(html)})")

            title, yyyy_mm_dd = extract_title_and_date(html, fallback_title=ep.title_hint)
            base = "https://www.twr360.org"

            mediahits = find_mediahit_links(html, base_url=base)
            if not mediahits:
                raise RuntimeError("No mediahit links found on program view page.")

            direct_urls = []
            for mh in mediahits:
                direct = decode_mediahit_to_direct_url(mh)
                if direct:
                    direct_urls.append(direct)

            # Keep only likely-audio URLs
            direct_urls = [u for u in direct_urls if ".mp3" in u.lower() or "audio" in u.lower()]
            if not direct_urls:
                # still keep any decoded URL
                for mh in mediahits:
                    direct = decode_mediahit_to_direct_url(mh)
                    if direct:
                        direct_urls.append(direct)

            if not direct_urls:
                raise RuntimeError("Cannot decode any mediahit -> direct URL.")

            audio_url = choose_best_audio_url(direct_urls)
            if not audio_url:
                raise RuntimeError("No usable audio URL selected.")

            # Decide file extension
            path = urlparse(audio_url).path
            ext = Path(path).suffix.lower() or ".mp3"
            if ext not in [".mp3", ".m4a", ".aac", ".wav", ".mp4"]:
                ext = ".mp3"

            date_prefix = f"{yyyy_mm_dd} - " if yyyy_mm_dd else ""
            fname = sanitize_filename(f"{date_prefix}{title}{ext}")
            local_path = OUT_DIR / fname

            print(f"  [DL] {audio_url}")
            download_audio(s, audio_url, local_path)
            if not local_path.exists() or local_path.stat().st_size < 10_000:
                raise RuntimeError(f"Downloaded file looks wrong (size={local_path.stat().st_size if local_path.exists() else 0})")

            # Upload to Drive
            mime = "audio/mpeg" if ext == ".mp3" else "application/octet-stream"
            fid = drive_upload_file(drive, folder_id, local_path, mimetype=mime)
            print(f"  [Drive] Uploaded: {fname} (fileId={fid})")

            # Mark seen only after successful upload
            seen.add(ep.program_id)
            state["seen_program_ids"] = sorted(list(seen), key=lambda x: int(x))
            uploaded += 1

            # Optional cleanup local
            try:
                local_path.unlink(missing_ok=True)
            except Exception:
                pass

        except Exception as e:
            failed += 1
            print(f"[FAIL] program_id={ep.program_id}: {e}")

        if idx < len(plan):
            time.sleep(SLEEP_SECONDS)

    # Save state even if some failed (but only successful are marked seen)
    save_state_to_drive(drive, folder_id, STATE_DRIVE_NAME, state)

    print(f"Done. Uploaded {uploaded} episode(s). Failed={failed}. Seen total={len(seen)}.")


if __name__ == "__main__":
    main()
