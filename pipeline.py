"""Theme-to-MP4 extraction, download, normalization, and stitching pipeline."""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Literal
from urllib.parse import parse_qs, urlparse

from llm_clients import generate_segment_json, repair_segment_json

Provider = Literal["OpenAI API", "Local Ollama"]
LogCallback = Callable[[str], None]
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}


@dataclass(frozen=True)
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _log(callback: LogCallback | None, message: str) -> None:
    if callback:
        callback(message)


def check_system_dependencies() -> dict[str, str | None]:
    """Return resolved executable paths. A missing tool maps to None."""
    return {name: shutil.which(name) for name in ("ffmpeg", "ffprobe", "yt-dlp")}


def parse_video_id(url: str) -> str:
    """Extract a standard 11-character YouTube video ID from common URL forms."""
    candidate = url.strip()
    if not candidate:
        raise ValueError("Empty URL")
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    video_id = ""

    if host == "youtu.be" or host.endswith(".youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        else:
            match = re.match(r"^/(?:shorts|embed|live|v)/([^/?#]+)", parsed.path)
            if match:
                video_id = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        raise ValueError("Could not identify a standard YouTube video ID. Use a watch, youtu.be, shorts, live, or embed URL.")
    return video_id


def parse_url_list(raw_text: str) -> list[str]:
    """Accept one URL per line, or URLs separated by commas/semicolons/whitespace."""
    found = re.findall(r"https?://[^\s,;]+", raw_text or "", flags=re.IGNORECASE)
    if not found:
        found = re.split(r"[\s,;]+", (raw_text or "").strip())
    urls: list[str] = []
    seen: set[str] = set()
    for item in found:
        url = item.strip().rstrip(")]>.,;")
        if url and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def fetch_transcript(video_id: str, languages: list[str]) -> tuple[list[dict[str, float | str]], float, str]:
    """Fetch transcript captions and return timestamped snippets, duration, language code."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise RuntimeError("youtube-transcript-api is not installed. Run: python -m pip install -r requirements.txt") from exc
    fetched = YouTubeTranscriptApi().fetch(video_id, languages=languages)
    snippets: list[dict[str, float | str]] = []
    for item in fetched:
        text = re.sub(r"\s+", " ", str(item.text)).strip()
        if not text:
            continue
        start = max(0.0, float(item.start))
        end = max(start, float(item.start) + float(item.duration))
        snippets.append({"text": text, "start": start, "end": end})
    if not snippets:
        raise RuntimeError("The transcript was empty.")
    duration = max(float(item["end"]) for item in snippets)
    return snippets, duration, str(getattr(fetched, "language_code", "unknown"))


def _format_clock(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remain = divmod(total_ms, 3_600_000)
    minutes, remain = divmod(remain, 60_000)
    secs, millis = divmod(remain, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _make_transcript_chunks(
    snippets: list[dict[str, float | str]], max_chars: int = 22_000, overlap_snippets: int = 3
) -> list[str]:
    """Split long transcripts by caption boundaries with a small overlap."""
    lines = [
        f"[{float(s['start']):.3f} - {float(s['end']):.3f}] {str(s['text'])}"
        for s in snippets
    ]
    chunks: list[str] = []
    start_index = 0
    while start_index < len(lines):
        end_index = start_index
        chars = 0
        while end_index < len(lines):
            next_size = len(lines[end_index]) + 1
            if end_index > start_index and chars + next_size > max_chars:
                break
            chars += next_size
            end_index += 1
        chunks.append("\n".join(lines[start_index:end_index]))
        if end_index >= len(lines):
            break
        start_index = max(start_index + 1, end_index - overlap_snippets)
    return chunks


def parse_model_segments(raw: str, video_duration: float) -> list[Segment]:
    """Parse and validate a model JSON array, tolerating a segments object wrapper."""
    cleaned = (raw or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Recover only a complete JSON array; do not attempt to interpret prose as timestamps.
        left, right = cleaned.find("["), cleaned.rfind("]")
        if left < 0 or right <= left:
            raise ValueError("The LLM response was not a valid JSON array.")
        data = json.loads(cleaned[left : right + 1])

    if isinstance(data, dict):
        data = data.get("segments")
    if not isinstance(data, list):
        raise ValueError("The LLM JSON must be an array of {start, end} objects.")

    segments: list[Segment] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        start_raw = item.get("start", item.get("start_seconds"))
        end_raw = item.get("end", item.get("end_seconds"))
        if start_raw is None or end_raw is None:
            continue
        try:
            start = _parse_timestamp(start_raw)
            end = _parse_timestamp(end_raw)
        except (TypeError, ValueError):
            continue
        # Reject non-finite values and only keep segments grounded within the transcript's known duration.
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        if start < 0 or end <= start or start >= video_duration:
            continue
        end = min(end, video_duration)
        if end > start:
            segments.append(Segment(round(start, 3), round(end, 3)))

    return _merge_segments(segments)


def _parse_timestamp(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid timestamp: {text}")
    seconds = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[-3]) if len(parts) == 3 else 0
    if seconds < 0 or seconds >= 60 or minutes < 0 or minutes >= 60 and len(parts) == 3 or hours < 0:
        raise ValueError(f"Invalid timestamp: {text}")
    return hours * 3600 + minutes * 60 + seconds


def _merge_segments(segments: Iterable[Segment], gap_tolerance: float = 0.35) -> list[Segment]:
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    if not ordered:
        return []
    merged: list[Segment] = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start <= previous.end + gap_tolerance:
            merged[-1] = Segment(previous.start, max(previous.end, current.end))
        else:
            merged.append(current)
    return merged


def _analyze_transcript(
    *,
    theme: str,
    snippets: list[dict[str, float | str]],
    video_duration: float,
    provider: Provider,
    model: str,
    api_key: str | None,
    ollama_base_url: str,
    log: LogCallback | None,
) -> list[Segment]:
    all_segments: list[Segment] = []
    chunks = _make_transcript_chunks(snippets)
    for chunk_index, transcript_chunk in enumerate(chunks, start=1):
        _log(log, f"Semantic filtering: transcript chunk {chunk_index}/{len(chunks)}")
        raw = generate_segment_json(
            provider=provider,
            model=model,
            theme=theme,
            transcript_excerpt=transcript_chunk,
            video_duration=video_duration,
            api_key=api_key,
            ollama_base_url=ollama_base_url,
        )
        try:
            parsed = parse_model_segments(raw, video_duration)
        except ValueError as exc:
            # Retry once with a formatting repair prompt; no transcript timestamps are changed.
            _log(log, f"LLM response needed JSON repair: {exc}")
            repaired = repair_segment_json(
                provider=provider,
                model=model,
                raw_response=raw,
                api_key=api_key,
                ollama_base_url=ollama_base_url,
            )
            parsed = parse_model_segments(repaired, video_duration)
        all_segments.extend(parsed)
    return _merge_segments(all_segments)


def _download_segment(
    *, url: str, video_index: int, segment_index: int, segment: Segment, raw_dir: Path
) -> Path:
    prefix = f"video_{video_index:02d}_segment_{segment_index:03d}"
    output_template = str(raw_dir / f"{prefix}.%(ext)s")
    section = f"*{_format_clock(segment.start)}-{_format_clock(segment.end)}"
    command = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "--force-overwrites",
        "--download-sections",
        section,
        "--force-keyframes-at-cuts",
        "--format",
        "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format",
        "mp4",
        "--output",
        output_template,
        url,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"yt-dlp timed out while downloading segment {segment_index}.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "No command output").strip()[-3000:]
        raise RuntimeError(f"yt-dlp failed for {section}: {detail}")

    candidates = [
        path for path in raw_dir.glob(f"{prefix}.*")
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS and path.stat().st_size > 0
    ]
    if not candidates:
        raise RuntimeError(f"yt-dlp reported success but no media file was found for {section}.")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _probe_media(source: Path) -> tuple[float, bool]:
    command = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(source)
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe could not inspect {source.name}: {(result.stderr or '').strip()[-1000:]}")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"Could not determine duration for {source.name}.") from exc
    audio_command = [
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(source)
    ]
    audio_result = subprocess.run(audio_command, capture_output=True, text=True, timeout=60, check=False)
    if audio_result.returncode != 0:
        raise RuntimeError(f"ffprobe could not inspect audio in {source.name}.")
    return duration, bool(audio_result.stdout.strip())


def _normalize_clip(source: Path, destination: Path) -> None:
    duration, has_audio = _probe_media(source)
    if duration <= 0:
        raise RuntimeError(f"Downloaded segment {source.name} has no valid duration.")
    video_filter = (
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30"
    )
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    if not has_audio:
        command.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
    command.extend(["-map", "0:v:0"])
    command.extend(["-map", "0:a:0" if has_audio else "1:a:0"])
    command.extend([
        "-vf", video_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-t", f"{duration:.3f}", "-movflags", "+faststart", str(destination),
    ])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg timed out while normalizing {source.name}.") from exc
    if result.returncode != 0 or not destination.exists() or destination.stat().st_size == 0:
        detail = (result.stderr or "No ffmpeg error output").strip()[-3000:]
        raise RuntimeError(f"ffmpeg failed to normalize {source.name}: {detail}")


def _concat_clips(clip_paths: list[Path], output_path: Path, work_dir: Path) -> None:
    if not clip_paths:
        raise ValueError("There are no normalized clips to concatenate.")
    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], output_path)
        return

    concat_list = work_dir / "concat_list.txt"
    with concat_list.open("w", encoding="utf-8", newline="\n") as handle:
        for clip in clip_paths:
            path = str(clip.resolve()).replace("\\", "\\\\").replace("'", "'\\''")
            handle.write(f"file '{path}'\n")

    copy_command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", "-movflags", "+faststart", str(output_path),
    ]
    result = subprocess.run(copy_command, capture_output=True, text=True, timeout=1800, check=False)
    if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
        return

    # Fallback: re-encode the joined stream for edge cases where stream-copy concat fails.
    if output_path.exists():
        output_path.unlink()
    fallback = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(output_path),
    ]
    fallback_result = subprocess.run(fallback, capture_output=True, text=True, timeout=2400, check=False)
    if fallback_result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        detail = (fallback_result.stderr or result.stderr or "No ffmpeg error output").strip()[-3000:]
        raise RuntimeError(f"ffmpeg could not concatenate the normalized clips: {detail}")


def run_pipeline(
    *,
    theme: str,
    urls: list[str],
    provider: Provider,
    model: str,
    languages: list[str],
    api_key: str | None = None,
    ollama_base_url: str = "http://localhost:11434",
    output_root: str | Path = "outputs",
    log: LogCallback | None = None,
) -> dict:
    """Run every URL sequentially; return paths and per-video results."""
    deps = check_system_dependencies()
    missing = [name for name, path in deps.items() if path is None]
    if missing:
        raise EnvironmentError(
            "Missing required system executable(s): " + ", ".join(missing) + ". Install them and ensure they are in PATH, then restart the terminal/app."
        )
    if not theme.strip():
        raise ValueError("Enter a theme before starting the pipeline.")
    if not urls:
        raise ValueError("Paste at least one YouTube URL.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(output_root) / f"run_{stamp}_{uuid.uuid4().hex[:6]}"
    raw_dir = run_dir / "raw"
    normalized_dir = run_dir / "normalized"
    raw_dir.mkdir(parents=True, exist_ok=False)
    normalized_dir.mkdir(parents=True, exist_ok=False)

    results: list[dict] = []
    normalized_clips: list[Path] = []
    clip_manifest: list[dict] = []

    for video_index, url in enumerate(urls, start=1):
        _log(log, f"\n[{video_index}/{len(urls)}] Processing {url}")
        video_result: dict = {
            "url": url,
            "video_id": None,
            "status": "failed",
            "transcript_language": None,
            "segments_found": 0,
            "clips_created": 0,
            "errors": [],
        }
        try:
            video_id = parse_video_id(url)
            video_result["video_id"] = video_id
            _log(log, f"Transcript extraction: fetching captions for video {video_id} (no full video download)")
            snippets, duration, transcript_language = fetch_transcript(video_id, languages)
            video_result["transcript_language"] = transcript_language
            _log(log, f"Fetched {len(snippets)} transcript entries; caption duration is about {duration:.1f} seconds")

            segments = _analyze_transcript(
                theme=theme.strip(),
                snippets=snippets,
                video_duration=duration,
                provider=provider,
                model=model,
                api_key=api_key,
                ollama_base_url=ollama_base_url,
                log=log,
            )
            video_result["segments_found"] = len(segments)
            if not segments:
                video_result["status"] = "no_matches"
                _log(log, "No relevant segments found for this theme.")
                results.append(video_result)
                continue

            for segment_index, segment in enumerate(segments, start=1):
                _log(log, f"Targeted download: {segment_index}/{len(segments)} ({_format_clock(segment.start)}–{_format_clock(segment.end)})")
                try:
                    downloaded = _download_segment(
                        url=url,
                        video_index=video_index,
                        segment_index=segment_index,
                        segment=segment,
                        raw_dir=raw_dir,
                    )
                    normalized_path = normalized_dir / f"clip_{video_index:02d}_{segment_index:03d}.mp4"
                    _log(log, f"Normalizing: {downloaded.name} → {normalized_path.name} (1920×1080, 30 fps, H.264/AAC)")
                    _normalize_clip(downloaded, normalized_path)
                    normalized_clips.append(normalized_path)
                    clip_manifest.append({
                        "source_url": url,
                        "video_id": video_id,
                        "segment_index": segment_index,
                        "start_seconds": segment.start,
                        "end_seconds": segment.end,
                        "duration_seconds": round(segment.duration, 3),
                        "normalized_file": str(normalized_path),
                    })
                    video_result["clips_created"] += 1
                except Exception as exc:  # continue to other matched segments/videos on individual failures
                    message = f"Segment {segment_index} ({segment.start:.3f}–{segment.end:.3f}s): {exc}"
                    video_result["errors"].append(message)
                    _log(log, f"ERROR: {message}")
            if video_result["clips_created"] and video_result["errors"]:
                video_result["status"] = "partial"
            else:
                video_result["status"] = "completed" if video_result["clips_created"] else "failed"
        except Exception as exc:
            video_result["errors"].append(str(exc))
            _log(log, f"ERROR: {exc}")
        results.append(video_result)

    final_path: Path | None = None
    compilation_error: str | None = None
    if normalized_clips:
        proposed_path = run_dir / "final_compilation.mp4"
        _log(log, f"Stitching {len(normalized_clips)} normalized clips into final_compilation.mp4")
        try:
            _concat_clips(normalized_clips, proposed_path, run_dir)
            final_path = proposed_path
            _log(log, f"Finished: {final_path}")
        except Exception as exc:
            compilation_error = str(exc)
            _log(log, f"ERROR stitching clips: {compilation_error}")
    else:
        _log(log, "No clips were created, so no MP4 compilation was produced.")

    manifest = {
        "theme": theme.strip(),
        "provider": provider,
        "model": model,
        "languages_requested": languages,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "system_dependencies": deps,
        "results": results,
        "clips": clip_manifest,
        "final_compilation": str(final_path) if final_path else None,
        "compilation_error": compilation_error,
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "run_dir": run_dir,
        "final_path": final_path,
        "manifest_path": manifest_path,
        "results": results,
        "clips": clip_manifest,
        "dependencies": deps,
        "compilation_error": compilation_error,
    }
