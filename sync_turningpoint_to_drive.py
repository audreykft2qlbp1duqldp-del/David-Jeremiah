#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sync Turning Point Radio (TWR360 ministry 23) -> Google Drive

Fix chính:
- KHÔNG phụ thuộc RSS (tránh lỗi "RSS parse ra 0 entries").
- Lấy danh sách episode mới nhất từ trang ministry (Recent + Listen).
- Với mỗi episode:
    + Mở trang "Listen" (action,audio)
    + Bóc link mediahit chứa URL audio dạng base64
    + Decode ra link audio trực tiếp (mp3/m4a)
    + Tải về -> upload lên Google Drive
- Lưu state (seen_program_ids) vào Drive dưới dạng state.json trong cùng folder
  => không cần git push/commit (né lỗi 403 quyền repo)

Yêu cầu secrets:
- GDRIVE_FOLDER_ID
- GDRIVE_OAUTH_TOKEN_JSON (authorized_user JSON có refresh_token)
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# =========================
# Config
# =========================
MINISTRY_URL = "https://www.twr360.org/ministry/23/turning-point-radio/?lang=1"
STATE_DRIVE_NAME = "state.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]

MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "10"))
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "3"))
TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))

OUT_DIR = Path(os.getenv("OUT_DIR", "out"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

UA = os.getenv(
    "HTTP_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
)

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
)

# =========================
# Models
# =========================
@dataclass
class Episode:
    program_id: str
    listen_url: str
    title: Optional[str] = None
    date: Optional[str] = None  # YYYY-MM-DD
    audio_url: Optional[str] = None  # direct mp3/m4a


# =========================
# Helpers: Drive
# =========================
def load_oauth_from_env() -> Optional[Credentials]:
    tok = (os.environ.get("GDRIVE_OAUTH_TOKEN_JSON") or "").strip()
    if not tok:
        print("[Drive] Missing GDRIVE_OAUTH_TOKEN_JSON. Skip upload/state.")
        return None

    try:
        info = json.loads(tok)
    except Exception as e:
        print(f"[Drive] OAuth token JSON invalid: {e}")
        return None

    # Tránh lỗi invalid_scope: nếu token có scopes/scope thì dùng đúng,
    # nếu không có thì để None (refresh không ép scope mới).
    scopes = info.get("scopes") or info.get("scope")
    if isinstance(scopes, str):
        scopes_list = scopes.split()
    elif isinstance(scopes, list):
        scopes_list = scopes
    else:
        scopes_list = None

    try:
        creds = Credentials.from_authorized_user_info(info, scopes=scopes_list)
        if creds.scopes is None:
            # set scope cho client lib; refresh vẫn không ép scope mới
            creds._scopes = SCOPES  # type: ignore[attr-defined]
        return creds
    except Exception as e:
        print(f"[Drive] Cannot build Credentials: {e}")
        return None


def init_drive():
    creds = load_oauth_from_env()
    if not creds:
        return None
    try:
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"[Drive] build() failed: {e}")
        return None


def ensure_folder_access(service, folder_id: str) -> Optional[str]:
    if not service or not folder_id:
        return None
    try:
        meta = service.files().get(fileId=folder_id, fields="id,name", supportsAllDrives=True).execute()
        print(f"[Drive] Folder OK: {meta.get('name')} ({meta.get('id')})")
        return meta.get("id")
    except HttpError as e:
        print(f"[Drive] Cannot access folder {folder_id}: {e}")
        return None


def drive_find_by_name(service, folder_id: str, name: str) -> Optional[str]:
    safe_name = name.replace("'", "\\'")
    q = f"name='{safe_name}' and '{folder_id}' in parents and trashed=false"
    res = service.files().list(
        q=q,
        fields="files(id,name,modifiedTime)",
        pageSize=5,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = res.get("files") or []
    if not files:
        return None
    files.sort(key=lambda x: x.get("modifiedTime", ""), reverse=True)
    return files[0]["id"]


def drive_download_text(service, file_id: str) -> Optional[str]:
    fh = io.BytesIO()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)
    raw = fh.read()
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("utf-8", errors="ignore")


