#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch douyin author profile video list via browser automation.

Strategy:
- All automation uses a persistent browser profile + a cookies backup file
  (.douyin_cookies.json). After a login, cookies are exported to the backup;
  every later run restores them into the context first, so the session
  survives even if the profile dir gets corrupted or a different browser
  channel is used.
- The browser channel follows the SYSTEM DEFAULT browser order (read from
  the Windows registry) with Edge/Chrome as fallback.
- fetch_profile_videos() is always silent (headless): it never pops a
  window. If douyin limits anonymous visitors, the result contains fewer
  videos.
- interactive_login() opens a visible window ONCE so the user can log in
  with any method they prefer. The session is kept permanently afterwards.
"""

import json
import os
import re
import subprocess
import threading
import time
from typing import Optional
from pathlib import Path

import requests


HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) '
                  'AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 '
                  'Version/17.0 Mobile/15E148 Safari/604.1'
}

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")

# Dedicated persistent browser profile = "browser cache" for this tool
PROFILE_DIR = Path(__file__).resolve().parent.parent.parent / ".douyin_profile"

# Cookie backup: survives profile-dir corruption / browser-channel switches
COOKIES_FILE = Path(__file__).resolve().parent.parent.parent / ".douyin_cookies.json"


def _default_browser_exe() -> Optional[str]:
    r"""Resolve the SYSTEM DEFAULT browser's executable path from the
    Windows registry (no hardcoded browser names).

    Reads HKCU\...\https\UserChoice -> ProgId, then looks up
    HKCR\<ProgId>\shell\open\command to get the real exe path.

    Uses the winreg API instead of `reg query` so the (Default) value is
    read by name regardless of the OS display language (on zh-CN systems
    `reg query` prints "(默认)" instead of "(Default)", which broke the
    old text-parsing approach and silently fell back to Edge).
    Returns None when it can't be resolved.
    """
    try:
        import winreg
        key_path = (r'Software\Microsoft\Windows\Shell\Associations'
                    r'\UrlAssociations\https\UserChoice')
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            progid = winreg.QueryValueEx(k, "ProgId")[0]
        if not progid:
            return None

        # Resolve ProgId -> shell open command -> exe path.
        # HKCU classes take priority, then HKCR (machine-wide).
        roots = [
            (winreg.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + progid),
            (winreg.HKEY_CLASSES_ROOT, progid),
        ]
        for hive, subkey in roots:
            command = None
            try:
                with winreg.OpenKey(hive, subkey + r"\shell\open\command") as k:
                    # Read the (Default) value; fall back to a named
                    # "command" value used by some registrations.
                    try:
                        command = winreg.QueryValueEx(k, "")[0]
                    except OSError:
                        command = winreg.QueryValueEx(k, "command")[0]
            except OSError:
                continue
            if not command:
                continue
            # Strip trailing args like "--single-argument %1"
            exe_m = re.match(r'"([^"]+)"', command) or re.match(r'(\S+\.exe)', command, re.I)
            if exe_m:
                exe = exe_m.group(1)
                if Path(exe).exists():
                    return exe
        return None
    except Exception:
        return None


def _is_chromium_exe(exe_path: str) -> bool:
    """Heuristic: is this executable a Chromium-based browser we can
    drive with Playwright? (chrome/msedge/brave/opera/vivaldi...)"""
    name = Path(exe_path).name.lower()
    return any(b in name for b in (
        'chrome', 'msedge', 'edge', 'brave', 'opera', 'vivaldi', 'chromium',
    ))


def _browser_candidates() -> list:
    """Launch options to try, in order.

    1. System default browser's real executable (if Chromium-based)
    2. Fallbacks by Playwright channel (Edge, Chrome)
    Each item is a kwargs dict for launch_persistent_context.
    """
    candidates = []
    exe = _default_browser_exe()
    if exe and _is_chromium_exe(exe):
        candidates.append({"executable_path": exe})
    for ch in ("msedge", "chrome"):
        candidates.append({"channel": ch})
    return candidates


def normalize_profile_url(share_text: str) -> str:
    """Resolve any douyin profile share text/short link to a canonical
    https://www.douyin.com/user/<sec_uid> URL."""
    urls = re.findall(
        r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+',
        share_text or ''
    )
    if not urls:
        raise ValueError("No URL found in input text")
    url = urls[0]

    # Already a canonical profile URL
    m = re.search(r'douyin\.com/user/([A-Za-z0-9_-]+)', url)
    if m:
        return f"https://www.douyin.com/user/{m.group(1)}"

    # Short links / share links -> follow redirects
    resp = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    final = resp.url
    m = re.search(r'(?:douyin\.com|iesdouyin\.com)/(?:user|share/user)/([A-Za-z0-9_-]+)', final)
    if m:
        uid = m.group(1)
        # numeric uid pages on iesdouyin need the sec_uid; try query param first
        if uid.isdigit():
            m2 = re.search(r'sec_uid=([A-Za-z0-9_-]+)', final) or re.search(r'sec_uid=([A-Za-z0-9_-]+)', resp.text)
            if m2:
                return f"https://www.douyin.com/user/{m2.group(1)}"
        return f"https://www.douyin.com/user/{uid}"

    raise ValueError(f"URL is not a douyin profile link: {final}")


