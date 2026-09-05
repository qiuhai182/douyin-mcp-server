#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Douyin watermark-free video downloader and transcript extractor

Features:
1. Get watermark-free video download link from douyin share link
2. Download video and extract audio
3. Extract text from audio via speech-to-text API
4. Auto save transcript to file (one folder per video)

Environment variables:
- API_KEY: API key for transcript extraction (any service compatible
  with OpenAI /v1/audio/transcriptions endpoint)
- API_BASE_URL: (optional) custom transcription endpoint URL
- ASR_MODEL: (optional) custom model name

Usage:
  # Get download link (no API key required)
  python douyin_downloader.py --link "douyin share link" --action info

  # Download video
  python douyin_downloader.py --link "douyin share link" --action download --output ./videos

  # Extract transcript and save to file (requires API_KEY env var)
  python douyin_downloader.py --link "douyin share link" --action extract --output ./output
"""

import os
import re
import sys
import json
import argparse
import tempfile
import shutil
import urllib.parse
from pathlib import Path
from typing import Optional
from datetime import datetime


def check_dependencies():
    """Check required dependencies are installed"""
    missing = []
    try:
        import requests
    except ImportError:
        missing.append("requests")
    try:
        import ffmpeg
    except ImportError:
        missing.append("ffmpeg-python")

    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print(f"Run: pip install {' '.join(missing)}")
        sys.exit(1)


check_dependencies()

import requests
import ffmpeg

from asr_backends import transcribe_audio_file, resolve_backend

# Ensure bundled ffmpeg is visible even if PATH was not refreshed
# (the web server process inherits PATH from its parent shell).
_BUNDLED_FFMPEG_BIN = Path(__file__).resolve().parent.parent.parent / "ffmpeg" / "ffmpeg-9.0.1-full_build" / "bin"
if _BUNDLED_FFMPEG_BIN.exists():
    os.environ["PATH"] = f"{_BUNDLED_FFMPEG_BIN}{os.pathsep}{os.environ.get('PATH', '')}"

# Request headers, simulating mobile access
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 Version/17.0 Mobile/15E148 Safari/604.1'
}


class DouyinProcessor:
    """Douyin video processor"""

    def __init__(self, api_key: str = "", provider: str = "", model: str = "", api_base_url: str = ""):
        self.api_key = api_key
        self.provider = provider
        self.model = model
        self.api_base_url = api_base_url
        self.temp_dir = Path(tempfile.mkdtemp())

    def __del__(self):
        """Clean up temp directory"""
        if hasattr(self, 'temp_dir') and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def parse_share_url(self, share_text: str) -> dict:
        """Extract watermark-free video link from share text"""
        # Extract share link
        urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', share_text)
        if not urls:
            raise ValueError("No valid share link found")

        share_url = urls[0]

        # Smart: user page links that carry a video in modal_id
        # e.g. https://www.douyin.com/user/self?modal_id=7656204910022708521&...
        parsed = urllib.parse.urlparse(share_url)
        query = urllib.parse.parse_qs(parsed.query)
        if '/user/' in parsed.path and 'modal_id' in query:
            video_id = query['modal_id'][0]
            share_url = f'https://www.iesdouyin.com/share/video/{video_id}'
        else:
            share_response = requests.get(share_url, headers=HEADERS, timeout=30)
            video_id = share_response.url.split("?")[0].strip("/").split("/")[-1]
            share_url = f'https://www.iesdouyin.com/share/video/{video_id}'

        # Get video page content
        response = requests.get(share_url, headers=HEADERS, timeout=30)
        response.raise_for_status()

        pattern = re.compile(
            pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            flags=re.DOTALL,
        )
        find_res = pattern.search(response.text)

        def _parse_router_data(html_text: str) -> Optional[dict]:
            """Parse share-page HTML into video info; None if not usable."""
            m = pattern.search(html_text)
            if not m or not m.group(1):
                return None
            try:
                json_data = json.loads(m.group(1).strip())
            except Exception:
                return None
            VIDEO_ID_PAGE_KEY = "video_(id)/page"
            NOTE_ID_PAGE_KEY = "note_(id)/page"
            loader = json_data.get("loaderData", {})
            page_key = None
            if VIDEO_ID_PAGE_KEY in loader:
                page_key = VIDEO_ID_PAGE_KEY
            elif NOTE_ID_PAGE_KEY in loader:
                page_key = NOTE_ID_PAGE_KEY
            if page_key is None:
                return None
            video_info_res = loader[page_key].get("videoInfoRes")
            if not video_info_res or not video_info_res.get("item_list"):
                # Risk-control shell page: videoInfoRes missing/empty
                return None
            data = video_info_res["item_list"][0]
            play = data.get("video", {}).get("play_addr", {})
            url_list = play.get("url_list") or []
            if not url_list:
                return None
            vid = data.get("aweme_id") or video_id
            desc = data.get("desc", "").strip() or f"douyin_{vid}"
            desc = re.sub(r'[\\/:*?"<>|]', '_', desc)
            return {
                "url": url_list[0].replace("playwm", "play"),
                "title": desc,
                "video_id": str(vid)
            }

        video_info = _parse_router_data(response.text)

        if video_info is None:
            # Share page is risk-controlled -> browser fallback via
            # the desktop video detail page (also handles private-ish videos)
            from video_detail_browser import fetch_video_info_via_browser
            video_info = fetch_video_info_via_browser(video_id)

        # Replace illegal characters in filename
        desc = re.sub(r'[\\/:*?"<>|]', '_', video_info["title"])
        video_info["title"] = desc
        return video_info

    def download_video(self, video_info: dict, output_dir: Optional[Path] = None, show_progress: bool = True) -> Path:
        """Download video (with retry + longer cooldown for network errors)."""
        if output_dir is None:
            output_dir = self.temp_dir
        else:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{video_info['video_id']}.mp4"
        filepath = output_dir / filename

        if show_progress:
            print(f"Downloading video: {video_info['title']}")

        # web CDN URLs (douyinvod.com) require a desktop browser UA + Referer;
        # mobile CDN URLs work with the mobile UA. Try browser headers first.
        is_web_cdn = 'douyinvod.com' in video_info['url'] or '-web' in video_info['url'][:60]
        dl_headers = {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/120.0.0.0 Safari/537.36'),
            'Referer': 'https://www.douyin.com/',
        } if is_web_cdn else HEADERS

        # Retry up to 3 times: each attempt uses a slightly different
        # user-agent to dodge simple CDN blocks, and waits longer on
        # network errors (5xx, connection reset, timeout) - these are
        # usually "you're rate-limited, slow down" responses.
        import time
        attempts = [
            dl_headers,
            HEADERS if is_web_cdn else dl_headers,                       # UA swap
            {**dl_headers, 'User-Agent': HEADERS['User-Agent']},         # alternate UA
        ]
        last_err = None
        for attempt_idx, headers in enumerate(attempts):
            try:
                response = requests.get(
                    video_info['url'], headers=headers, stream=True, timeout=60
                )
                if response.status_code == 403 and attempt_idx < len(attempts) - 1:
                    continue
                response.raise_for_status()

                # Get file size
                total_size = int(response.headers.get('content-length', 0))

                # Download file
                downloaded = 0
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if show_progress and total_size > 0:
                                pass  # quiet in batch
                # quick sanity: file should be > 100KB for a real video
                if filepath.exists() and filepath.stat().st_size > 100_000:
                    return filepath
                last_err = RuntimeError(f"downloaded file too small ({filepath.stat().st_size} bytes)")
            except (requests.exceptions.RequestException, RuntimeError) as e:
                last_err = e
                if show_progress:
                    print(f"  download attempt {attempt_idx+1} failed: {e}")
            # Cooldown between attempts: longer for the last retry so we
            # don't hammer the CDN if it's rate-limiting us.
            time.sleep(2.0 + attempt_idx * 3.0)
        raise RuntimeError(f"Error downloading video after {len(attempts)} attempts: {last_err}")

    def extract_audio(self, video_path: Path, show_progress: bool = True) -> Path:
        """Extract audio from video file.

        Applies a voice-optimization filter chain to improve ASR accuracy:
        - highpass 200Hz: cut rumble/BGM bass
        - lowpass 3400Hz: keep speech band (phone-quality focus)
        - afftdn: FFT denoiser for background noise/music
        - loudnorm: normalize volume so quiet speech is audible
        """
        audio_path = video_path.with_suffix('.mp3')

        if show_progress:
            print("Extracting audio (voice optimized)...")
        try:
            voice_filter = (
                "highpass=f=200,"
                "lowpass=f=3400,"
                "afftdn=nf=-25,"
                "loudnorm=I=-16:TP=-1.5:LRA=11"
            )
            (
                ffmpeg
                .input(str(video_path))
                .output(str(audio_path), acodec='libmp3lame', q=0, af=voice_filter, ar=16000, ac=1)
                .run(capture_stdout=True, capture_stderr=True, overwrite_output=True)
            )
            if show_progress:
                print(f"Audio extracted: {audio_path}")
            return audio_path
        except Exception as e:
            # Fallback to plain extraction if the filter chain fails
            try:
                (
                    ffmpeg
                    .input(str(video_path))
                    .output(str(audio_path), acodec='libmp3lame', q=0)
                    .run(capture_stdout=True, capture_stderr=True, overwrite_output=True)
                )
                if show_progress:
                    print(f"Audio extracted (plain): {audio_path}")
                return audio_path
            except Exception as e2:
                raise Exception(f"Error extracting audio: {str(e2)}")

    def get_audio_info(self, audio_path: Path) -> dict:
        """Get audio file info (duration and size)"""
        try:
            probe = ffmpeg.probe(str(audio_path))
            duration = float(probe['format'].get('duration', 0))
            size = audio_path.stat().st_size
            return {'duration': duration, 'size': size}
        except Exception:
            return {'duration': 0, 'size': audio_path.stat().st_size}

    def split_audio(self, audio_path: Path, segment_duration: int = 600, show_progress: bool = True) -> list:
        """
        Split audio into segments

        Args:
            audio_path: audio file path
            segment_duration: segment duration in seconds, default 10 minutes
            show_progress: whether to show progress

        Returns:
            list of split audio file paths
        """
        audio_info = self.get_audio_info(audio_path)
        duration = audio_info['duration']

        if duration <= segment_duration:
            return [audio_path]

        segments = []
        segment_index = 0
        current_time = 0

        if show_progress:
            total_segments = int(duration / segment_duration) + 1
            print(f"Audio duration {duration:.0f}s, will split into {total_segments} segments...")

        while current_time < duration:
            segment_path = self.temp_dir / f"segment_{segment_index}.mp3"

            try:
                (
                    ffmpeg
                    .input(str(audio_path), ss=current_time, t=segment_duration)
                    .output(str(segment_path), acodec='libmp3lame', q=0)
                    .run(capture_stdout=True, capture_stderr=True, overwrite_output=True)
                )
                segments.append(segment_path)

                if show_progress:
                    print(f"  Segment {segment_index + 1}: {current_time:.0f}s - {min(current_time + segment_duration, duration):.0f}s")

            except Exception as e:
                raise Exception(f"Error splitting audio segment {segment_index}: {str(e)}")

            current_time += segment_duration
            segment_index += 1

        return segments

    def transcribe_single_audio(self, audio_path: Path) -> str:
        """Transcribe a single audio file via the configured backend"""
        return transcribe_audio_file(
            audio_path,
            api_key=self.api_key,
            provider=self.provider or None,
            model=self.model or None,
            api_base_url=self.api_base_url or None,
        )

    def extract_text_from_audio(self, audio_path: Path, show_progress: bool = True) -> str:
        """Extract text from audio file (auto-split large files)"""
        if not self.api_key:
            raise ValueError("API key not set, please set API_KEY environment variable")

        # Check file size and duration
        audio_info = self.get_audio_info(audio_path)
        max_duration = 3600  # 1 hour
        max_size = 50 * 1024 * 1024  # 50MB

        # Determine if splitting is needed
        need_split = audio_info['duration'] > max_duration or audio_info['size'] > max_size

        if not need_split:
            # File within limits, process directly
            if show_progress:
                print("Recognizing speech...")
            return self.transcribe_single_audio(audio_path)

        # Need to split
        if show_progress:
            print(f"Audio file is large (duration: {audio_info['duration']:.0f}s, size: {audio_info['size'] / 1024 / 1024:.1f}MB)")
            print("Will auto-split...")

        # Split audio
        segments = self.split_audio(audio_path, segment_duration=540, show_progress=show_progress)  # 9 min per segment

        # Transcribe each segment
        all_texts = []
        for i, segment_path in enumerate(segments):
            if show_progress:
                print(f"Recognizing segment {i + 1}/{len(segments)}...")

            text = self.transcribe_single_audio(segment_path)
            all_texts.append(text)

            # Clean up segment file
            if segment_path != audio_path:
                self.cleanup_files(segment_path)

        # Merge texts
        merged_text = ''.join(all_texts)

        if show_progress:
            print(f"Speech recognition done, processed {len(segments)} segments")

        return merged_text

    def cleanup_files(self, *file_paths: Path):
        """Clean up given files"""
        for file_path in file_paths:
            if file_path.exists():
                file_path.unlink()


def get_video_info(share_link: str) -> dict:
    """Get video info and download link"""
    processor = DouyinProcessor()
    return processor.parse_share_url(share_link)


def download_video(share_link: str, output_dir: str = ".") -> Path:
    """Download video to directory"""
    processor = DouyinProcessor()
    video_info = processor.parse_share_url(share_link)
    return processor.download_video(video_info, Path(output_dir))


def extract_text(share_link: str, api_key: Optional[str] = None, output_dir: Optional[str] = None,
                 save_video: bool = False, show_progress: bool = True) -> dict:
    """
    Extract transcript from video and save to file

    Returns:
        dict: containing video_info, text, output_path
    """
    api_key = api_key or os.getenv('API_KEY') or os.getenv('DASHSCOPE_API_KEY') or os.getenv('ARK_API_KEY')
    if not api_key:
        raise ValueError("API key not set. Set API_KEY (works for all providers) "
                         "or DASHSCOPE_API_KEY / ARK_API_KEY for those providers.")

    # Backend resolved from env: ASR_PROVIDER (siliconflow|dashscope|ark)
    # plus ASR_MODEL / API_BASE_URL overrides
    backend = resolve_backend()
    processor = DouyinProcessor(
        backend['api_key'],
        provider=backend['provider'],
        model=backend['model'],
        api_base_url=backend['api_base_url'] or "",
    )

    if show_progress:
        print("Parsing douyin share link...")
    video_info = processor.parse_share_url(share_link)

    if show_progress:
        print("Downloading video...")
    video_path = processor.download_video(video_info, show_progress=show_progress)

    if show_progress:
        print("Extracting audio...")
    audio_path = processor.extract_audio(video_path, show_progress=show_progress)

    if show_progress:
        print("Extracting text from audio...")
    text_content = processor.extract_text_from_audio(audio_path, show_progress=show_progress)

    # Record to the global library file (all extractions in one place)
    from transcript_library import append_record
    append_record(
        video_id=video_info["video_id"],
        title=video_info["title"],
        text=text_content,
        source="single",
        provider=backend['provider'],
        model=backend['model'],
    )

    result = {
        "video_info": video_info,
        "text": text_content,
        "output_path": None
    }

    # Save to file
    if output_dir:
        output_base = Path(output_dir)
        video_folder = output_base / video_info['video_id']
        video_folder.mkdir(parents=True, exist_ok=True)

        # Save transcript as Markdown
        transcript_path = video_folder / "transcript.md"
        with open(transcript_path, 'w', encoding='utf-8') as f:
            f.write(f"# {video_info['title']}\n\n")
            f.write(f"| Attribute | Value |\n")
            f.write(f"|------|----|\n")
            f.write(f"| Video ID | `{video_info['video_id']}` |\n")
            f.write(f"| Extracted at | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |\n")
            f.write(f"| Download link | [Download]({video_info['url']}) |\n\n")
            f.write(f"---\n\n")
            f.write(f"## Transcript\n\n")
            f.write(text_content)

        result["output_path"] = str(video_folder)

        if show_progress:
            print(f"Transcript saved to: {transcript_path}")

        # Save video (optional)
        if save_video:
            saved_video_path = video_folder / f"{video_info['video_id']}.mp4"
            shutil.copy2(video_path, saved_video_path)
            if show_progress:
                print(f"Video saved to: {saved_video_path}")

    # Clean up temp files
    if show_progress:
        print("Cleaning temp files...")
    processor.cleanup_files(video_path, audio_path)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Douyin watermark-free video downloader and transcript extractor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Get video info and download link
  python douyin_downloader.py --link "douyin share link" --action info

  # Download video
  python douyin_downloader.py --link "douyin share link" --action download --output ./videos

  # Extract transcript and save to file (requires API_KEY env var)
  python douyin_downloader.py --link "douyin share link" --action extract --output ./output

  # Extract transcript and save video too
  python douyin_downloader.py --link "douyin share link" --action extract --output ./output --save-video
        """
    )

    parser.add_argument("--link", "-l", required=True, help="Douyin share link (video or author profile)")
    parser.add_argument("--action", "-a", choices=["info", "download", "extract", "batch"],
                        default="info", help="Action: info(get info), download(download video), extract(extract transcript), batch(extract all author videos)")
    parser.add_argument("--output", "-o", default="./output", help="Output directory (default ./output)")
    parser.add_argument("--api-key", "-k", help="API key (can also be set via API_KEY env var)")
    parser.add_argument("--save-video", "-v", action="store_true", help="Also save video when extracting transcript")
    parser.add_argument("--quiet", "-q", action="store_true", help="Quiet mode, less output")
    parser.add_argument("--max-videos", "-m", type=int, default=0, help="batch: max videos to process (0 = all)")
    parser.add_argument("--workers", "-w", type=int, default=3, help="batch: concurrent workers (1 = serial)")
    parser.add_argument("--force", action="store_true", help="batch: re-extract videos already in history.json")
    parser.add_argument("--no-headless", action="store_true", help="batch: show browser window (useful for login)")
    parser.add_argument("--provider", help="ASR provider: siliconflow | dashscope | ark (default from config/env)")
    parser.add_argument("--asr-model", help="ASR model name override")

    args = parser.parse_args()

    try:
        if args.action == "info":
            info = get_video_info(args.link)
            print("\n" + "=" * 50)
            print("Video info:")
            print("=" * 50)
            print(f"Video ID: {info['video_id']}")
            print(f"Title: {info['title']}")
            print(f"Download link: {info['url']}")
            print("=" * 50)

        elif args.action == "download":
            video_path = download_video(args.link, args.output)
            print(f"\nVideo saved to: {video_path}")

        elif args.action == "batch":
            from batch_extractor import batch_extract

            def _on_progress(info):
                if args.quiet:
                    return
                stage = info.get("stage")
                if stage == "list":
                    print(f"[list] found {info.get('found', 0)} videos")
                elif stage == "extract":
                    status = info.get("status")
                    if info.get("resumed"):
                        mark = "R"  # resumed from an interrupted run
                    else:
                        mark = {"ok": "+", "skip": "=", "fail": "x"}.get(status, "?")
                    line = f"[{info.get('index')}/{info.get('total')}] {mark} {info.get('aweme_id')} {info.get('title', '')[:40]}"
                    if status == "fail":
                        line += f" | error: {info.get('error', '')[:120]}"
                    elif status == "ok":
                        line += f" | {info.get('output', '')}"
                    print(line)
                elif stage == "done":
                    print("=" * 50)
                    done_line = (f"Batch done. ok={info.get('ok')} skip={info.get('skip')} "
                                 f"fail={info.get('fail')}")
                    if info.get("resumed"):
                        done_line += f" resumed={info.get('resumed')}"
                    print(done_line)
                    print(f"Output: {info.get('output_dir')}")

            summary = batch_extract(
                args.link,
                output_dir=args.output,
                api_key=args.api_key,
                provider=args.provider,
                model=args.asr_model,
                max_videos=args.max_videos,
                force=args.force,
                headless=not args.no_headless,
                save_video=args.save_video,
                on_progress=_on_progress,
            )

            if args.quiet:
                print(json.dumps(summary, ensure_ascii=False, indent=2))

        elif args.action == "extract":
            result = extract_text(
                args.link,
                args.api_key,
                output_dir=args.output,
                save_video=args.save_video,
                show_progress=not args.quiet
            )

            if not args.quiet:
                print("\n" + "=" * 50)
                print("Extraction complete!")
                print("=" * 50)
                print(f"Video ID: {result['video_info']['video_id']}")
                print(f"Title: {result['video_info']['title']}")
                if result['output_path']:
                    print(f"Saved to: {result['output_path']}")
                print("=" * 50)
                print("\nTranscript:\n")
                print(result['text'][:500] + "..." if len(result['text']) > 500 else result['text'])
                print("\n" + "=" * 50)

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
