# -*- coding: utf-8 -*-
"""
Turning Point Radio (TWR360 ministry 23) -> tải audio mới nhất (public) -> upload Google Drive
- Không dùng RSS (RSS bạn nhận entries=0).
- Parse từ trang ministry -> lấy link Listen (action,audio) -> lấy link Downloads/Audio (mediahit) -> decode ra URL mp3 -> tải -> upload.
- State lưu trên Drive: state.json (tránh commit/push -> khỏi dính 403).
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set, Tuple, List
from urllib.parse import urljoin, unquote

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError

# =========================
# Config
# =========================
MINISTRY_URL = "https://www.twr360.org/ministry/23/turning-point-radio/?lang=1"
STATE_DRIVE_NAME = "state.json"

MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "10"))
SLEEP_SECONDS = float(os.getenv("SLEEP_SECONDS", "3"))
TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))

OUT_DIR = Path(os.getenv("OUT_DIR", "out")).resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

SCOPES_DEFAULT = ["https://www.googleapis.com/auth/drive"]

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


# =========================
# HTTP
# =========================
def http_get_text(url: str) -> Tuple[int, str]:
    r = SESSION.get(url, timeout=TIMEOUT)
    return r.status_code, r.text


def download_file(url: str, out_path: Path) -> None:
    with SESSION.get(url, stream=True, timeout=TIMEOUT) as r:
        r.raise_for_status()
        tmp = out_path.with_suffix(out_path.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
        tmp.replace(out_path)


# =========================
# Drive
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

    scopes = info.get("scopes") or info.get("scope")
    if isinstance(scopes, str):
        scopes_list = scopes.split()
    elif isinstance(scopes, list):
        scopes_list = scopes
    else:
        scopes_list = SCOPES_DEFAULT  # fallback

    try:
        return Credentials.from_authorized_user_info(info, scopes=scopes_list)
    except Exception as e:
        print(f"[Drive] Cannot build Credentials: {e}")
        return None


def init_drive():
    creds = load_oauth_from_env()
    if not creds:
        return None
    return build("drive", "v3", credentials=creds, cache_discovery=False)


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
    escaped_name = name.replace("'", "\\'")
    q = f"name='{escaped_name}' and '{folder_id}' in parents and trashed=false"
    res = service.files().list(
        q=q,
        fields="files(id,name,modifiedTime)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = res.get("files") or []
    if not files:
        return None
    files.sort(key=lambda x: x.get("modifiedTime", ""), reverse=True)
    return files[0]["id"]


def drive_download_text(service, file_id: str) -> str:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)
    return fh.read().decode("utf-8", errors="ignore")


def drive_upload_or_update(service, folder_id: str, local_path: Path, remote_name: Optional[str] = None) -> str:
    remote_name = remote_name or local_path.name
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


# =========================
# State (on Drive)
# =========================
def load_state_from_drive(service, folder_id: str) -> dict:
    state = {"seen_program_ids": []}
    if not service or not folder_id:
        return state

    fid = drive_find_by_name(service, folder_id, STATE_DRIVE_NAME)
    if not fid:
        print("[State] No state.json on Drive yet. Start fresh.")
        return state

    try:
        txt = drive_download_text(service, fid)
        obj = json.loads(txt) if txt.strip() else {}
        if isinstance(obj, dict) and "seen_program_ids" in obj:
            return obj
    except Exception as e:
        print(f"[State] Load state failed: {e}")

    return state


def save_state_to_drive(service, folder_id: str, state: dict) -> None:
    if not service or not folder_id:
        return
    tmp = OUT_DIR / "_state_tmp.json"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fid = drive_upload_or_update(service, folder_id, tmp, remote_name=STATE_DRIVE_NAME)
    print(f"[State] Uploaded state.json to Drive (fileId={fid}).")
    try:
        tmp.unlink()
    except Exception:
        pass


# =========================
# TWR360 parsing
# =========================
def extract_episode_list(ministry_html: str) -> List[Episode]:
    """
    Lấy các link Listen từ trang ministry:
    /programs/view/id%2C1146101/action%2Caudio/lang%2C1
    """
    hrefs = re.findall(r'href="([^"]+)"', ministry_html, flags=re.I)
    out: List[Episode] = []
    seen: Set[str] = set()

    for href in hrefs:
        if "programs/view/" not in href:
            continue
        if "action" not in href:
            continue
        if "audio" not in href:
            continue

        full = urljoin("https://www.twr360.org/", href)
        full_dec = unquote(full)

        m = re.search(r"id,(\d+)", full_dec)
        if not m:
            continue
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)

        out.append(Episode(program_id=pid, listen_url=full_dec))

    return out


def parse_title_and_date(html: str) -> Tuple[Optional[str], Optional[str]]:
    title = None
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html, flags=re.I)
    if m:
        title = m.group(1).strip()

    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.I | re.S)
        if m:
            t = re.sub(r"<[^>]+>", "", m.group(1))
            title = re.sub(r"\s+", " ", t).strip() or None

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


def decode_mediahit_to_audio_url(mediahit_url: str) -> Optional[str]:
    """
    mediahit url:
      https://www.twr360.org/mediahit/id%2C6566871/url%2C<base64>~
    hoặc /mediahit/...
    Decode base64 -> direct audio url
    """
    try:
        u = unquote(mediahit_url)
        # bắt cả url, và url%2C
        m = re.search(r"url,([^/?#]+)", u)
        if not m:
            return None
        b64 = m.group(1).strip().replace("~", "=")

        pad = (-len(b64)) % 4
        if pad:
            b64 += "=" * pad

        raw = base64.b64decode(b64.encode("utf-8")).decode("utf-8", errors="ignore").strip()
        return raw if raw.startswith("http") else None
    except Exception:
        return None


def extract_audio_url_from_listen_page(html: str, base_url: str) -> Optional[str]:
    """
    FIX CHÍNH:
    - Bắt cả link mediahit tương đối (/mediahit/...) lẫn tuyệt đối
    - Ưu tiên đúng link có anchor text 'Audio'
    """
    # 1) Ưu tiên <a href="...mediahit...">Audio</a>
    m = re.search(r'<a[^>]+href="([^"]*mediahit[^"]*)"[^>]*>\s*Audio\s*</a>', html, flags=re.I)
    if m:
        href = m.group(1)
        full = urljoin(base_url, href)
        au = decode_mediahit_to_audio_url(full)
        if au:
            return au

    # 2) Fallback: lấy tất cả href chứa mediahit (kể cả tương đối)
    hrefs = re.findall(r'href="([^"]*mediahit[^"]*)"', html, flags=re.I)
    candidates: List[str] = []
    for href in hrefs:
        full = urljoin(base_url, href)
        au = decode_mediahit_to_audio_url(full)
        if not au:
            continue
        low = au.lower()
        if any(low.endswith(ext) for ext in [".mp3", ".m4a", ".aac", ".mp4", ".m4b"]):
            candidates.append(au)

    if not candidates:
        return None

    # 3) Chọn “best”: ưu tiên bitrate lớn hơn nếu có _64/_128...
    def score(u: str) -> Tuple[int, int]:
        ul = u.lower()
        ext_rank = 0
        if ul.endswith(".m4a"):
            ext_rank = 3
        elif ul.endswith(".mp3"):
            ext_rank = 2
        elif ul.endswith(".aac"):
            ext_rank = 1
        br = 0
        mm = re.search(r"[_\-](\d{2,3})\.(mp3|m4a|aac|mp4|m4b)\b", ul)
        if mm:
            try:
                br = int(mm.group(1))
            except Exception:
                br = 0
        return (ext_rank, br)

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def safe_filename(s: str, max_len: int = 180) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len].rstrip()


# =========================
# Main
# =========================
def main() -> int:
    folder_id_env = (os.environ.get("GDRIVE_FOLDER_ID") or "").strip()

    drive = init_drive()
    drive_folder = ensure_folder_access(drive, folder_id_env) if drive and folder_id_env else None

    state = load_state_from_drive(drive, drive_folder) if (drive and drive_folder) else {"seen_program_ids": []}
    seen: Set[str] = set(str(x) for x in (state.get("seen_program_ids") or []))

    print(f"[Fetch] {MINISTRY_URL}")
    st, ministry_html = http_get_text(MINISTRY_URL)
    print(f"[Fetch] status={st} bytes={len(ministry_html.encode('utf-8', errors='ignore'))}")
    if st != 200:
        raise RuntimeError(f"Fetch ministry page failed: HTTP {st}")

    eps = extract_episode_list(ministry_html)
    print(f"[Parse] Found {len(eps)} episode(s) from ministry page.")

    new_eps = [e for e in eps if e.program_id not in seen][:MAX_PER_RUN]
    print(f"[Plan] New episodes this run: {len(new_eps)} (max {MAX_PER_RUN}).")

    uploaded = 0
    failed = 0

    for i, ep in enumerate(new_eps, 1):
        pid = ep.program_id
        print(f"[{i}/{len(new_eps)}] program_id={pid}")

        try:
            st2, listen_html = http_get_text(ep.listen_url)
            if st2 != 200:
                raise RuntimeError(f"listen page HTTP {st2}")

            title, date_iso = parse_title_and_date(listen_html)
            title = title or f"TurningPoint_{pid}"
            date_iso = date_iso or dt.date.today().isoformat()

            audio_url = extract_audio_url_from_listen_page(listen_html, ep.listen_url)
            if not audio_url:
                raise RuntimeError("Cannot find audio mediahit on listen page (Downloads -> Audio).")

            # file ext
            low = audio_url.lower()
            ext = ".mp3"
            for eext in [".m4a", ".mp3", ".aac", ".mp4", ".m4b"]:
                if low.endswith(eext):
                    ext = eext
                    break

            filename = safe_filename(f"{date_iso} - {title} - {pid}{ext}")
            local_path = OUT_DIR / filename

            print(f"[Audio] {audio_url}")
            print(f"[DL] -> {local_path.name}")
            download_file(audio_url, local_path)

            if drive and drive_folder:
                fid = drive_upload_or_update(drive, drive_folder, local_path, remote_name=local_path.name)
                uploaded += 1
                print(f"[Drive] Uploaded: {local_path.name} (fileId={fid})")
                try:
                    local_path.unlink()
                except Exception:
                    pass
            else:
                print("[Drive] Skip upload (missing Drive creds/folder). Kept local file.")

            # mark seen only when success
            seen.add(pid)

        except Exception as e:
            failed += 1
            print(f"[FAIL] program_id={pid}: {e}")

        if i < len(new_eps):
            time.sleep(SLEEP_SECONDS)

    state["seen_program_ids"] = sorted(seen, key=lambda x: int(x) if x.isdigit() else x)
    if drive and drive_folder:
        save_state_to_drive(drive, drive_folder, state)

    print(f"Done. Uploaded {uploaded} episode(s). Seen total={len(seen)}. Failed={failed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