def is_profile_url(share_text: str) -> bool:
    """Heuristic: does this share text point to a user profile (not a video)?"""
    if not share_text:
        return False
    if re.search(r'douyin\.com/user/', share_text):
        return True
    if re.search(r'iesdouyin\.com/share/user/', share_text):
        return True
    # v.douyin.com short link - resolve and check
    m = re.search(r'https?://v\.douyin\.com/[A-Za-z0-9]+', share_text)
    if m:
        try:
            resp = requests.get(m.group(0), headers=HEADERS, timeout=15, allow_redirects=True)
            return bool(re.search(r'/user/', resp.url))
        except Exception:
            return False
    return False


# Only ONE Chromium may use the shared persistent profile at a time: two
# instances on the same user-data-dir lock each other's cookie DB, which
# freezes the page's renderer (page.evaluate / mouse.wheel then hang forever
# while XHR responses are still captured - the classic "已发现 N 个视频" freeze).
# All callers live in this process, so a thread lock is enough.
_PROFILE_LOCK = threading.Lock()

# A batch must never wait forever for the profile: if the lock is not released
# within this window, something leaked it and we abort with a clear message.
_PROFILE_LOCK_TIMEOUT = 180


def _release_profile_lock():
    """Release the shared-profile lock, ignoring an unbalanced release."""
    try:
        _PROFILE_LOCK.release()
    except RuntimeError:
        pass


def _kill_profile_browsers(timeout: int = 20) -> int:
    r"""Force-kill browser processes still holding our persistent profile.

    Only processes whose command line references the profile directory are
    touched, so the user's own Chrome/Edge windows are never affected.
    """
    marker = PROFILE_DIR.name  # ".douyin_profile"
    script = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='chrome.exe' or Name='msedge.exe'\" | "
        f"Where-Object {{ $_.CommandLine -and $_.CommandLine -like '*{marker}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        pass
    return 0


