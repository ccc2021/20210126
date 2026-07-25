from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts" / "localize_youtube.py"
spec = importlib.util.spec_from_file_location("localize_worker", WORKER)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load {WORKER}")
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)

UA = "zhko-video-localizer/2.0 (+https://github.com/ccc2021/DC_Video)"
VIDEO_ID = worker._video_id_from_url(worker.VIDEO_URL)


def cobalt_instances() -> list[str]:
    result = ["https://cobalt-api.meowing.de", "https://capi.3kh0.net"]
    try:
        response = requests.get(
            "https://instances.cobalt.best/instances.json",
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=(10, 25),
        )
        response.raise_for_status()
        for row in response.json():
            if not isinstance(row, dict) or not row.get("online"):
                continue
            if (row.get("services") or {}).get("youtube") is not True:
                continue
            if (row.get("info") or {}).get("auth") is True:
                continue
            api = str(row.get("api") or "").strip()
            if not api:
                continue
            if not api.startswith(("http://", "https://")):
                api = f"{row.get('protocol') or 'https'}://{api}"
            api = api.rstrip("/")
            if api not in result:
                result.append(api)
    except Exception as exc:
        print(f"Cobalt instance discovery failed: {exc}", file=sys.stderr, flush=True)
    return result


def download_cobalt():
    payload = {
        "url": worker.VIDEO_URL,
        "downloadMode": "auto",
        "videoQuality": str(worker.MAX_HEIGHT),
        "youtubeVideoCodec": "h264",
        "youtubeVideoContainer": "mp4",
        "audioFormat": "best",
        "filenameStyle": "basic",
        "alwaysProxy": True,
        "localProcessing": "disabled",
    }
    headers = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
    errors = []
    for base in cobalt_instances():
        try:
            print(f"Trying Cobalt: {base}", flush=True)
            response = requests.post(base + "/", headers=headers, json=payload, timeout=(15, 150))
            response.raise_for_status()
            data = response.json()
            status = str(data.get("status") or "")
            media_url = None
            filename = data.get("filename")
            if status in {"tunnel", "redirect"}:
                media_url = data.get("url")
            elif status == "picker":
                choices = [x for x in data.get("picker", []) if x.get("type") == "video" and x.get("url")]
                if choices:
                    media_url = choices[0]["url"]
            if not media_url:
                raise RuntimeError(json.dumps(data, ensure_ascii=False)[:1200])
            output = worker.WORKDIR / "source.cobalt.mp4"
            worker._download_stream(str(media_url), output, referer=base)
            if output.stat().st_size < 100_000:
                raise RuntimeError("downloaded file too small")
            return output, {
                "id": VIDEO_ID,
                "title": filename or VIDEO_ID,
                "duration": None,
                "webpage_url": worker.VIDEO_URL,
                "extractor": f"cobalt:{base}",
            }
        except Exception as exc:
            errors.append(f"{base}: {exc}")
            print(f"Cobalt failed: {base}: {exc}", file=sys.stderr, flush=True)
    raise RuntimeError("All Cobalt instances failed:\n" + "\n".join(errors))


def invidious_instances() -> list[str]:
    result = [
        "https://inv.nadeko.net",
        "https://invidious.nerdvpn.de",
        "https://yt.chocolatemoo53.com",
    ]
    try:
        response = requests.get(
            "https://api.invidious.io/instances.json?sort_by=health",
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=(10, 30),
        )
        response.raise_for_status()
        for item in response.json():
            if not isinstance(item, list) or len(item) < 2:
                continue
            host, meta = item[0], item[1] or {}
            if meta.get("api") is not True:
                continue
            uri = str(meta.get("uri") or f"https://{host}").rstrip("/")
            if uri not in result:
                result.append(uri)
    except Exception as exc:
        print(f"Invidious instance discovery failed: {exc}", file=sys.stderr, flush=True)
    return result