def drive_upload_or_update(service, folder_id: str, local_path: Path, name: Optional[str] = None) -> str:
    """Upload; nếu trùng tên trong folder -> update (không tạo rác file)."""
    name = name or local_path.name
    existing_id = drive_find_by_name(service, folder_id, name)
    media = MediaFileUpload(str(local_path), resumable=True)

    if existing_id:
        updated = service.files().update(
            fileId=existing_id,
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        return updated["id"]

    created = service.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return created["id"]


# =========================
# Helpers: TWR360 parsing
# =========================
def http_get(url: str) -> Tuple[int, str]:
    r = SESSION.get(url, timeout=TIMEOUT)
    return r.status_code, r.text


def extract_episode_list(html: str) -> List[Episode]:
    """
    Bóc list "Listen" URL từ ministry page.
    Thường có dạng:
      /programs/view/id,1146101/action,audio/lang,1
    hoặc URL-encoded:
      /programs/view/id%2C1146101/action%2Caudio/lang%2C1
    """
    hrefs = re.findall(r'href="([^"]*programs/view/[^"]*action[^"]*)"', html, flags=re.I)
    out: List[Episode] = []
    seen: Set[str] = set()

    for href in hrefs:
        full = urllib.parse.urljoin("https://www.twr360.org/", href)
        full_dec = urllib.parse.unquote(full)

        m = re.search(r"id,(\d+)", full_dec)
        if not m:
            continue

        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)

        out.append(Episode(program_id=pid, listen_url=full_dec))

    return out


def decode_mediahit_to_audio_url(mediahit_url: str) -> Optional[str]:
    """
    mediahit url chứa:
      .../mediahit/id%2Cxxxx/url%2C<base64>   (kết thúc bằng ~ thay cho =)
    Decode base64 -> direct audio url (mp3/m4a)
    """
    try:
        u = urllib.parse.unquote(mediahit_url)
        if "url," in u:
            b64 = u.split("url,", 1)[1]
        elif "url%2C" in mediahit_url:
            b64 = urllib.parse.unquote(mediahit_url.split("url%2C", 1)[1])
        else:
            return None

        b64 = b64.strip().replace("~", "=")
        decoded = base64.urlsafe_b64decode(b64.encode("utf-8")).decode("utf-8", errors="ignore")
        return decoded if decoded.startswith("http") else None
    except Exception:
        return None


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def html_unescape(s: str) -> str:
    return (
        s.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def parse_title_and_date(html: str) -> Tuple[Optional[str], Optional[str]]:
    # title: ưu tiên og:title
    title = None
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html, flags=re.I)
    if m:
        title = html_unescape(m.group(1)).strip()

    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.I | re.S)
        if m:
            t = strip_tags(m.group(1))
            title = re.sub(r"\s+", " ", t).strip() or None

    # date: "December 27, 2025"
    date_iso = None
    dm = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
        html,
    )
    if dm:
        try:
            d = dt.datetime.strptime(dm.group(0), "%B %d, %Y").date()
            date_iso = d.isoformat()
        except Exception:
            date_iso = None

    return title, date_iso


def extract_best_audio_from_listen_page(html: str) -> Optional[str]:
    """
    Trên listen page sẽ có link "Downloads -> Audio" trỏ tới /mediahit/.../url,<b64>
    Ta lấy TẤT CẢ mediahit rồi decode, chọn cái có vẻ chất lượng tốt nhất.
    """
    mediahits = re.findall(r"https?://www\.twr360\.org/mediahit/[^\"' <>\n]+", html, flags=re.I)
    decoded_urls: List[str] = []

    for mh in mediahits:
        au = decode_mediahit_to_audio_url(mh)
        if au and (".mp3" in au or ".m4a" in au or "audio" in au):
            decoded_urls.append(au)

    if not decoded_urls:
        return None

    def score(u: str) -> int:
        u2 = u.lower()
        sc = 0
        if "hi" in u2 or "128" in u2 or "192" in u2:
            sc += 30
        if "med" in u2 or "64" in u2:
            sc += 20
        if "low" in u2 or "32" in u2:
            sc += 10
        if u2.endswith(".mp3"):
            sc += 5
        return sc

    decoded_urls.sort(key=score, reverse=True)
    return decoded_urls[0]


# =========================
# Download + naming
# =========================
def safe_filename(name: str, max_len: int = 140) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len].rstrip() if len(name) > max_len else name


def download_file(url: str, out_path: Path) -> None:
    with SESSION.get(url, stream=True, timeout=TIMEOUT) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", "0") or "0")
        got = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)

    if total and got:
        print(f"[DL] {out_path.name}: {got}/{total} bytes ({got*100.0/total:.1f}%)")
    else:
        print(f"[DL] {out_path.name}: {got} bytes")