def _open_persistent(p, headless: bool):
    """Open a persistent browser context.

    Tries the system default browser's real executable first (resolved
    from the registry - no hardcoded names), then falls back to Edge/Chrome
    channels. After opening, cookies from the backup file are restored
    (if any), making the login state durable across runs.

    Holds _PROFILE_LOCK until the context is torn down: callers must release
    it via _release_profile_lock() right after _kill_profile_browsers().
    """
    if not _PROFILE_LOCK.acquire(timeout=_PROFILE_LOCK_TIMEOUT):
        raise RuntimeError(
            "上一个批次仍占用抖音浏览器配置，"
            f"已等待 {_PROFILE_LOCK_TIMEOUT}s 仍未释放；本次抓取已中止（避免卡死）"
        )
    # A browser left behind by a killed/crashed run still holds the profile
    # dir; the new instance's page would then freeze (scroll/XHR hang forever).
    # Clear any such leftover before launching.
    _kill_profile_browsers()
    last_err = None
    try:
        for extra in _browser_candidates():
            try:
                context = p.chromium.launch_persistent_context(
                    str(PROFILE_DIR),
                    headless=headless,
                    user_agent=DESKTOP_UA,
                    viewport={"width": 1380, "height": 900},
                    locale="zh-CN",
                    args=["--disable-blink-features=AutomationControlled"],
                    **extra,
                )
                _restore_cookies(context)
                return context
            except Exception as e:
                last_err = e
        raise RuntimeError(
            "No Chromium-based browser available for automation "
            "(system default browser is not Chromium-based or not found). "
            f"Last error: {last_err}"
        )
    except Exception:
        _release_profile_lock()
        raise


def _save_cookies(context):
    """Export douyin cookies to the backup file (called after login)."""
    try:
        cookies = [c for c in context.cookies() if 'douyin' in (c.get('domain') or '')]
        if any(c.get('name') == 'sessionid' for c in cookies):
            COOKIES_FILE.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        pass


def _restore_cookies(context):
    """Restore douyin cookies from the backup file into this context."""
    try:
        if not COOKIES_FILE.exists():
            return
        cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        if cookies:
            context.add_cookies(cookies)
    except Exception:
        pass


def _has_saved_session() -> bool:
    """True if the cookies backup file holds a sessionid (no browser needed)."""
    try:
        if not COOKIES_FILE.exists():
            return False
        cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        return any(c.get("name") == "sessionid" for c in cookies)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Read the login state DIRECTLY from installed browsers' cookie databases
# (Chrome/Edge/Brave...). This reads the user's REAL browser profile, so a
# douyin login done in everyday browsing is detected without any window.
# ---------------------------------------------------------------------------

def _browser_cookie_db_paths() -> list:
    """Candidate (browser_name, Cookies db path) pairs for all installed
    Chromium-based browsers, WITHOUT hardcoding which one must exist."""
    home = Path.home()
    bases = [
        home / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
        home / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data",
        home / "AppData" / "Local" / "Chromium" / "User Data",
        home / "AppData" / "Local" / "BraveSoftware" / "Brave-Browser" / "User Data",
        home / "AppData" / "Local" / "Vivaldi" / "User Data",
        home / "AppData" / "Roaming" / "Opera Software" / "Opera Stable",
    ]
    out = []
    for base in bases:
        if not base.exists():
            continue
        # All profiles: Default, Profile 1, Profile 2, ...
        for profile in base.iterdir():
            if not profile.is_dir():
                continue
            db = profile / "Network" / "Cookies"
            if not db.exists():
                db = profile / "Cookies"
            if db.exists():
                out.append((base.name, profile.name, db))
    return out


def _decrypt_chromium_cookie(blob: bytes, key: bytes) -> bytes:
    """Decrypt one cookie value (AES-GCM v10/v20 format or plain DPAPI)."""
    import win32crypt
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:3] in (b'v10', b'v20'):
        nonce, ct = blob[3:15], blob[15:]
        return AESGCM(key).decrypt(nonce, ct, None)
    return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]


def _local_state_key(user_data_dir: Path) -> Optional[bytes]:
    """AES key for cookie decryption, from the browser's Local State file."""
    import base64
    import win32crypt
    lf = user_data_dir / "Local State"
    try:
        encrypted_key = json.loads(lf.read_text(encoding="utf-8"))[
            "os_crypt"]["encrypted_key"]
        key = base64.b64decode(encrypted_key)[5:]  # strip DPAPI prefix
        return win32crypt.CryptUnprotectData(key, None, None, None, 0)[1]
    except Exception:
        return None


