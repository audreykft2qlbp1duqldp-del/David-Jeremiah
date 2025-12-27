# -*- coding: utf-8 -*-
"""
Turning Point (TWR360) -> Download latest audio -> Upload to Google Drive
Lưu ý: Chỉ dùng cho nội dung bạn có quyền tải/lưu trữ theo điều khoản của nguồn.
"""

import os
import re
import json
import time
import base64
import html as _html
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import unquote, urljoin

import requests

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials

# ================== CONFIG ==================
MINISTRY_URL = "https://www.twr360.org/ministry/23/turning-point-radio/?lang=1"

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "10"))
SLEEP_SECONDS = float(os.environ.get("SLEEP_SECONDS", "3"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))

OUT_DIR = Path(os.environ.get("OUT_DIR", "out")).resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)

GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
GDRIVE_OAUTH_TOKEN_JSON = os.environ.get("GDRIVE_OAUTH_TOKEN_JSON", "").strip()

STATE_DRIVE_NAME = "state.json"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# ================== DATA ==================
@dataclass
class Episode:
    program_id: str

# ================== HTTP ==================
def http_get(url: str) -> Tuple[int, bytes]:
    headers = {"User-Agent": UA}
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    return r.status_code, r.content

def http_download(url: str, dest: Path) -> None:
    headers = {"User-Agent": UA}
    with requests.get(url, headers=headers, stream=True, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
        tmp.replace(dest)

# ================== PARSE ==================
def parse_program_ids_from_ministry(html_bytes: bytes) -> List[Episode]:
    s = html_bytes.decode("utf-8", errors="ignore")

    # Bắt các dạng:
    # /programs/view/id%2C1146101/action%2Caudio/lang%2C1
    # /programs/view/id,1146101/action,audio/lang,1
    ids = []
    patterns = [
        r"/programs/view/id%2C(\d+)/action%2Caudio",
        r"/programs/view/id,(\d+)/action,audio",
        r"/programs/view/id%2C(\d+)\b",
        r"/programs/view/id,(\d+)\b",
    ]
    seen = set()
    for pat in patterns:
        for m in re.finditer(pat, s, flags=re.IGNORECASE):
            pid = m.group(1)
            if pid not in seen:
                seen.add(pid)
                ids.append(Episode(program_id=pid))

    return ids

def sanitize_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = name.strip(" .")
    return name[:180] if len(name) > 180 else name

def extract_title_date_and_audio(program_id: str, page_html: str) -> Tuple[str, str, str]:
    """
    Return (title, date_yyyy_mm_dd, audio_url)
    """
    # Title: ưu tiên <h1>
    title = ""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1))
        title = _html.unescape(title).strip()

    if not title:
        # fallback meta og:title
        m = re.search(r'property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', page_html, flags=re.I)
        if m:
            title = _html.unescape(m.group(1)).strip()

    if not title:
        title = f"program_{program_id}"

    # Date (ví dụ: December 27, 2025)
    date_iso = ""
    m = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})\b",
        page_html,
        flags=re.IGNORECASE,
    )
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)}, {m.group(3)}", "%B %d, %Y")
            date_iso = dt.strftime("%Y-%m-%d")
        except Exception:
            date_iso = ""

    if not date_iso:
        date_iso = datetime.utcnow().strftime("%Y-%m-%d")

    # ====== AUDIO: tìm link mediahit trong HTML ======
    # Lấy tất cả href có chữ mediahit
    hrefs = []
    for mm in re.finditer(r'href=["\']([^"\']*mediahit[^"\']*)["\']', page_html, flags=re.I):
        hrefs.append(mm.group(1))

    # fallback: đôi khi link nằm trong JS / text
    if not hrefs:
        for mm in re.finditer(r'((?:https?://www\.twr360\.org)?/mediahit/[^"\'<>\s]+)', page_html, flags=re.I):
            hrefs.append(mm.group(1))

    # Decode các mediahit -> URL thật
    audio_candidates = []
    for href in hrefs:
        full = urljoin("https://www.twr360.org/", href)
        decoded = decode_mediahit(full)
        if not decoded:
            continue
        low = decoded.lower()
        if any(low.endswith(ext) for ext in [".mp3", ".m4a", ".mp4", ".aac", ".m4b"]):
            audio_candidates.append(decoded)

    if not audio_candidates:
        raise RuntimeError("Cannot find audio mediahit on program view page.")

    # Chọn “best” theo bitrate số trong tên file (vd: _64.mp3, _128.mp3)
    best = pick_best_audio(audio_candidates)

    return title, date_iso, best