def save_invidious_caption(data: dict, base: str) -> None:
    captions = data.get("captions") or []
    order = ("zh-TW", "zh-Hant", "zh-CN", "zh-Hans", "zh")
    urls: list[str] = []
    for code in order:
        match = next(
            (x for x in captions if str(x.get("languageCode") or x.get("language_code") or "").lower() == code.lower()),
            None,
        )
        if match and match.get("url"):
            urls.append(urljoin(base + "/", str(match["url"])))
            break
    urls.extend(f"{base}/api/v1/captions/{VIDEO_ID}?lang={code}" for code in order)
    for url in urls:
        try:
            raw = worker.WORKDIR / "source.invidious.zh.vtt"
            worker._download_stream(url, raw, referer=base)
            target = worker.WORKDIR / "source.invidious.zh.srt"
            result = worker.subprocess.run(
                ["ffmpeg", "-y", "-i", str(raw), str(target)],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and target.exists() and target.stat().st_size > 20:
                print(f"Downloaded Chinese captions through Invidious: {url}", flush=True)
                return
        except Exception:
            continue


def download_invidious():
    errors = []
    for base in invidious_instances():
        try:
            endpoint = f"{base}/api/v1/videos/{VIDEO_ID}?local=true&region=TW"
            print(f"Trying Invidious: {endpoint}", flush=True)
            response = requests.get(endpoint, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=(15, 75))
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            muxed = [x for x in data.get("formatStreams", []) if x.get("url")]
            eligible = [x for x in muxed if 0 < worker._stream_height(x) <= worker.MAX_HEIGHT] or muxed
            if eligible:
                chosen = max(eligible, key=lambda x: (worker._stream_height(x), worker._stream_bitrate(x)))
                output = worker.WORKDIR / "source.invidious.mp4"
                worker._download_stream(urljoin(base + "/", str(chosen["url"])), output, referer=base)
            else:
                adaptive = [x for x in data.get("adaptiveFormats", []) if x.get("url")]
                videos = [x for x in adaptive if str(x.get("type") or "").startswith("video/")]
                audios = [x for x in adaptive if str(x.get("type") or "").startswith("audio/")]
                eligible_video = [x for x in videos if 0 < worker._stream_height(x) <= worker.MAX_HEIGHT] or videos
                if not eligible_video or not audios:
                    raise RuntimeError("no usable formats")
                video = max(eligible_video, key=lambda x: (worker._stream_height(x), worker._stream_bitrate(x)))
                audio = max(audios, key=worker._stream_bitrate)
                raw_video = worker.WORKDIR / "source.invidious.video"
                raw_audio = worker.WORKDIR / "source.invidious.audio"
                worker._download_stream(urljoin(base + "/", str(video["url"])), raw_video, referer=base)
                worker._download_stream(urljoin(base + "/", str(audio["url"])), raw_audio, referer=base)
                output = worker.WORKDIR / "source.invidious.mkv"
                worker.run([
                    "ffmpeg", "-y", "-i", str(raw_video), "-i", str(raw_audio),
                    "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest", str(output),
                ])
            if output.stat().st_size < 100_000:
                raise RuntimeError("downloaded file too small")
            save_invidious_caption(data, base)
            return output, {
                "id": VIDEO_ID,
                "title": data.get("title") or VIDEO_ID,
                "duration": data.get("lengthSeconds"),
                "webpage_url": worker.VIDEO_URL,
                "extractor": f"invidious:{base}",
            }
        except Exception as exc:
            errors.append(f"{base}: {exc}")
            print(f"Invidious failed: {base}: {exc}", file=sys.stderr, flush=True)
    raise RuntimeError("All Invidious instances failed:\n" + "\n".join(errors))


original_download = worker.download_video


def robust_download():
    errors = []
    try:
        return original_download()
    except Exception as exc:
        errors.append(f"yt-dlp/Piped: {exc}")
    for name, function in (("Cobalt", download_cobalt), ("Invidious", download_invidious)):
        try:
            print(f"Activating {name} fallback", flush=True)
            return function()
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError("Every download route failed:\n" + "\n".join(errors))


worker.download_video = robust_download
worker.main()