# =========================
# State
# =========================
def load_state_from_drive(service, folder_id: str) -> Dict:
    """
    State format:
      {"seen_program_ids": ["1146101", ...]}
    """
    state = {"seen_program_ids": []}
    if not service or not folder_id:
        return state

    try:
        fid = drive_find_by_name(service, folder_id, STATE_DRIVE_NAME)
        if not fid:
            print("[State] No state.json on Drive yet. Start fresh.")
            return state

        txt = drive_download_text(service, fid) or ""
        obj = json.loads(txt) if txt.strip() else {}
        if isinstance(obj, dict) and "seen_program_ids" in obj:
            print(f"[State] Loaded state.json from Drive. seen={len(obj.get('seen_program_ids', []))}")
            return obj

        print("[State] state.json invalid format. Start fresh.")
        return state
    except Exception as e:
        print(f"[State] Load state from Drive failed: {e}")
        return state


def save_state_to_drive(service, folder_id: str, state: Dict) -> None:
    if not service or not folder_id:
        return
    tmp = OUT_DIR / STATE_DRIVE_NAME
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    fid = drive_upload_or_update(service, folder_id, tmp, name=STATE_DRIVE_NAME)
    print(f"[State] Uploaded state.json to Drive (fileId={fid}).")


# =========================
# Main
# =========================
def main() -> int:
    folder_id = (os.environ.get("GDRIVE_FOLDER_ID") or "").strip()

    drive = init_drive()
    drive_folder = ensure_folder_access(drive, folder_id) if drive and folder_id else None

    state = load_state_from_drive(drive, drive_folder) if drive and drive_folder else {"seen_program_ids": []}
    seen: Set[str] = set(state.get("seen_program_ids") or [])

    print(f"[Fetch] {MINISTRY_URL}")
    status, html = http_get(MINISTRY_URL)
    print(f"[Fetch] status={status} bytes={len(html.encode('utf-8', errors='ignore'))}")

    eps = extract_episode_list(html)
    print(f"[Parse] Found {len(eps)} episode(s) from ministry page.")

    if not eps:
        snippet = html[:500].replace("\n", "\\n")
        print(f"[ERROR] Cannot parse episodes. First 500 chars: {snippet}")
        return 2

    # Trang ministry đang hiển thị newest trước -> giữ thứ tự
    new_eps = [e for e in eps if e.program_id not in seen][:MAX_PER_RUN]
    print(f"[Plan] New episodes this run: {len(new_eps)} (max {MAX_PER_RUN}).")

    uploaded = 0
    for idx, ep in enumerate(new_eps, 1):
        print(f"\n[{idx}/{len(new_eps)}] program_id={ep.program_id}")
        try:
            st, page = http_get(ep.listen_url)
            if st != 200:
                raise RuntimeError(f"listen page status={st}")

            title, date_iso = parse_title_and_date(page)
            ep.title = title or f"TurningPoint_{ep.program_id}"
            ep.date = date_iso

            audio = extract_best_audio_from_listen_page(page)
            if not audio:
                raise RuntimeError("Cannot find audio mediahit on listen page.")
            ep.audio_url = audio

            prefix = ep.date or dt.date.today().isoformat()
            fn = safe_filename(f"{prefix} - {ep.title}.mp3")
            local_path = OUT_DIR / fn
            if local_path.exists():
                local_path.unlink()

            print(f"[Audio] {audio}")
            print(f"[File]  {local_path}")

            download_file(audio, local_path)

            if drive and drive_folder:
                fid = drive_upload_or_update(drive, drive_folder, local_path)
                uploaded += 1
                print(f"[Drive] Uploaded: {local_path.name} (fileId={fid})")
                try:
                    local_path.unlink()
                except OSError:
                    pass
            else:
                print("[Drive] Skip upload (missing Drive creds/folder). Kept local file.")

            # mark seen
            seen.add(ep.program_id)

        except Exception as e:
            print(f"[FAIL] program_id={ep.program_id}: {e}")

        if idx < len(new_eps):
            time.sleep(SLEEP_SECONDS)

    # save state
    state["seen_program_ids"] = sorted(seen)
    if drive and drive_folder:
        save_state_to_drive(drive, drive_folder, state)

    print(f"\nDone. Uploaded {uploaded} episode(s). Seen total={len(seen)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