def decode_mediahit(mediahit_url: str) -> Optional[str]:
    """
    mediahit url dạng:
    https://www.twr360.org/mediahit/id%2C6566871/url%2C<BASE64>~
    => base64 decode ra URL mp3 thật
    """
    u = unquote(mediahit_url)
    m = re.search(r"url,([^/?#]+)", u)
    if not m:
        return None
    b64 = m.group(1).replace("~", "=")

    # padding
    pad = (-len(b64)) % 4
    if pad:
        b64 = b64 + ("=" * pad)

    try:
        raw = base64.b64decode(b64).decode("utf-8", errors="ignore").strip()
        if raw.startswith("http"):
            return raw
    except Exception:
        return None
    return None

def pick_best_audio(urls: List[str]) -> str:
    def score(u: str) -> Tuple[int, int]:
        ul = u.lower()
        # ưu tiên ext
        ext_rank = 0
        if ul.endswith(".m4a"):
            ext_rank = 3
        elif ul.endswith(".mp3"):
            ext_rank = 2
        elif ul.endswith(".aac"):
            ext_rank = 1

        # ưu tiên bitrate số lớn hơn nếu có
        br = 0
        m = re.search(r"[_\-](\d{2,3})\.(mp3|m4a|aac)\b", ul)
        if m:
            try:
                br = int(m.group(1))
            except Exception:
                br = 0
        return (ext_rank, br)

    return sorted(urls, key=score, reverse=True)[0]

# ================== DRIVE ==================
def load_credentials_from_env() -> Optional[Credentials]:
    if not GDRIVE_OAUTH_TOKEN_JSON:
        return None
    try:
        info = json.loads(GDRIVE_OAUTH_TOKEN_JSON)

        # QUAN TRỌNG: dùng đúng scope đã cấp trong token để tránh invalid_scope
        scopes = info.get("scopes") or info.get("scope")
        if isinstance(scopes, str):
            scopes = scopes.split()
        if not scopes:
            # fallback an toàn nếu token không ghi scope
            scopes = ["https://www.googleapis.com/auth/drive"]

        creds = Credentials.from_authorized_user_info(info, scopes=scopes)
        return creds
    except Exception as e:
        print(f"[Drive] OAuth token JSON không hợp lệ: {e}")
        return None

def init_drive_service():
    creds = load_credentials_from_env()
    if not creds:
        print("[Drive] Missing GDRIVE_OAUTH_TOKEN_JSON. Skip Drive.")
        return None
    return build("drive", "v3", credentials=creds)

def ensure_folder(service, folder_id: str) -> Optional[str]:
    if not service or not folder_id:
        return None
    try:
        meta = service.files().get(
            fileId=folder_id,
            fields="id,name",
            supportsAllDrives=True,
        ).execute()
        print(f"[Drive] Folder OK: {meta.get('name')} ({meta.get('id')})")
        return meta["id"]
    except HttpError as e:
        print(f"[Drive] Cannot access folder: {e}")
        return None

