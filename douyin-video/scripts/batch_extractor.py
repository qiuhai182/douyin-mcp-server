#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Batch extract transcripts from all videos of a douyin author profile.

Features:
- Reads the author's public video list via browser automation
  (profile_fetcher.py)
- Skips videos already extracted before (history.json dedup) so re-runs
  only process new uploads (incremental)
- Reuses the multi-backend speech-to-text stack (asr_backends.py)
- Progress callback for WebUI/CLI consumption

History file layout (history.json):
{
  "<aweme_id>": {"title": ..., "extracted_at": ..., "output": ...}
}
"""

import time
import json
import shutil
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from profile_fetcher import (
    fetch_profile_videos, is_profile_url,
    normalize_profile_url, HEADERS,
)

HISTORY_FILE = Path(__file__).resolve().parent.parent.parent / "history.json"
PROFILE_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "profile_cache.json"

# Pause control for the batch pipeline (run gate): set = running,
# cleared = paused. Checked BEFORE each video starts, so an in-flight
# video always finishes (pause takes effect at the next video boundary).
run_gate = threading.Event()


class TranscriptHistory:
    """Persistent record of extracted videos for dedup across runs.

    Each entry may carry a "status" field:
      "ok"  - transcript extracted successfully (default for old records)
      "fail"- extraction failed; retried on the next run instead of skipped
    """

    def __init__(self, path: Path = HISTORY_FILE):
        self.path = Path(path)
        self._data = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def has(self, aweme_id: str) -> bool:
        """True only for successfully extracted videos (failures retried)."""
        entry = self._data.get(aweme_id)
        return bool(entry) and entry.get("status", "ok") == "ok"

    def add(self, aweme_id: str, title: str = "", output: str = ""):
        self._data[aweme_id] = {
            "title": title,
            "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "output": output,
            "status": "ok",
        }
        self._flush()

    def mark_fail(self, aweme_id: str, title: str = "", error: str = ""):
        """Record a failed attempt so stats show it, but next run retries."""
        self._data[aweme_id] = {
            "title": title,
            "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "output": "",
            "status": "fail",
            "error": (error or "")[:300],
        }
        self._flush()

    def _flush(self):
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def __len__(self):
        return len(self._data)

    def counts(self) -> dict:
        ok = fail = 0
        for entry in self._data.values():
            if entry.get("status", "ok") == "ok":
                ok += 1
            else:
                fail += 1
        return {"ok": ok, "fail": fail}


class ProfileCache:
    """Cache of author video lists keyed by canonical profile URL.

    Lets a re-run fix previously failed videos WITHOUT re-opening the
    browser to scroll the whole profile again.
    """

    def __init__(self, path: Path = PROFILE_CACHE_FILE):
        self.path = Path(path)
        self._data = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def get(self, profile_url: str, max_age_hours: float = 72) -> Optional[dict]:
        entry = self._data.get(profile_url)
        if not entry:
            return None
        cached_at = entry.get("cached_at_ts", 0)
        if max_age_hours and (time.time() - cached_at) > max_age_hours * 3600:
            return None
        return entry

    def put(self, profile_url: str, profile: dict):
        self._data[profile_url] = {
            **profile,
            "cached_at_ts": time.time(),
            "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _read_complete_transcript(path: Path) -> Optional[str]:
    """Return the transcript text if the md file is COMPLETE, else None.

    A file counts as complete when the final '## Transcript' section exists
    and holds non-empty text. Combined with the atomic .part-rename write
    (_write_transcript_md), a half-written file is never mistaken for a
    finished transcript.
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
    except Exception:
        return None
    if "## Transcript" not in content:
        return None
    tail = content.split("## Transcript", 1)[1].strip()
    return tail or None


def _write_transcript_md(path: Path, title: str, aweme_id: str,
                         author: str, text: str):
    """Write a transcript md atomically: .part file first, then rename, so
    a crash mid-write can never leave a partial file that looks complete."""
    path = Path(path)
    part = path.with_suffix(".md.part")
    with open(part, 'w', encoding='utf-8') as f:
        f.write(f"# {title}\n\n")
        f.write("| Attribute | Value |\n|---|---|\n")
        f.write(f"| Video ID | `{aweme_id}` |\n")
        f.write(f"| Author | {author} |\n")
        f.write(f"| Extracted at | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |\n\n")
        f.write(f"---\n\n## Transcript\n\n{text}\n")
    part.replace(path)


