from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import aiohttp
import yt_dlp
from yt_dlp.utils import DownloadError

from config import YT_COOKIES_FILE, YT_PROXY

CHANNEL_RE = re.compile(r'"channelId":"(UC[\w-]+)"')
ATOM = {"a": "http://www.w3.org/2005/Atom"}


def _base_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }
    if YT_COOKIES_FILE and Path(YT_COOKIES_FILE).is_file():
        opts["cookiefile"] = YT_COOKIES_FILE
    if YT_PROXY:
        opts["proxy"] = YT_PROXY
    return opts


def _ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _channel_url(url: str) -> str:
    target = url.strip()
    if target.startswith("@"):
        target = "https://www.youtube.com/" + target
    elif not target.startswith(("http://", "https://")):
        target = "https://www.youtube.com/@" + target.lstrip("@")
    if "/@" in target and not target.rstrip("/").endswith("/videos"):
        target = target.rstrip("/") + "/videos"
    return target


def _resolve_sync(url: str) -> dict[str, str]:
    target = _channel_url(url)
    opts = _base_opts() | {"extract_flat": True, "playlistend": 1, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    channel_id = str(info.get("channel_id") or "")
    if not channel_id.startswith("UC"):
        candidate = str(info.get("id") or "")
        channel_id = candidate if candidate.startswith("UC") else ""
    return {"channel_id": channel_id, "title": str(info.get("channel") or info.get("title") or "")}


async def resolve_channel(url: str) -> dict[str, str]:
    return await asyncio.to_thread(_resolve_sync, url)


def _channel_from_html_sync(url: str) -> str:
    import urllib.request

    request = urllib.request.Request(_channel_url(url), headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru"})
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", "replace")
    match = CHANNEL_RE.search(html)
    return match.group(1) if match else ""


async def channel_from_html(url: str) -> str:
    try:
        return await asyncio.to_thread(_channel_from_html_sync, url)
    except Exception:
        return ""


def _parse_feed(xml: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(xml)
    out: list[dict[str, Any]] = []
    for entry in root.findall("a:entry", ATOM):
        video_id = (entry.findtext("a:id", default="", namespaces=ATOM) or "").rsplit(":", 1)[-1]
        title = entry.findtext("a:title", default="", namespaces=ATOM) or ""
        published = entry.findtext("a:published", default="", namespaces=ATOM) or ""
        try:
            ts = int(datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp())
        except ValueError:
            ts = int(time.time())
        link = ""
        for node in entry.findall("a:link", ATOM):
            if node.get("rel") == "alternate":
                link = node.get("href") or ""
                break
        if video_id:
            out.append({
                "youtube_id": video_id,
                "title": title,
                "published_at": ts,
                "url": link or f"https://www.youtube.com/watch?v={video_id}",
            })
    return out


async def fetch_feed(session: aiohttp.ClientSession, channel_id: str) -> list[dict[str, Any]]:
    if not channel_id:
        return []
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
            return _parse_feed(await response.read())
    except Exception:
        return []


def _probe_sync(url: str) -> dict[str, Any]:
    opts = _base_opts() | {"skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        info = entries[0] if entries else {}
    height = int(info.get("height") or 0)
    return {
        "youtube_id": str(info.get("id") or ""),
        "title": str(info.get("title") or ""),
        "duration": int(info.get("duration") or 0),
        "height": height,
        "width": int(info.get("width") or 0),
        "uploaded_at": int(info.get("release_timestamp") or info.get("timestamp") or time.time()),
    }


async def probe(url: str) -> dict[str, Any]:
    return await asyncio.to_thread(_probe_sync, url)


def _download_sync(
    url: str,
    quality: int,
    target: Path,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[Path, int, int, int]:
    ffmpeg = _ffmpeg()
    opts = _base_opts() | {
        "format": (
            f"bv*[height<={quality}][vcodec^=avc1]+ba[ext=m4a]/"
            f"bv*[height<={quality}]+ba/b[height<={quality}]/b"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(target.with_suffix("")) + ".%(ext)s",
        "overwrites": True,
    }
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
        opts["postprocessor_args"] = {"merger": ["-movflags", "+faststart"]}
    if progress:
        def hook(data: dict[str, Any]) -> None:
            if data.get("status") == "downloading":
                progress(int(data.get("downloaded_bytes") or 0), int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0))
        opts["progress_hooks"] = [hook]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        info = entries[0] if entries else {}
    candidates = [
        path for path in target.parent.glob(target.stem + ".*")
        if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
        and ".part" not in path.name
    ]
    if not candidates:
        raise DownloadError("файл не создан")
    path = max(candidates, key=lambda item: item.stat().st_size)
    return path, int(info.get("height") or 0), int(info.get("width") or 0), int(info.get("duration") or 0)


async def download(
    url: str,
    quality: int,
    target: Path,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[Path, int, int, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    return await asyncio.to_thread(_download_sync, url, quality, target, progress)