def drive_find_by_name(service, folder_id: str, name: str) -> Optional[str]:
    # Escape dấu ' cho Drive query
    escaped_name = name.replace("'", "\\'")

    q = f"name='{escaped_name}' and '{folder_id}' in parents and trashed=false"

    res = service.files().list(
        q=q,
        fields="files(id,name)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = res.get("files", [])
    return files[0]["id"] if files else None


def drive_download_text(service, file_id: str) -> str:
    req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue().decode("utf-8", errors="ignore")

def drive_upload_or_update(service, folder_id: str, local_path: Path, remote_name: str) -> str:
    existing_id = drive_find_by_name(service, folder_id, remote_name)
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
        body={"name": remote_name, "parents": [folder_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return created["id"]

# ================== STATE ==================
def load_state_from_drive(service, folder_id: str) -> dict:
    state = {"seen_program_ids": []}
    if not service or not folder_id:
        return state

    sid = drive_find_by_name(service, folder_id, STATE_DRIVE_NAME)
    if not sid:
        print("[State] No state.json on Drive yet. Start fresh.")
        return state

    try:
        txt = drive_download_text(service, sid)
        st = json.loads(txt)
        if isinstance(st, dict) and "seen_program_ids" in st:
            return st
    except Exception as e:
        print(f"[State] Failed to read state.json from Drive: {e}")
    return state

def save_state_to_drive(service, folder_id: str, state: dict) -> Optional[str]:
    if not service or not folder_id:
        return None
    tmp = OUT_DIR / "_state.json"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fid = drive_upload_or_update(service, folder_id, tmp, STATE_DRIVE_NAME)
    print(f"[State] Uploaded state.json to Drive (fileId={fid}).")
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    return fid

# ================== MAIN ==================
def main():
    service = init_drive_service()
    folder_id = ensure_folder(service, GDRIVE_FOLDER_ID) if service else None

    state = load_state_from_drive(service, folder_id) if (service and folder_id) else {"seen_program_ids": []}
    seen = set(str(x) for x in state.get("seen_program_ids", []))

    # 1) Fetch ministry page
    print(f"[Fetch] {MINISTRY_URL}")
    st, content = http_get(MINISTRY_URL)
    print(f"[Fetch] status={st} bytes={len(content)}")
    if st != 200:
        raise RuntimeError(f"Fetch ministry page failed: HTTP {st}")

    eps = parse_program_ids_from_ministry(content)
    print(f"[Parse] Found {len(eps)} episode(s) from ministry page.")

    # new episodes (theo thứ tự xuất hiện)
    new_eps = [e for e in eps if e.program_id not in seen][:MAX_PER_RUN]
    print(f"[Plan] New episodes this run: {len(new_eps)} (max {MAX_PER_RUN}).")

    uploaded = 0
    failed = 0

    for idx, ep in enumerate(new_eps, 1):
        pid = ep.program_id
        print(f"[{idx}/{len(new_eps)}] program_id={pid}")

        # 2) Fetch program view page (CHỖ FIX CHÍNH)
        program_url = f"https://www.twr360.org/programs/view/id%2C{pid}"
        try:
            st2, html_bytes = http_get(program_url)
            if st2 != 200:
                raise RuntimeError(f"HTTP {st2} on program page")
            page_html = html_bytes.decode("utf-8", errors="ignore")

            title, date_iso, audio_url = extract_title_date_and_audio(pid, page_html)

            ext = ".mp3"
            low = audio_url.lower()
            for e in [".m4a", ".mp3", ".aac", ".mp4", ".m4b"]:
                if low.endswith(e):
                    ext = e
                    break

            fname = sanitize_filename(f"{date_iso} - {title} - {pid}{ext}")
            local_path = OUT_DIR / fname

            print(f"[Audio] {audio_url}")
            print(f"[DL] -> {local_path.name}")
            http_download(audio_url, local_path)

            if service and folder_id:
                # Upload audio file
                media = MediaFileUpload(str(local_path), resumable=True)
                created = service.files().create(
                    body={"name": local_path.name, "parents": [folder_id]},
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
                print(f"[Drive] Uploaded: {local_path.name} (fileId={created.get('id')})")
                uploaded += 1

                # Cleanup local
                try:
                    local_path.unlink(missing_ok=True)
                except Exception:
                    pass

            # mark seen only if we got audio successfully
            seen.add(pid)

        except Exception as e:
            failed += 1
            print(f"[FAIL] program_id={pid}: {e}")

        if idx < len(new_eps):
            time.sleep(SLEEP_SECONDS)

    # update state
    state["seen_program_ids"] = sorted(seen, key=lambda x: int(x) if x.isdigit() else x)
    if service and folder_id:
        save_state_to_drive(service, folder_id, state)

    print(f"Done. Uploaded {uploaded} episode(s). Seen total={len(seen)}. Failed={failed}.")

if __name__ == "__main__":
    main()