CATALOG_NAME = "0-目录.txt"

# Transcript files are named "N-<aweme_id>.md" (N = per-author serial
# number) so the file list reads in catalog order. A trailing digit run in
# the stem is always the aweme_id (aweme ids are all digits).
_ID_RE = None  # compiled lazily


def _split_numbered_stem(stem: str) -> tuple:
    """'12-7595175846302715163' -> ('12', '7595175846302715163');
    '7595175846302715163' (legacy) -> (None, same)."""
    if "-" in stem:
        head, tail = stem.split("-", 1)
        if head.isdigit() and tail.isdigit():
            return head, tail
    return None, stem


def _transcript_file(author_dir: Path, aweme_id: str,
                     number: Optional[str] = None) -> Path:
    """Resolve the transcript path for a video: numbered file first,
    legacy <aweme_id>.md as fallback (resume of pre-rename runs)."""
    if number is not None:
        numbered = author_dir / f"{number}-{aweme_id}.md"
        if numbered.exists():
            return numbered
    legacy = author_dir / f"{aweme_id}.md"
    if legacy.exists():
        return legacy
    return author_dir / f"{number}-{aweme_id}.md" if number is not None else legacy


def _next_serial(author_dir: Path, aweme_id: str,
                 taken=()) -> str:
    """Next free per-author serial number. An existing numbered file with
    the same video id keeps its number; otherwise max(existing, already
    assigned `taken` numbers this run) + 1 — `taken` prevents two pending
    videos from claiming the same serial before their files exist."""
    max_n = 0
    for p in author_dir.glob("*.md*"):
        head, tail = _split_numbered_stem(p.stem)
        if tail == aweme_id and head is not None:
            return head
        if head is not None:
            max_n = max(max_n, int(head))
    floor = max([max_n] + [int(v) for v in taken
                           if str(v).isdigit() and str(v) != "0"])
    return str(floor + 1)


def rename_transcripts_numbered(author_dir: Path) -> int:
    """One-time (idempotent) migration: rename '<aweme_id>.md' files to
    'N-<aweme_id>.md' in ascending video-id order, reusing existing numbers
    when possible. Returns the number of files renamed."""
    author_dir = Path(author_dir)
    used: dict = {}
    for p in author_dir.glob("*.md*"):
        if p.name == CATALOG_NAME or p.name.endswith(".part"):
            continue
        head, tail = _split_numbered_stem(p.stem)
        if head is not None:
            used[tail] = head
    legacy = sorted(
        (p for p in author_dir.glob("*.md")
         if p.stem.isdigit() and p.name != CATALOG_NAME),
        key=lambda p: p.stem,
    )
    n_renamed = 0
    for p in legacy:
        if p.stem in used:
            continue
        n = str(max((int(v) for v in used.values()), default=0) + 1)
        target = author_dir / f"{n}-{p.stem}.md"
        p.replace(target)
        used[p.stem] = n
        n_renamed += 1
    return n_renamed


def update_catalog(author_dir: Path, author: str = "") -> int:
    """(Re)generate the per-author catalog file 0-目录.txt.

    Transcript files are 'N-<aweme_id>.md' (legacy '<aweme_id>.md' is
    migrated first). One catalog entry per file:
        N-<aweme_id>.md
          标题: <title>
          简介: <desc line 2 (optional)>
    Returns the number of entries written.
    """
    author_dir = Path(author_dir)
    try:
        rename_transcripts_numbered(author_dir)
    except Exception:
        pass
    entries = []
    for md in author_dir.glob("*.md"):
        if md.name == CATALOG_NAME or md.name.endswith(".part"):
            continue
        head, tail = _split_numbered_stem(md.stem)
        sort_key = int(head) if head else 10 ** 12  # legacy files last
        entries.append((sort_key, md.name, tail, md))
    entries.sort(key=lambda e: (e[0], e[2]))

    out = [f"# {author} 文案目录" if author else "# 文案目录",
           f"共 {len(entries)} 个视频", ""]
    for sort_key, name, tail, md in entries:
        try:
            lines = md.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []
        title = (lines[0].lstrip("# ").strip() if lines else "") or tail
        desc2 = lines[1].strip() if len(lines) > 1 else ""
        # Skip metadata-table lines that sometimes follow the title
        if desc2.startswith("|"):
            desc2 = ""
        out.append(name)
        out.append(f"  标题: {title}")
        if desc2:
            out.append(f"  简介: {desc2}")
        out.append("")
    (author_dir / CATALOG_NAME).write_text("\n".join(out), encoding="utf-8")
    return len(entries)


