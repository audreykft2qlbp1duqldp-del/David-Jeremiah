import os, re, json, base64, hashlib, io
from datetime import datetime
from urllib.parse import urljoin

import requests
import feedparser
from bs4 import BeautifulSoup

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# ==== SOURCE (public RSS) ====
RSS_URL = "https://www.twr360.org/programs/rss/ministry_id%2C23"

# ==== LOCAL TEMP ====
TMP_DIR = ".tmp_dl"
UA = "Mozilla/5.0 (GitHubActions; +https://github.com/)"

# ==== DRIVE AUTH SCOPE (GIỐNG CODE CỦA BẠN) ====
SCOPES = ["https://www.googleapis.com/auth/drive"]

# ==== STATE LƯU TRÊN DRIVE (TRONG CÙNG FOLDER) ====
STATE_DRIVE_NAME = "turningpoint_state.json"


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def safe_name(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s).strip()
    return re.sub(r"\s+", " ", s)[:160]


def decode_mediahit_url(href: str) -> str | None:
    """
    TWR360 đôi khi đưa link dạng /mediahit/.../url,<base64>
    Giải base64 để lấy URL thật.
    """
    m = re.search(r"/mediahit/.*?/url,([^/?#]+)", href)
    if not m:
        return None
    b64 = m.group(1).replace("-", "+").replace("_", "/")
    pad = "=" * ((4 - (len(b64) % 4)) % 4)
    try:
        raw = base64.b64decode(b64 + pad).decode("utf-8", errors="ignore")
        return raw if raw.startswith("http") else None
    except Exception:
        return None


def extract_audio_url_from_program_page(session: requests.Session, page_url: str) -> str:
    """
    Vào trang episode trên TWR360 -> tìm Downloads -> Audio -> lấy link mp3 thật.
    """
    r = session.get(page_url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    a = soup.find("a", string=re.compile(r"^\s*Audio\s*$", re.I))
    if not a or not a.get("href"):
        raise RuntimeError("Không tìm thấy link Downloads -> Audio trên trang tập.")

    href = urljoin(page_url, a["href"])

    decoded = decode_mediahit_url(href)
    if decoded:
        return decoded

    rr = session.get(href, allow_redirects=True, timeout=30)
    rr.raise_for_status()
    return rr.url


def get_drive_service():
    folder_id = os.getenv("GDRIVE_FOLDER_ID", "").strip()
    token_json = os.getenv("GDRIVE_OAUTH_TOKEN_JSON", "").strip()

    if not folder_id:
        raise RuntimeError("Thiếu env GDRIVE_FOLDER_ID")
    if not token_json:
        raise RuntimeError("Thiếu env GDRIVE_OAUTH_TOKEN_JSON")

    info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(info, SCOPES)  # GIỐNG code của bạn

    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return service, folder_id


def drive_find_by_name(service, folder_id: str, filename: str) -> str | None:
    # Escape dấu nháy đơn cho Drive query
    escaped_name = filename.replace("'", "\\'")
    q = f"name = '{escaped_name}' and '{folder_id}' in parents and trashed = false"

    res = service.files().list(
        q=q,
        fields="files(id,name)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = res.get("files", [])
    return files[0]["id"] if files else None


def drive_download_json(service, file_id: str) -> dict:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)
    return json.loads(fh.read().decode("utf-8"))


def drive_upload_json(service, folder_id: str, filename: str, data: dict, existing_id: str | None):
    os.makedirs(TMP_DIR, exist_ok=True)
    local_path = os.path.join(TMP_DIR, filename)

    with open(local_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    media = MediaFileUpload(local_path, mimetype="application/json", resumable=True)

    if existing_id:
        service.files().update(
            fileId=existing_id,
            media_body=media,
            supportsAllDrives=True,
        ).execute()
    else:
        service.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()

    try:
        os.remove(local_path)
    except Exception:
        pass


def drive_upload_file(service, folder_id: str, local_path: str, filename: str) -> str:
    existed_id = drive_find_by_name(service, folder_id, filename)
    if existed_id:
        print(f"[SKIP] Đã tồn tại trên Drive: {filename} (id={existed_id})")
        return existed_id

    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype="audio/mpeg", resumable=True)

    created = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id,webViewLink",
        supportsAllDrives=True,
    ).execute()

    fid = created["id"]
    print(f"[OK] Uploaded: {filename} (id={fid})")
    if "webViewLink" in created:
        print(f"      Link: {created['webViewLink']}")
    return fid


def main():
    os.makedirs(TMP_DIR, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    service, folder_id = get_drive_service()

    # ==== Load state từ Drive ====
    state_id = drive_find_by_name(service, folder_id, STATE_DRIVE_NAME)
    if state_id:
        state = drive_download_json(service, state_id)
    else:
        state = {"seen": []}

    seen = set(state.get("seen", []))

    # ==== Fetch RSS bằng requests rồi parse (ổn định hơn parse thẳng URL) ====
    rss_resp = session.get(RSS_URL, timeout=30)
    rss_resp.raise_for_status()
    feed = feedparser.parse(rss_resp.content)

    print(f"[RSS] status={rss_resp.status_code} bytes={len(rss_resp.content)} entries={len(feed.entries)}")
    if len(feed.entries) == 0:
        raise RuntimeError("RSS parse ra 0 entries. Có thể RSS thay đổi hoặc bị chặn tạm thời.")

    uploaded = 0

    for e in feed.entries:
        title = (e.get("title", "untitled") or "untitled").strip()
        link = (e.get("link", "") or "").strip()
        guid = e.get("id") or e.get("guid") or (link + "|" + title)
        key = sha1(guid)

        if key in seen or not link:
            continue

        # Date prefix cho tên file
        try:
            dt = datetime(*e.published_parsed[:6]) if getattr(e, "published_parsed", None) else datetime.utcnow()
        except Exception:
            dt = datetime.utcnow()
        date_prefix = dt.strftime("%Y-%m-%d")

        audio_url = extract_audio_url_from_program_page(session, link)

        filename = f"{date_prefix} - {safe_name(title)}.mp3"
        local_path = os.path.join(TMP_DIR, filename)

        # Download mp3
        with session.get(audio_url, stream=True, timeout=60) as rr:
            rr.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in rr.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)

        # Upload Drive
        drive_upload_file(service, folder_id, local_path, filename)

        # Mark seen sau khi upload OK
        seen.add(key)
        uploaded += 1

        # Dọn file local
        try:
            os.remove(local_path)
        except Exception:
            pass

    # ==== Save state lại lên Drive ====
    state["seen"] = sorted(seen)
    drive_upload_json(service, folder_id, STATE_DRIVE_NAME, state, state_id)

    print(f"Done. Uploaded {uploaded} new episode(s).")


if __name__ == "__main__":
    main()