def _firefox_cookie_db_paths() -> list:
    """Firefox profiles' cookies.sqlite (values stored in PLAINTEXT)."""
    base = Path.home() / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
    out = []
    if base.exists():
        for profile in base.iterdir():
            db = profile / "cookies.sqlite"
            if db.exists():
                out.append(("Firefox", profile.name, db))
    return out


def _read_sessionid_from_browsers() -> Optional[dict]:
    """Scan installed browsers' cookie DBs for a douyin sessionid.

    Returns a Playwright-ready cookie dict, or None. Works entirely offline:
    the DB is copied to temp (dodge the file lock) and decrypted locally.
    """
    import shutil
    import tempfile
    try:
        import win32crypt  # noqa: F401
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except Exception:
        return None

    for browser, profile, db in _browser_cookie_db_paths():
        tmp = None
        try:
            # User Data root: db = <User Data>/<profile>/Network/Cookies
            # (new layout) or <User Data>/<profile>/Cookies (old layout)
            profile_dir = db.parent.parent if db.parent.name.lower() == 'network' \
                else db.parent
            key = _local_state_key(profile_dir.parent)
            if not key:
                continue
            # Copy to temp: the live DB is locked by the running browser.
            tmp = Path(tempfile.mkdtemp(prefix="dyck_")) / "Cookies"
            src = open(db, 'rb')
            try:
                data = src.read()
            finally:
                src.close()
            tmp.write_bytes(data)
            import sqlite3
            conn = sqlite3.connect(str(tmp))
            rows = conn.execute(
                "SELECT name, encrypted_value, host_key, path, is_secure, "
                "is_httponly, expires_utc FROM cookies "
                "WHERE host_key LIKE '%douyin%'").fetchall()
            conn.close()
            for name, enc, host, path, secure, httponly, exp in rows:
                if name != "sessionid":
                    continue
                try:
                    value = _decrypt_chromium_cookie(enc, key)
                except Exception:
                    continue
                if not value:
                    continue
                return {
                    "name": "sessionid",
                    "value": value.decode("utf-8", "replace"),
                    "domain": host or ".douyin.com",
                    "path": path or "/",
                    "secure": bool(secure),
                    "httpOnly": bool(httponly),
                }
        except Exception:
            continue
        finally:
            if tmp:
                try:
                    shutil.rmtree(tmp.parent, ignore_errors=True)
                except Exception:
                    pass

    # Firefox: cookies.sqlite stores values in PLAINTEXT (no decryption)
    for browser, profile, db in _firefox_cookie_db_paths():
        tmp = None
        try:
            tmp = Path(tempfile.mkdtemp(prefix="dyck_")) / "cookies.sqlite"
            src = open(db, 'rb')
            try:
                data = src.read()
            finally:
                src.close()
            tmp.write_bytes(data)
            import sqlite3
            conn = sqlite3.connect(str(tmp))
            rows = conn.execute(
                "SELECT name, value, host, path, isSecure, isHttpOnly, expiry "
                "FROM moz_cookies WHERE host LIKE '%douyin%'").fetchall()
            conn.close()
            for name, value, host, path, secure, httponly, exp in rows:
                if name != "sessionid" or not value:
                    continue
                return {
                    "name": "sessionid",
                    "value": value,
                    "domain": host or ".douyin.com",
                    "path": path or "/",
                    "secure": bool(secure),
                    "httpOnly": bool(httponly),
                }
        except Exception:
            continue
        finally:
            if tmp:
                try:
                    shutil.rmtree(tmp.parent, ignore_errors=True)
                except Exception:
                    pass
    return None


