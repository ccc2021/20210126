from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts" / "localize_youtube.py"
spec = importlib.util.spec_from_file_location("localize_worker", WORKER)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load {WORKER}")
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)

BASE = "http://127.0.0.1:9000"
UA = "zhko-video-localizer/2.0 (+https://github.com/ccc2021/DC_Video)"
VIDEO_ID = worker._video_id_from_url(worker.VIDEO_URL)


def download_stream(url: str, destination: Path) -> None:
    worker._download_stream(url, destination, referer=BASE)
    if not destination.exists() or destination.stat().st_size < 100_000:
        raise RuntimeError(f"Cobalt output too small: {destination}")


def process_response(data: dict, request_url: str) -> Path:
    status = str(data.get("status") or "")
    if status in {"tunnel", "redirect"}:
        media_url = data.get("url")
        if not media_url:
            raise RuntimeError("Cobalt response did not include url")
        output = worker.WORKDIR / "source.local-cobalt.mp4"
        download_stream(str(media_url), output)
        return output

    if status == "picker":
        choices = [x for x in data.get("picker", []) if x.get("type") == "video" and x.get("url")]
        if not choices:
            raise RuntimeError("Cobalt picker did not include video")
        output = worker.WORKDIR / "source.local-cobalt-picker.mp4"
        download_stream(str(choices[0]["url"]), output)
        return output

    if status == "local-processing":
        tunnels = [str(x) for x in data.get("tunnel", []) if x]
        operation = str(data.get("type") or "")
        if not tunnels:
            raise RuntimeError("Cobalt local-processing response had no tunnels")
        parts: list[Path] = []
        for index, tunnel in enumerate(tunnels):
            part = worker.WORKDIR / f"source.local-cobalt.part{index}"
            download_stream(tunnel, part)
            parts.append(part)
        output = worker.WORKDIR / "source.local-cobalt.mkv"
        if operation == "merge" and len(parts) >= 2:
            worker.run([
                "ffmpeg", "-y", "-i", str(parts[0]), "-i", str(parts[1]),
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest", str(output),
            ])
        elif operation == "mute":
            worker.run(["ffmpeg", "-y", "-i", str(parts[0]), "-an", "-c:v", "copy", str(output)])
        else:
            worker.run(["ffmpeg", "-y", "-i", str(parts[0]), "-c", "copy", str(output)])
        if not output.exists() or output.stat().st_size < 100_000:
            raise RuntimeError("Cobalt local-processing output is too small")
        return output

    raise RuntimeError(json.dumps(data, ensure_ascii=False)[:2000])


def download_local_cobalt():
    source_urls = [
        worker.VIDEO_URL,
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://music.youtube.com/watch?v={VIDEO_ID}",
    ]
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    errors: list[str] = []
    info_response = requests.get(BASE + "/", headers={"User-Agent": UA}, timeout=(5, 20))
    print(f"Local Cobalt info: {info_response.status_code} {info_response.text[:1000]}", flush=True)
    for source_url in source_urls:
        for local_mode in ("disabled", "preferred"):
            payload = {
                "url": source_url,
                "downloadMode": "auto",
                "videoQuality": str(worker.MAX_HEIGHT),
                "youtubeVideoCodec": "h264",
                "youtubeVideoContainer": "mp4",
                "youtubeBetterAudio": False,
                "youtubeHLS": False,
                "audioFormat": "best",
                "filenameStyle": "basic",
                "alwaysProxy": True,
                "localProcessing": local_mode,
            }
            try:
                print(f"Trying self-hosted Cobalt: {source_url} localProcessing={local_mode}", flush=True)
                response = requests.post(BASE + "/", headers=headers, json=payload, timeout=(15, 300))
                text = response.text
                print(f"Self-hosted Cobalt response: HTTP {response.status_code} {text[:1800]}", flush=True)
                response.raise_for_status()
                data = response.json()
                output = process_response(data, source_url)
                return output, {
                    "id": VIDEO_ID,
                    "title": data.get("filename") or VIDEO_ID,
                    "duration": None,
                    "webpage_url": worker.VIDEO_URL,
                    "extractor": f"self-hosted-cobalt:{source_url}",
                }
            except Exception as exc:
                errors.append(f"{source_url} ({local_mode}): {exc}")
    raise RuntimeError("Self-hosted Cobalt failed:\n" + "\n".join(errors))


original_download = worker.download_video


def robust_download():
    errors: list[str] = []
    try:
        return download_local_cobalt()
    except Exception as exc:
        errors.append(str(exc))
        print(str(exc), file=sys.stderr, flush=True)
    try:
        return original_download()
    except Exception as exc:
        errors.append(f"Original yt-dlp/Piped routes: {exc}")
    raise RuntimeError("Every download route failed:\n" + "\n".join(errors))


worker.download_video = robust_download
worker.main()