def is_profile_input(text: str) -> bool:
    """True if the pasted text is an author profile link (vs a video link)."""
    return is_profile_url(text)


def fetch_author_videos(profile_link: str, max_videos: int = 0,
                        headless: bool = True, progress_cb: Optional[Callable[[int], None]] = None) -> dict:
    """Get the author's video list. Thin wrapper around profile_fetcher."""
    return fetch_profile_videos(
        profile_link, max_videos=max_videos, headless=headless,
        progress_cb=progress_cb,
    )


def batch_extract(
    profile_link: str,
    output_dir: str = "./output",
    api_key: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_base_url: Optional[str] = None,
    max_videos: int = 0,
    force: bool = False,
    headless: bool = True,
    save_video: bool = False,
    delay_seconds: float = 3.0,
    use_cache: bool = False,
    retry_failed: bool = True,
    workers: int = 3,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Extract transcripts for every (new) video of an author profile.

    on_progress receives dicts:
      {"stage": "list", "found": N}
      {"stage": "extract", "index": i, "total": N, "aweme_id":..., "title":...,
       "status": "ok"|"skip"|"fail", "error":..., "output":...}
      {"stage": "done", "ok": N, "skip": N, "fail": N}

    use_cache: True = reuse the cached author video list (no browser) to
      quickly retry previously failed videos. Fresh runs refresh the cache.
    retry_failed: previously failed videos are retried (history entries
      with status="fail" are NOT skipped).
    workers: number of videos processed concurrently (1 = serial). Each
      worker runs the full pipeline (parse -> download -> ffmpeg -> ASR)
      in its own thread; task STARTS are spaced by delay_seconds so the
      request rate towards douyin stays the same as in serial mode.

    Interrupted runs recover automatically: a video whose transcript md
      (or library record) survived is RESUMED (bookkeeping completed) instead
      of being re-downloaded; videos without a complete artifact are
      re-extracted. force only re-extracts videos already recorded as ok.

    Pause: module-level run_gate. When cleared, workers block BEFORE starting
      the next video; the video in flight always completes. Set it to
      resume (web /api/profile/batch/pause and /resume manage it).

    Returns summary dict.
    """
    # Import here to avoid circular import at module load
    from douyin_downloader import DouyinProcessor
    from asr_backends import resolve_backend

    # Each batch starts unpaused (a previous run may have left the gate cleared)
    run_gate.set()

    def _report(payload: dict):
        # Persist every progress/feedback event to the runtime operation log
        try:
            from operation_logger import log_operation
            log_operation("batch.progress", status=payload.get("stage", ""),
                          **{k: v for k, v in payload.items() if k != "stage"})
        except Exception:
            pass
        if on_progress:
            try:
                on_progress(payload)
            except Exception:
                pass

    from profile_fetcher import normalize_profile_url
    try:
        canonical = normalize_profile_url(profile_link)
    except Exception:
        canonical = profile_link

    cache = ProfileCache()
    profile = None
    if use_cache:
        cached = cache.get(canonical)
        if cached and cached.get("videos") and cached.get("logged_in", False):
            profile = cached
            _report({"stage": "list", "found": len(profile["videos"]),
                     "notice": f"使用缓存视频列表（{len(profile['videos'])} 个，{cached.get('cached_at', '')}）"})
        elif cached and cached.get("videos"):
            # The cached list came from an anonymous session and is likely
            # incomplete (douyin only serves the first page). Re-fetch instead
            # of silently reusing a partial list.
            _report({"stage": "list", "notice": "缓存列表来自未登录状态（可能只含第一页），改为重新抓取；如需抓全请先「登录抖音」"})
    if profile is None:
        _report({"stage": "list"})
        profile = fetch_profile_videos(
            profile_link, max_videos=max_videos, headless=headless,
            progress_cb=lambda n: _report({"stage": "list", "found": n}),
            on_notice=lambda msg: _report({"stage": "list", "notice": msg}),
        )
        if profile.get("videos"):
            try:
                cache.put(canonical, {
                    "sec_uid": profile.get("sec_uid", ""),
                    "nickname": profile.get("nickname", ""),
                    "videos": profile.get("videos", []),
                    "logged_in": bool(profile.get("logged_in")),
                })
            except Exception:
                pass
    videos = profile.get("videos", [])
    _report({"stage": "list", "found": len(videos)})

    history = TranscriptHistory()
    backend_cfg = resolve_backend(api_key, provider, model, api_base_url)

    out_base = Path(output_dir)
    out_base.mkdir(parents=True, exist_ok=True)
    author_dir_name = (profile.get("nickname") or profile.get("sec_uid") or "author")
    author_dir_name = "".join(c for c in author_dir_name if c not in r'\/:*?"<>|').strip() or "author"
    author_dir = out_base / author_dir_name
    author_dir.mkdir(parents=True, exist_ok=True)

    # Migrate legacy '<aweme_id>.md' files to 'N-<aweme_id>.md' before any
    # resume/serial logic runs, so serial assignment sees numbered names.
    try:
        rename_transcripts_numbered(author_dir)
    except Exception:
        pass

    total = len(videos)
    ok = skip = fail = 0

    # Shared mutable state guarded by one lock (counters, history file,
    # library file appends).
    state_lock = threading.Lock()
    counters = {"ok": 0, "fail": 0, "resumed": 0}

    # Library index (video_id -> record), loaded once, for resume lookups.
    from transcript_library import read_records as _lib_read_records
    try:
        lib_records = {str(r.get("video_id") or ""): r
                       for r in _lib_read_records() if r.get("video_id")}
    except Exception:
        lib_records = {}
    lib_ids = set(lib_records)

    # Per-author serial numbers, assigned in the main thread (single
    # assigner -> no races): existing file keeps its number, new videos
    # get max+1 in profile order. Used by resume writes and workers alike.
    serials: dict = {}

    def _serial_for(aweme_id: str) -> str:
        if aweme_id not in serials:
            serials[aweme_id] = _next_serial(author_dir, aweme_id,
                                             taken=serials.values())
        return serials[aweme_id]

    # Pre-filter in the main thread:
    # - history-ok videos are skipped (re-extracted under force)
    # - INTERRUPTED videos (no history entry) are RESUMED when a complete
    #   transcript survived on disk / in the library (crash between writing
    #   the md and recording history); otherwise they are re-extracted.
    pending = []
    for i, video in enumerate(videos):
        aweme_id = video["aweme_id"]
        title = video.get("desc") or f"douyin_{aweme_id}"
        hist_ok = history.has(aweme_id)
        if hist_ok and not force:
            skip += 1
            _report({"stage": "extract", "index": i + 1, "total": total,
                     "aweme_id": aweme_id, "title": title,
                     "status": "skip", "output": ""})
            continue
        if hist_ok:
            pending.append((i, aweme_id, title))
            _serial_for(aweme_id)  # reserve serial in profile order
            continue

        md_path = _transcript_file(author_dir, aweme_id,
                                   _serial_for(aweme_id))
        lib_rec = lib_records.get(aweme_id) or {}
        md_text = _read_complete_transcript(md_path)
        text = md_text or (lib_rec.get("text") or "").strip() or None
        if text is not None:
            # Resume: finish the bookkeeping an interrupted run missed.
            try:
                if md_text is None:
                    _write_transcript_md(
                        md_path,
                        (lib_rec.get("title") or title).strip() or title,
                        aweme_id, profile.get("nickname", ""), text)
                with state_lock:
                    if aweme_id not in lib_ids:
                        from transcript_library import append_record
                        append_record(
                            video_id=aweme_id, title=title, text=text,
                            author=profile.get("nickname", ""), source="batch",
                            output=str(md_path),
                            provider=backend_cfg['provider'],
                            model=backend_cfg['model'],
                        )
                    history.add(aweme_id, title, str(md_path))
                    counters["ok"] += 1
                    counters["resumed"] += 1
                _report({"stage": "extract", "index": i + 1, "total": total,
                         "aweme_id": aweme_id, "title": title,
                         "status": "ok", "resumed": True,
                         "output": str(md_path)})
                continue
            except Exception:
                pass  # resume failed -> fall through and re-extract
        pending.append((i, aweme_id, title))
        _serial_for(aweme_id)  # reserve serial in profile order

    # Polish config resolved once for the whole run
    from asr_backends import load_config_file
    polish_cfg = (load_config_file().get("polish") or {})

    # Start-interval pacing: keep at least delay_seconds between task
    # STARTS (parse/download hit douyin) regardless of worker count, so
    # concurrency does not raise the request rate towards douyin.
    start_lock = threading.Lock()
    last_start = [0.0]

    def _paced_start():
        # Block here while paused: the in-flight video finishes normally,
        # the next one does not start until the gate is set again.
        run_gate.wait()
        with start_lock:
            wait = delay_seconds - (time.time() - last_start[0])
            if wait > 0:
                time.sleep(wait)
            last_start[0] = time.time()

    def _process_one(index: int, aweme_id: str, title: str):
        """Full per-video pipeline; runs inside a worker thread."""
        # One processor per task: isolated temp dir, auto-cleaned on exit.
        processor = DouyinProcessor(
            backend_cfg['api_key'],
            provider=backend_cfg['provider'],
            model=backend_cfg['model'],
            api_base_url=backend_cfg['api_base_url'] or "",
        )
        _paced_start()
        try:
            share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}"
            video_info = processor.parse_share_url(share_url)
            video_path = processor.download_video(video_info, show_progress=False)
            audio_path = processor.extract_audio(video_path, show_progress=False)
            try:
                text = processor.extract_text_from_audio(audio_path, show_progress=False)
            finally:
                processor.cleanup_files(audio_path)

            # Optional LLM polish (fixes homophones/punctuation)
            if polish_cfg.get("enabled"):
                try:
                    from transcript_polish import polish_transcript
                    text = polish_transcript(text)
                except Exception:
                    pass

            transcript_path = _transcript_file(
                author_dir, aweme_id, _serial_for(aweme_id))
            _write_transcript_md(transcript_path, title, aweme_id,
                                 profile.get("nickname", ""), text)

            if save_video:
                saved = author_dir / f"{aweme_id}.mp4"
                shutil.copy2(video_path, saved)

            processor.cleanup_files(video_path)

            with state_lock:
                # Append to the global library file (all extractions in one place)
                from transcript_library import append_record
                append_record(
                    video_id=aweme_id,
                    title=title,
                    text=text,
                    author=profile.get("nickname", ""),
                    source="batch",
                    output=str(transcript_path),
                    provider=backend_cfg['provider'],
                    model=backend_cfg['model'],
                )
                history.add(aweme_id, title, str(transcript_path))
                counters["ok"] += 1

            _report({"stage": "extract", "index": index + 1, "total": total,
                     "aweme_id": aweme_id, "title": title,
                     "status": "ok", "output": str(transcript_path)})
        except Exception as e:
            with state_lock:
                counters["fail"] += 1
                try:
                    history.mark_fail(aweme_id, title, str(e))
                except Exception:
                    pass
            _report({"stage": "extract", "index": index + 1, "total": total,
                     "aweme_id": aweme_id, "title": title,
                     "status": "fail", "error": str(e)[:300]})

    workers = max(1, int(workers or 1))
    if workers == 1 or len(pending) <= 1:
        for index, aweme_id, title in pending:
            _process_one(index, aweme_id, title)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as pool:
            futures = [
                pool.submit(_process_one, index, aweme_id, title)
                for index, aweme_id, title in pending
            ]
            for f in futures:
                f.result()  # errors are reported inside _process_one

    ok += counters["ok"]
    fail += counters["fail"]
    # Refresh the per-author catalog (filename <-> title/desc index)
    try:
        update_catalog(author_dir, profile.get("nickname", ""))
    except Exception:
        pass
    summary = {
        "author": profile.get("nickname", ""),
        "sec_uid": profile.get("sec_uid", ""),
        "total": total, "ok": ok, "skip": skip, "fail": fail,
        "resumed": counters["resumed"],
        "output_dir": str(author_dir),
    }
    _report({"stage": "done", **summary})
    return summary