def _import_browser_session_to_backup() -> bool:
    """Import a douyin sessionid found in installed browsers into our
    cookies backup (merging with existing backup cookies)."""
    found = _read_sessionid_from_browsers()
    if not found:
        return False
    try:
        existing = []
        if COOKIES_FILE.exists():
            try:
                existing = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        existing = [c for c in existing if c.get("name") != "sessionid"]
        existing.append(found)
        COOKIES_FILE.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


# Port used to attach to an ALREADY RUNNING browser via CDP
CDP_PORT = 9222


def _attach_running_browser(p):
    """Attach to an already-running Chromium browser via CDP (new tab mode).

    Returns (context, close_fn) or (None, None) when no debuggable browser
    is running. Never launches a new window.
    """
    import urllib.request
    try:
        urllib.request.urlopen(
            f'http://127.0.0.1:{CDP_PORT}/json/version', timeout=1)
    except Exception:
        return None, None
    try:
        browser = p.chromium.connect_over_cdp(
            f'http://127.0.0.1:{CDP_PORT}', timeout=5000)
    except Exception:
        return None, None
    # Reuse the default context (the user's real browser session)
    context = browser.contexts[0] if browser.contexts else None
    if context is None:
        browser.close()
        return None, None

    def _close():
        # Only detach; do NOT close the user's browser
        try:
            browser.close()
        except Exception:
            pass

    return context, _close


def _is_logged_in(context) -> bool:
    """True if the context holds a douyin session cookie."""
    try:
        cookies = context.cookies()
    except Exception:
        return False
    return any(c.get("name") == "sessionid" for c in cookies)


def interactive_login(timeout: int = 240, on_notice=None) -> bool:
    """Ensure a douyin login session exists, WITHOUT opening a new window
    whenever possible.

    Order of strategies:
    1. Cookies backup file already has a session -> done, no window.
    2. Read the douyin sessionid DIRECTLY from installed browsers' local
       cookie DBs (Chrome/Edge/Brave...) -> done, no window.
    3. A browser is running with CDP debugging -> open douyin.com as a NEW
       TAB there, wait for the user to log in, save cookies. The user logs
       in inside their own browser window.
    4. Last resort: launch our own visible window (profile-based) and wait
       for login there.

    The session is persisted to the cookies backup file for later headless
    runs. Returns True when a session cookie is seen.
    """
    from playwright.sync_api import sync_playwright

    def _notice(msg: str):
        if on_notice:
            try:
                on_notice(msg)
            except Exception:
                pass

    # 1) Already have a session backed up -> nothing to do
    if _has_saved_session():
        _notice("已从浏览器缓存读取到抖音登录状态，无需重新登录")
        return True

    # 2) Try to read the session from installed browsers' cookie DBs
    if _import_browser_session_to_backup():
        _notice("已从系统浏览器（Chrome/Edge等）读取到抖音登录状态，无需登录")
        return True

    with sync_playwright() as p:
        # 4) Attach to the user's running browser (new tab, no new window)
        context, close_fn = _attach_running_browser(p)
        if context is not None:
            try:
                if _is_logged_in(context):
                    _save_cookies(context)
                    _notice("已在运行中的浏览器里检测到抖音登录状态")
                    return True
                page = context.new_page()
                page.goto("https://www.douyin.com/",
                          wait_until="domcontentloaded", timeout=60000)
                _notice("已在你正在运行的浏览器中打开抖音标签页，请在其中登录（扫码/短信/密码均可）")
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if _is_logged_in(context):
                        _notice("登录成功，正在保存会话，请稍候约 15 秒（请勿关闭页面）...")
                        page.wait_for_timeout(15000)
                        _save_cookies(context)
                        return True
                    page.wait_for_timeout(2000)
                return False
            finally:
                if close_fn:
                    close_fn()

        # 5) Fallback: launch our own visible window (profile-based)
        context = _open_persistent(p, headless=False)
        try:
            if _is_logged_in(context):
                _save_cookies(context)
                return True
            page = context.new_page()
            page.goto("https://www.douyin.com/",
                      wait_until="domcontentloaded", timeout=60000)
            _notice("请在弹出的浏览器窗口中登录抖音（扫码/短信/密码均可），成功后窗口会自动关闭")
            deadline = time.time() + timeout
            while time.time() < deadline:
                if _is_logged_in(context):
                    # give session cookies enough time to settle (>=15s),
                    # then back up before closing the window
                    _notice("登录成功，正在保存会话，请稍候约 15 秒（请勿关闭窗口）...")
                    page.wait_for_timeout(15000)
                    _save_cookies(context)
                    return True
                page.wait_for_timeout(2000)
            return False
        finally:
            try:
                context.close()
            except Exception:
                pass
            _release_profile_lock()


def fetch_profile_videos(
    profile_url: str,
    max_videos: int = 0,
    headless: bool = True,
    scroll_rounds: int = 40,
    progress_cb=None,
    on_notice=None,
) -> dict:
    """Open the profile page headlessly, scroll to load the video list.

    Never opens a visible window. Uses the persistent profile cache, so a
    login done once via interactive_login() is reused forever.

    Args:
        profile_url: canonical or share profile URL
        max_videos: stop after N videos (0 = all)
        headless: kept for compatibility; fetch itself never shows a window
        scroll_rounds: base scroll iterations (auto-extended)
        progress_cb: optional callable(found_count)
        on_notice: optional callable(str) for user-facing status messages

    Returns:
        {"sec_uid", "nickname", "videos": [...], "logged_in": bool}
    """
    from playwright.sync_api import sync_playwright

    def _notice(msg: str):
        if on_notice:
            try:
                on_notice(msg)
            except Exception:
                pass

    canonical = normalize_profile_url(profile_url)

    # If our cookies backup has no session yet, try importing one from the
    # installed browsers' cookie DBs (no window needed).
    if not _has_saved_session():
        try:
            _import_browser_session_to_backup()
        except Exception:
            pass

    videos = {}      # aweme_id -> {"desc":, "url":}
    nickname = ""
    sec_uid = ""
    has_more = {"value": True}  # XHR flag: does the server have more pages?

    with sync_playwright() as p:
        context = _open_persistent(p, headless=True)
        try:
            logged_in = _is_logged_in(context)
            if not logged_in and _has_saved_session():
                # cookies were just restored but not yet visible in
                # context.cookies() until a douyin page loads; optimistic
                logged_in = True
            page = context.new_page()

            # Capture video entries from XHR responses (works even if DOM lazy-renders)
            def _handle_response(resp):
                try:
                    if "aweme/post" not in resp.url and "aweme/list" not in resp.url:
                        return
                    if resp.status != 200:
                        return
                    data = resp.json()
                except Exception:
                    return
                # Track server-side pagination flag so we don't stop too early
                flag = data.get("has_more")
                if flag is None:
                    flag = (data.get("data") or {}).get("has_more")
                if flag is not None:
                    has_more["value"] = bool(flag)
                aweme_list = (
                    data.get("aweme_list")
                    or (data.get("data") or {}).get("aweme_list")
                    or []
                )
                for item in aweme_list:
                    aweme_id = item.get("aweme_id") or item.get("awemeId")
                    if not aweme_id:
                        continue
                    if aweme_id not in videos:
                        play = (item.get("video") or {}).get("play_addr") or {}
                        url_list = play.get("url_list") or []
                        videos[aweme_id] = {
                            "desc": (item.get("desc") or "").strip(),
                            "url": item.get("share_info", {}).get("share_url")
                                   or (url_list[0] if url_list else ""),
                        }
                        if progress_cb:
                            try:
                                progress_cb(len(videos))
                            except Exception:
                                pass

            page.on("response", _handle_response)

            page.goto(canonical, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            try:
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass

            # After the page loads, refresh login flag + back up cookies
            # (a restored session becomes visible once douyin.com responds)
            if not logged_in and _is_logged_in(context):
                logged_in = True
                _save_cookies(context)

            # Best-effort: grab nickname and sec_uid
            try:
                sec_uid = page.evaluate(
                    "() => { const m = location.pathname.match(/user\\/([A-Za-z0-9_-]+)/);"
                    " return m ? m[1] : ''; }"
                )
            except Exception:
                pass
            try:
                nickname = page.evaluate(
                    "() => { const el = document.querySelector('[data-e2e=\"user-info\"] span,"
                    " h1, .author-name, [class*=nickname]');"
                    " return el ? el.textContent.trim().slice(0, 60) : ''; }"
                )
            except Exception:
                pass

            # Robust scroll loop:
            # - keeps scrolling while the server says has_more=True (even if
            #   a scroll round fetched nothing, e.g. slow network)
            # - only stops early when has_more is False AND count is stable
            prev_count = -1
            stable = 0
            max_rounds = max(scroll_rounds, 160)  # never give up too early
            for i in range(max_rounds):
                if max_videos and len(videos) >= max_videos:
                    break
                # Douyin's list lives in an inner scroll container; scrolling
                # window alone is often not enough, so scroll every candidate.
                try:
                    page.evaluate(
                        "() => {"
                        "  window.scrollTo(0, document.body.scrollHeight);"
                        "  const els = document.querySelectorAll("
                        "    '#douyin-right-container, [class*=route-scroll], [class*=scroll]');"
                        "  for (const el of els) {"
                        "    if (el.scrollHeight > el.clientHeight + 50) el.scrollTop = el.scrollHeight;"
                        "  }"
                        "}"
                    )
                except Exception:
                    pass
                page.mouse.wheel(0, 8000)
                page.wait_for_timeout(1200)
                cur = len(videos)
                if cur == prev_count:
                    stable += 1
                    if has_more["value"]:
                        # Server still reports more pages -> be very patient
                        # (rate-limited/slow responses can lag several rounds).
                        if stable >= 20:
                            break
                    else:
                        # No more pages from the server + count stable -> done
                        if stable >= 4:
                            break
                else:
                    stable = 0
                    prev_count = cur

            # Final settle pass: flush late XHR responses that were still in
            # flight when the main loop decided to stop.
            for _ in range(5):
                try:
                    page.evaluate(
                        "() => {"
                        "  window.scrollTo(0, document.body.scrollHeight);"
                        "  const els = document.querySelectorAll("
                        "    '#douyin-right-container, [class*=route-scroll], [class*=scroll]');"
                        "  for (const el of els) {"
                        "    if (el.scrollHeight > el.clientHeight + 50) el.scrollTop = el.scrollHeight;"
                        "  }"
                        "}"
                    )
                except Exception:
                    pass
                page.wait_for_timeout(1500)

            if not logged_in:
                _notice("未检测到抖音登录状态，可能只抓到第一页；点「登录抖音」一次即可抓全")
        finally:
            # Detach the XHR listener before shutting the browser down: sync
            # Playwright runs "response" handlers on its dispatcher, and a
            # response that lands while close() is in flight re-enters that
            # dispatcher from inside the handler - which deadlocks close().
            try:
                page.remove_listener("response", _handle_response)
            except Exception:
                pass
            # Persist any douyin cookies earned during this session
            _save_cookies(context)
            # After driving a douyin page, Chromium's graceful shutdown never
            # finishes: context.close() blocks forever (verified - it still
            # hangs after force-killing the browser). So kill the browser and
            # let the sync_playwright() block exit, which stops the driver
            # instantly and releases the profile dir.
            try:
                _kill_profile_browsers()
            finally:
                # Must always run: a leaked lock freezes every later batch
                _release_profile_lock()

    return {
        "sec_uid": sec_uid,
        "nickname": nickname,
        "videos": [
            {"aweme_id": vid, **meta} for vid, meta in videos.items()
        ],
        "logged_in": logged_in,
    }
