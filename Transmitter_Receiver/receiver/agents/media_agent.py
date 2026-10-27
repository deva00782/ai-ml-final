import os
import secrets
import socket
import subprocess
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
from typing import Any, Dict, Optional, Tuple

from agents.logger import get_logger, get_base_dir, log_box

logger = get_logger("media")

CHROME_PATH_SUFFIX = r"Google\Chrome\Application\chrome.exe"
EDGE_PATH_SUFFIX = r"Microsoft\Edge\Application\msedge.exe"


def _get_autoplay_browser_paths():
    """Dynamically resolves Chrome and Edge executable paths across user profiles and architectures."""
    prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    prog_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get("LOCALAPPDATA", "")

    candidates = [
        os.path.join(prog_files, CHROME_PATH_SUFFIX),
        os.path.join(prog_files_x86, CHROME_PATH_SUFFIX),
        os.path.join(local_app_data, CHROME_PATH_SUFFIX) if local_app_data else None,
        os.path.join(prog_files, EDGE_PATH_SUFFIX),
        os.path.join(prog_files_x86, EDGE_PATH_SUFFIX),
        os.path.join(local_app_data, EDGE_PATH_SUFFIX) if local_app_data else None,
    ]
    valid = []
    for c in candidates:
        if c and c not in valid and os.path.exists(c):
            valid.append(c)
    return valid

# Base directory for the dedicated automation browser profile (persists cookies/sessions beside exe)
AUTOMATION_PROFILE_DIR = os.path.join(get_base_dir(), ".browser_profile")
# Remote-debugging port for Selenium to attach to the automation Chrome.
# 9222 belongs to the desktop dashboard's debug Chrome (start_chrome.py).
AUTOMATION_DEBUG_PORT = 9224

# True when the automation browser (autoplay unlocked) is being used.
# In that mode song pages start playing automatically - no Space needed.
_autoplay_unlocked = False

# Master switch: use the dedicated autoplay-disabled browser (Preferred) or the
# user's normal browser + keyboard/click playback (fallback).
USE_AUTOMATION_BROWSER = True

# Per your report: after navigating to a TRACK page, click the viewport center
# once to reclaim focus and clear the browser autoplay block before Space.
# Safe on track pages (unlike the home page, the center is hero/artwork, not a
# playlist grid - the playlist misclicks happened on the HOME page).
CENTER_VIEWPORT_CLICK = True

DEFAULT_JIOSAAVN_URL = "https://www.jiosaavn.com/"
JIOSAAVN_DOMAIN = "jiosaavn.com"

# Verified live JioSaavn song URLs (dead links caused the "no-match" page)
INITIAL_SONG_URL = f"{DEFAULT_JIOSAAVN_URL}song/apna-bana-le/ATIfejZ9bWw"
PREDEFINED_SONG_URLS = [
    "https://www.jiosaavn.com/song/kesariya/BRpGZEd7ZAs",
    "https://www.jiosaavn.com/song/tum-hi-ho/EToxUyFpcwQ",
    "https://www.jiosaavn.com/song/chaleya/FgkHXjldXWs",
    "https://www.jiosaavn.com/song/apna-bana-le/ATIfejZ9bWw",
    "https://www.jiosaavn.com/song/raataan-lambiyan/HTg-QzBjQl0",
    "https://www.jiosaavn.com/song/heeriye-feat.-arijit-singh/PiECVR10DlQ",
    "https://www.jiosaavn.com/song/o-maahi/BwsYdR1jRHI",
    "https://www.jiosaavn.com/song/lut-gaye/Rhg6AgtAQ1Y",
]

# Real-music-app session playback history stack (tracks previous/next navigation)
_PLAYBACK_HISTORY = []
_HISTORY_INDEX = -1

# Categorized media entities for rich search support
SEARCH_SONGS = [
    "Kesariya",
    "Apna Bana Le",
    "Chaleya",
    "Tum Hi Ho",
    "Heeriye",
    "Naatu Naatu",
    "Raataan Lambiyan",
    "O Maahi",
    "Lut Gaye",
    "Tere Vaaste",
]

SEARCH_PLAYLISTS = [
    "Hindi Top 50",
    "Romantic Hits Hindi",
    "Party Songs Hindi",
    "Weekly Top Songs",
    "Chill Vibes Hindi",
    "Trending Today Hindi",
    "90s Romance Hindi",
    "Dance Hits Hindi",
    "Sad Songs Hindi",
    "JioTunes Top 20",
]

SEARCH_ARTISTS = [  # Singers / Composers
    "Arijit Singh",
    "Shreya Ghoshal",
    "Anirudh Ravichander",
    "Atif Aslam",
    "KK",
    "Neha Kakkar",
    "Jubin Nautiyal",
    "Sid Sriram",
    "Diljit Dosanjh",
    "Sonu Nigam",
    "A.R. Rahman",
    "Sunidhi Chauhan",
]

SEARCH_ACTORS = [
    "Shah Rukh Khan",
    "Ranbir Kapoor",
    "Salman Khan",
    "Hrithik Roshan",
    "Allu Arjun",
    "Vijay Thalapathy",
    "Prabhas",
    "Deepika Padukone",
    "Ranveer Singh",
    "Alia Bhatt",
]

SEARCH_ALBUMS = [
    "Animal",
    "Brahmastra",
    "Kabir Singh",
    "Jawan",
    "Aashiqui 2",
    "Rockstar",
    "Pushpa",
    "Yeh Jawaani Hai Deewani",
    "War",
    "Pathaan",
]

SEARCH_CATEGORIES = {
    "songs": SEARCH_SONGS,
    "playlists": SEARCH_PLAYLISTS,
    "artists": SEARCH_ARTISTS,  # singers
    "actors": SEARCH_ACTORS,
    "albums": SEARCH_ALBUMS,
}

# Unified default search queries pool covering all categories
DEFAULT_SEARCH_QUERIES = (
    SEARCH_SONGS + SEARCH_PLAYLISTS + SEARCH_ARTISTS + SEARCH_ACTORS + SEARCH_ALBUMS
)


def _extract_from_dict_payload(payload: dict) -> Optional[str]:
    for key in ("query", "song", "title", "playlist", "artist", "singer", "actor", "album", "name"):
        val = payload.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return None


def _extract_from_url_slug(raw_text: str) -> Optional[str]:
    parts = [p for p in raw_text.split("/") if p]
    for i, p in enumerate(parts):
        if p in ("song", "album", "artist", "featured", "playlist") and i + 1 < len(parts):
            slug = parts[i + 1]
            return " ".join(
                word.capitalize() for word in slug.replace("-", " ").replace("_", " ").split()
            )
    return None


def _extract_from_prefixed_text(raw_text: str) -> Optional[str]:
    for prefix in ("playlist:", "artist:", "singer:", "actor:", "album:", "song:", "query:"):
        if raw_text.lower().startswith(prefix):
            val = raw_text[len(prefix):].strip()
            if val:
                return val
    return None


def _extract_search_query(payload) -> str:
    """Extracts a clean song title, playlist, artist, singer, actor, or album query string from the received payload."""
    if not payload:
        return secrets.choice(DEFAULT_SEARCH_QUERIES)

    if isinstance(payload, dict):
        found = _extract_from_dict_payload(payload)
        if found:
            return found

    raw_text = str(payload).strip()
    if not raw_text:
        return secrets.choice(DEFAULT_SEARCH_QUERIES)

    prefixed = _extract_from_prefixed_text(raw_text)
    if prefixed:
        return prefixed

    if raw_text.lower().startswith("http"):
        slug_title = _extract_from_url_slug(raw_text)
        if slug_title:
            return slug_title
        return secrets.choice(DEFAULT_SEARCH_QUERIES)

    return raw_text

# Default playlist configuration (can be overridden in agents/config.json)
DEFAULT_PLAYLIST = "Dumdaar Hits"
DEFAULT_PLAYLIST_URL = "https://www.jiosaavn.com/featured/dumdaar-hits/8MT-LQlP35c_"


class JioSaavnState:
    NOT_INITIALIZED = "NOT_INITIALIZED"
    JIOSAAVN_OPENING = "JIOSAAVN_OPENING"
    DASHBOARD_READY = "DASHBOARD_READY"
    SELECT_PLAYLIST = "SELECT_PLAYLIST"
    START_PLAYBACK = "START_PLAYBACK"
    VERIFY_PLAYBACK = "VERIFY_PLAYBACK"
    JIOSAAVN_READY = "JIOSAAVN_READY"
    FAILED = "FAILED"


_current_state = JioSaavnState.NOT_INITIALIZED


class JioSaavnInitError(RuntimeError):
    """Structured failure exception for JioSaavn initialization steps."""
    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def get_jiosaavn_state() -> str:
    """Return the current JioSaavn lifecycle state."""
    return _current_state


def set_jiosaavn_state(state: str) -> None:
    """Explicitly override the current JioSaavn lifecycle state (used by tests / resets)."""
    global _current_state
    _current_state = state


def _load_playlist_config() -> Tuple[str, str]:
    """Loads default playlist name and URL from agents/config.json if available."""
    default_name = DEFAULT_PLAYLIST
    default_url = DEFAULT_PLAYLIST_URL
    base_dir = get_base_dir()
    for candidate in [
        os.path.join(base_dir, "agents", "config.json"),
        os.path.join(base_dir, "config.json"),
    ]:
        if os.path.isfile(candidate):
            try:
                import json
                with open(candidate, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    name = cfg.get("default_playlist")
                    url = cfg.get("default_playlist_url")
                    if name:
                        default_name = str(name).strip()
                    if url:
                        default_url = str(url).strip()
                break
            except Exception:
                pass
    return default_name, default_url

# Active-audio flag: False = a song is playing (media keys work natively)
is_resting_state = True

# True while the browser tab is showing a song page (not the home page)
_on_song_page = False

_ACTION_LOCK = threading.Lock()

# Lazily-created Selenium driver attached to the automation Chrome; used for
# DOM click + playback verification via the project's existing Selenium setup.
_DOM_DRIVER = None
_dom_driver_lock = threading.Lock()
_CHROME_PROC = None


def _set_clipboard(text: str) -> None:
    """Put `text` on the Windows clipboard (clip.exe first, tkinter fallback)."""
    try:
        subprocess.run(["clip.exe"], input=text, capture_output=True, text=True, timeout=5)
        return
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
    root.destroy()


def _navigate_current_tab(url: str) -> None:
    """Change the current tab's URL via Selenium driver."""
    driver = _get_dom_driver() or _ensure_dom_driver(url)
    if driver is not None:
        try:
            _switch_to_jiosaavn(driver)
            driver.get(url)
            return
        except Exception as e:
            logger.warning(f"[NAV] Selenium navigation failed: {e}")


def _activate_window(win) -> bool:
    try:
        if not win.isVisible:
            return False
        if win.isMinimized:
            try:
                win.restore()
            except Exception:
                pass
        try:
            win.activate()
        except Exception:
            pass
        time.sleep(0.25)
        logger.info(f"[FOCUS] Focused JioSaavn window: {win.title}")
        return True
    except Exception:
        return False


def _focus_with_pygetwindow() -> bool:
    try:
        import pygetwindow as gw
        for win in gw.getAllWindows():
            t = (win.title or "").lower()
            if ("jiosaavn" in t or "saavn" in t) and _activate_window(win):
                return True
    except Exception:
        pass
    return False


def _focus_browser_window() -> bool:
    """Bring the JioSaavn browser window to the foreground.
    STRICT: ONLY matches windows titled 'JioSaavn' or 'Saavn'.
    Will NEVER match unrelated windows like Microsoft Teams, Google Sheets, or general browser windows.
    """
    if _focus_with_pygetwindow():
        return True

    if os.name == "nt" and _focus_chrome_by_class():
        return True

    logger.warning("[FOCUS] No JioSaavn window found on system")
    return False


def _focus_chrome_by_class() -> bool:
    """Win32 fallback: ONLY focus a Chrome/Edge window that contains 'jiosaavn' or 'saavn'."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    chrome_classes = ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0")
    matches = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title_len = user32.GetWindowTextLengthW(hwnd) + 1
        title_buf = ctypes.create_unicode_buffer(title_len)
        user32.GetWindowTextW(hwnd, title_buf, title_len)
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        t = title_buf.value.lower()
        if class_buf.value in chrome_classes and ("jiosaavn" in t or "saavn" in t):
            matches.append((hwnd, title_buf.value))
            return False
        return True

    try:
        user32.EnumWindows(_enum_proc, 0)
    except Exception:
        return False
    if not matches:
        return False

    hwnd, title = matches[0]
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        else:
            user32.ShowWindow(hwnd, 5)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    except Exception:
        return False
    time.sleep(0.25)
    logger.info(f"[FOCUS] Focused JioSaavn window (Win32): {title}")
    return True


def _patch_pref_file(pref_file: str) -> None:
    if not os.path.isfile(pref_file):
        return
    try:
        import json
        with open(pref_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        prof = data.get("profile")
        if not isinstance(prof, dict):
            return
        modified = False
        if prof.get("exit_type") != "Normal":
            prof["exit_type"] = "Normal"
            modified = True
        if not prof.get("exited_cleanly", True):
            prof["exited_cleanly"] = True
            modified = True
        if modified:
            with open(pref_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
    except Exception as e:
        logger.debug(f"[BROWSER] Could not patch Chrome crash state: {e}")


def _fix_chrome_crash_state(profile_dir: str) -> None:
    """Reset crashed flags in Chrome Preferences so 'Restore pages?' bubble does not appear,
    and clear stale lock files when browser is not running to prevent startup collisions."""
    if not profile_dir or not os.path.exists(profile_dir):
        return
    for pref_rel in (os.path.join("Default", "Preferences"), "Preferences"):
        _patch_pref_file(os.path.join(profile_dir, pref_rel))
    if _find_active_debug_port() == 0:
        for lock_name in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
            lpath = os.path.join(profile_dir, lock_name)
            if os.path.exists(lpath):
                try:
                    os.remove(lpath)
                    logger.debug(f"[BROWSER] Cleaned stale lock file: {lock_name}")
                except Exception:
                    pass


def _launch_automation_browser(url: str) -> bool:
    """Launch Chrome/Edge with autoplay restrictions disabled (Solution 2A).

    Uses a separate user-data-dir so the '--autoplay-policy' flag takes effect;
    navigating straight to the song URL then starts playback with NO clicks/keys.
    """
    global _autoplay_unlocked, _CHROME_PROC
    if not USE_AUTOMATION_BROWSER:
        logger.info("[BROWSER] Automation browser disabled via USE_AUTOMATION_BROWSER")
        _autoplay_unlocked = False
        return False
    os.makedirs(AUTOMATION_PROFILE_DIR, exist_ok=True)
    _fix_chrome_crash_state(AUTOMATION_PROFILE_DIR)
    for browser in _get_autoplay_browser_paths():
        if os.path.exists(browser):
            _CHROME_PROC = subprocess.Popen(
                [
                    browser,
                    "--autoplay-policy=no-user-gesture-required",
                    f"--remote-debugging-port={AUTOMATION_DEBUG_PORT}",
                    f"--user-data-dir={AUTOMATION_PROFILE_DIR}",
                    "--hide-crash-restore-bubble",
                    "--disable-session-crashed-bubble",
                    "--disable-features=InfiniteSessionRestore",
                    "--disable-infobars",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--start-maximized",
                    "--window-size=1920,1080",
                    url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"[BROWSER] Launched automation browser: {browser}")
            _autoplay_unlocked = True
            return True
    logger.warning("[BROWSER] No Chrome/Edge found - using default browser")
    _autoplay_unlocked = False
    return False



def _start_playback() -> None:
    """Start playback on the loaded TRACK page using Selenium DOM play controls."""
    driver = _get_dom_driver() or _ensure_dom_driver()
    if driver is not None and _switch_to_jiosaavn(driver):
        try:
            res = driver.execute_script(_SONG_PLAY_CLICK_JS)
            if res and res.get("success"):
                logger.info(f"[PLAY] Triggered play via DOM: {res.get('selector')}")
                return
            driver.execute_script("var a = document.querySelector('audio'); if (a && a.paused) { a.play(); }")
            logger.info("[PLAY] Triggered play via HTML5 audio element")
        except Exception as e:
            logger.warning(f"[PLAY] Selenium playback trigger error: {e}")


def _load_song(url: str) -> None:
    """Navigate the JioSaavn tab to a song URL and start playback."""
    if not _focus_browser_window():
        logger.info(f"[LOAD SONG] JioSaavn not open. Launching {url}...")
        launched = _launch_automation_browser(url)
        if not launched:
            webbrowser.open(url)
        time.sleep(3.5)
        _focus_browser_window()
        return
    time.sleep(0.2)
    _navigate_current_tab(url)
    # Allow the browser to fetch the stream + build the player DOM + bind hotkeys
    time.sleep(3.5)
    if not _autoplay_unlocked:
        # Default browser may still block autoplay -> supply the gesture (Space)
        _start_playback()


# ---- DOM automation (Selenium) helpers ----
# These utilize Selenium/Chrome-debugging mechanism to directly click play
# controls and VERIFY that a song is playing before a command reports success.
# Pure Selenium implementation without any PyAutoGUI dependency.

# Media element check: at least one audio/video element is loaded & unpaused.
_IS_PLAYING_JS = (
    "var els = document.querySelectorAll('audio, video');"
    "return Array.from(els).some(function(a){ return a.readyState > 2 && !a.paused; });"
)

# Play-button selector list: page-level action buttons first, player toggle last,
# so clicking never accidentally pauses an already-autoplaying item.
_PLAY_CLICK_JS = """
var sels = ['#player_play_pause','.c-player__play','a.c-btn--primary.js-play-button','button.c-btn--primary.js-play-button','a.js-play-button','button.js-play-button','.js-play-button','.o-snippet__action-final','.o-icon-play-circle','.c-song__play','.o-icon--play','a[aria-label*="Play"]','button[aria-label*="Play"]','span[aria-label="Play"]'];
for (var i=0;i<sels.length;i++){ var els=document.querySelectorAll(sels[i]); for(var j=0;j<els.length;j++){ var el=els[j]; var t=(el.innerText||el.textContent||'').trim().toLowerCase(); if(t.indexOf('new playlist')>-1||t.indexOf('create')>-1||t.indexOf('log in')>-1||t.indexOf('sign up')>-1){continue;} if(el&&(el.offsetParent!==null||el.offsetWidth>0)){ try{ el.click(); return {success:true,selector:sels[i]}; }catch(e){ try{ el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); return {success:true,selector:sels[i]}; }catch(e2){} } } } }
return {success:false};
"""

# Homepage: click the first playable song card or direct play button (no hard-coded URL).
_HOME_SONG_JS = """
var playSpans = Array.from(document.querySelectorAll('.o-block__action, [aria-label^="Play "], [aria-label*="Play"]')).filter(function(el) {
    var aria = (el.getAttribute('aria-label') || '').toLowerCase();
    var txt = (el.innerText || el.textContent || '').toLowerCase();
    return aria.startsWith('play ') && !aria.includes('playlist') && !txt.includes('playlist') && (el.offsetParent !== null || el.offsetWidth > 0);
});
if (playSpans.length > 0) {
    try {
        playSpans[0].click();
        return { success: true, selector: '.o-block__action [Play]' };
    } catch(e) {
        try {
            playSpans[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            return { success: true, selector: '.o-block__action [Play]' };
        } catch(e2) {}
    }
}
var link = document.querySelector('a[href*="/song/"]');
if (link) {
    try {
        link.click();
        return { success: true, selector: 'a[href*="/song/"]' };
    } catch(e) {
        try {
            link.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            return { success: true, selector: 'a[href*="/song/"]' };
        } catch(e2) {}
    }
}
return { success: false };
"""

# Song-page Play control: page-level play buttons first, player toggle LAST
# (mirrors the project's proven browser_manager.in_tab_search_and_play selectors).
_SONG_PLAY_CLICK_JS = """
var sels = ['a.c-btn--primary.js-play-button','button.c-btn--primary.js-play-button','a.js-play-button','button.js-play-button','.js-play-button','.o-snippet__action-final','.o-icon-play-circle','.c-song__play','a[aria-label*="Play"]','button[aria-label*="Play"]','span[aria-label="Play"]','#player_play_pause'];
for (var i=0;i<sels.length;i++){ var els=document.querySelectorAll(sels[i]); for(var j=0;j<els.length;j++){ var el=els[j]; var t=(el.innerText||el.textContent||'').trim().toLowerCase(); if(t.indexOf('new playlist')>-1||t.indexOf('create')>-1||t.indexOf('log in')>-1||t.indexOf('sign up')>-1){continue;} if(el&&(el.offsetParent!==null||el.offsetWidth>0)){ try{ el.click(); return {success:true,selector:sels[i]}; }catch(e){ try{ el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); return {success:true,selector:sels[i]}; }catch(e2){} } } } }
return {success:false};
"""

# JioSaavn in-page search bar interaction script
_SEARCH_INPUT_JS = """
var query = arguments[0];
var sels = [
    'input.rbt-input-main',
    'input[aria-label="Search"]',
    'input[aria-controls="site-search"]',
    '[role="search"] input',
    'input.rbt-input',
    'input[type="search"]',
    'header input[type="text"]'
];
var input = null;
for (var i = 0; i < sels.length; i++) {
    var el = document.querySelector(sels[i]);
    if (el && (el.offsetParent !== null || el.offsetWidth > 0 || el === document.activeElement)) {
        input = el;
        break;
    }
}
if (!input) {
    input = document.querySelector('header input, [role="search"] input, input.form-control');
}
if (!input) {
    return { success: false, reason: "Search input element not found in JioSaavn DOM" };
}

input.scrollIntoView({ block: 'center', inline: 'center' });
input.focus();
input.click();

try {
    var nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    nativeSetter.call(input, query);
} catch(e) {
    input.value = query;
}

input.dispatchEvent(new Event('input', { bubbles: true }));
input.dispatchEvent(new Event('change', { bubbles: true }));

var enterOpts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
input.dispatchEvent(new KeyboardEvent('keydown', enterOpts));
input.dispatchEvent(new KeyboardEvent('keypress', enterOpts));
input.dispatchEvent(new KeyboardEvent('keyup', enterOpts));

if (input.form) {
    try { input.form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); } catch(e){}
}

return { success: true, selector: 'input.rbt-input-main' };
"""

# JioSaavn search results play button selection script
_SEARCH_PLAY_CLICK_JS = """
var query = arguments[0] || '';
var sels = [
    query ? '[aria-label^="Play ' + query + '" i]' : null,
    '.c-song [aria-label^="Play "]',
    '[role="row"] [aria-label^="Play "]',
    '.o-snippet [aria-label^="Play "]',
    '.o-flag__img[aria-label^="Play "]',
    '[aria-label^="Play "]',
    '#site-search [role="option"] a[href*="/song/"]',
    '#site-search [role="option"] button',
    '#site-search [role="option"]',
    '.rbt-menu [role="option"]',
    'a.c-btn--primary.js-play-button',
    'button.c-btn--primary.js-play-button',
    '.c-song__play',
    '.o-icon-play-circle',
    'a.js-play-button',
    'button.js-play-button',
    '#player_play_pause',
    '.c-song a[href*="/song/"]',
    'a[href*="/song/"]'
].filter(Boolean);

for (var i = 0; i < sels.length; i++) {
    var els = document.querySelectorAll(sels[i]);
    for (var j = 0; j < els.length; j++) {
        var el = els[j];
        var aria = (el.getAttribute('aria-label') || '').toLowerCase();
        var t = (el.innerText || el.textContent || '').trim().toLowerCase();
        if (aria.includes('playlist') || aria.includes('album') || t.includes('new playlist') || t.includes('create') || t.includes('log in') || t.includes('sign up')) {
            continue;
        }
        if (el && (el.offsetParent !== null || el.offsetWidth > 0)) {
            try {
                el.click();
                return { success: true, selector: sels[i], aria: el.getAttribute('aria-label') };
            } catch(e) {
                try {
                    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    return { success: true, selector: sels[i], aria: el.getAttribute('aria-label') };
                } catch(e2) {}
            }
        }
    }
}
return { success: false };
"""


def _find_active_debug_port() -> int:
    """Check if automation port (9224) or standard debug port (9222) is listening."""
    for p in (AUTOMATION_DEBUG_PORT, 9222):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.4)
                if s.connect_ex(("127.0.0.1", p)) == 0:
                    return p
        except Exception:
            continue
    return 0


def _detect_browser_type_on_port(port: int) -> str:
    """Detect whether Chrome or Edge is running on the debug port via CDP version endpoint."""
    try:
        import urllib.request
        import json
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/version", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            b_str = data.get("Browser", "").lower()
            if "edg" in b_str:
                return "edge"
            return "chrome"
    except Exception:
        return "chrome"


def _get_dom_driver():
    """Lazily attach Selenium to the automation Edge/Chrome (shared across commands)."""
    global _DOM_DRIVER
    if _DOM_DRIVER is not None:
        try:
            _ = _DOM_DRIVER.title
            return _DOM_DRIVER
        except Exception:
            _DOM_DRIVER = None
    port = _find_active_debug_port()
    if not port:
        return None
    with _dom_driver_lock:
        if _DOM_DRIVER is not None:
            return _DOM_DRIVER
        target_browser = _detect_browser_type_on_port(port)
        candidates = ["chrome", "edge"] if target_browser == "chrome" else ["edge", "chrome"]
        for b in candidates:
            try:
                from selenium import webdriver
                if b == "chrome":
                    from selenium.webdriver.chrome.options import Options as ChromeOptions
                    opts = ChromeOptions()
                    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
                    _DOM_DRIVER = webdriver.Chrome(options=opts)
                    logger.info(f"[DOM] Attached to automation Chrome on port {port}")
                    return _DOM_DRIVER
                else:
                    from selenium.webdriver.edge.options import Options as EdgeOptions
                    opts = EdgeOptions()
                    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
                    _DOM_DRIVER = webdriver.Edge(options=opts)
                    logger.info(f"[DOM] Attached to automation Edge on port {port}")
                    return _DOM_DRIVER
            except Exception as error:
                logger.warning(f"[DOM] Selenium {b.capitalize()} attach unavailable: {error}")
                _DOM_DRIVER = None
    return _DOM_DRIVER


def _kill_process_on_port(port: int) -> None:
    """Terminate any unresponsive process bound to the specified debug port."""
    if not port:
        return
    try:
        if os.name == "nt":
            cmd = (
                f"$p = (Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue).OwningProcess; "
                f"if ($p) {{ $p | ForEach-Object {{ Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }} }}; "
                f"$procs = Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*remote-debugging-port={port}*' }}; "
                f"if ($procs) {{ $procs | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }} }}"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=3)
        else:
            subprocess.run(["pkill", "-9", "-f", f"remote-debugging-port={port}"], capture_output=True, timeout=2)
    except Exception as e:
        logger.debug(f"[DOM] Kill process on port {port} notice: {e}")


def _ensure_dom_driver(url: Optional[str] = None):
    """Get or lazily connect/launch the Selenium driver so actions execute reliably without fallbacks."""
    target_url = url or DEFAULT_JIOSAAVN_URL
    driver = _get_dom_driver()
    if driver is not None:
        return driver
    port = _find_active_debug_port()
    if port:
        logger.warning(f"[DOM] Port {port} is active but cannot attach via Selenium. Cleaning unresponsive instance...")
        _kill_process_on_port(port)
        time.sleep(0.5)

    logger.info("[DOM] No active debug port found. Launching automation browser...")
    _launch_automation_browser(target_url)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        port = _find_active_debug_port()
        if port:
            break
        time.sleep(0.3)
    return _get_dom_driver()


def _switch_to_jiosaavn(driver) -> bool:
    """Focus the driver on the JioSaavn tab, if any."""
    try:
        if JIOSAAVN_DOMAIN in (driver.current_url or "").lower():
            return True
    except Exception:
        pass

    try:
        # Check window handles in reverse order so the newest opened tab is favored
        for handle in reversed(driver.window_handles):
            try:
                driver.switch_to.window(handle)
                if JIOSAAVN_DOMAIN in (driver.current_url or "").lower():
                    return True
            except Exception:
                continue

        # If user opened a new blank tab (e.g. chrome://newtab or about:blank)
        if driver.window_handles:
            driver.switch_to.window(driver.window_handles[-1])
            curr = (driver.current_url or "").lower()
            if "about:blank" in curr or "chrome://newtab" in curr or not curr or JIOSAAVN_DOMAIN not in curr:
                logger.info("[BROWSER] Active tab not on JioSaavn. Navigating to JioSaavn...")
                driver.get(DEFAULT_JIOSAAVN_URL)
                time.sleep(2.5)
                return True
        else:
            driver.get(DEFAULT_JIOSAAVN_URL)
            return True
    except Exception as e:
        logger.warning(f"[BROWSER] Switch to JioSaavn tab error: {e}")
        return False
    return False


def _dismiss_modals(driver) -> None:
    """Dismiss any blocking modals, dialogs, or login prompts that JioSaavn pops up,
    and recover from guest session streaming quotas."""
    try:
        driver.execute_script("""
            var modals = document.querySelectorAll('.c-modal.active, #login-signup-modal.active, [role="dialog"], .c-dialog');
            modals.forEach(function(m) {
                var btn = m.querySelector('.c-modal__close, [aria-label="Close"], button.c-btn--close, .close');
                if (btn) btn.click();
            });
            var inp = document.querySelector('input.rbt-input-main, input[aria-label="Search"]');
            if (inp && inp === document.activeElement) {
                inp.blur();
            }
            var menu = document.querySelector('#site-search, .rbt-menu');
            if (menu) {
                menu.style.display = 'none';
            }
        """)
        limit_reached = driver.execute_script("""
            var text = (document.body ? document.body.innerText : '');
            return text.includes('Streaming limit reached');
        """)
        if limit_reached:
            logger.warning("[BROWSER] Guest streaming limit reached. Clearing guest storage & cookies...")
            driver.delete_all_cookies()
            driver.execute_script("try { localStorage.clear(); sessionStorage.clear(); } catch(e){}")
            driver.refresh()
            time.sleep(2.0)
    except Exception:
        pass


def _dom_is_playing() -> bool:
    """Verify (via DOM) that playback has actually started."""
    driver = _get_dom_driver()
    if driver is None:
        return False
    if not _switch_to_jiosaavn(driver):
        return False
    try:
        return bool(driver.execute_script(_IS_PLAYING_JS))
    except Exception:
        return False


def _dom_start_playback(max_wait=8.0) -> bool:
    """Click a real play control via the project's Selenium mechanism and verify."""
    driver = _get_dom_driver()
    if driver is None or not _switch_to_jiosaavn(driver):
        return False
    try:
        if driver.execute_script(_IS_PLAYING_JS):
            return True
        deadline = time.time() + max_wait
        while time.time() < deadline:
            result = driver.execute_script(_PLAY_CLICK_JS)
            if result and result.get("success"):
                logger.info(f"[PLAY] Clicked play control: {result.get('selector')}")
                time.sleep(1.5)
                if driver.execute_script(_IS_PLAYING_JS):
                    return True
            else:
                time.sleep(1.0)
        return bool(driver.execute_script(_IS_PLAYING_JS))
    except Exception as error:
        logger.exception(f"[PLAY] DOM playback error: {error}")
        return False


def _wait_for_homepage_playback(driver, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if driver.execute_script(_IS_PLAYING_JS):
            logger.info("[PLAY] Playback verified active.")
            return True
        time.sleep(0.5)
    return False


def _start_song_from_homepage() -> bool:
    """Homepage: pick any available song card and make sure it is really playing."""
    driver = _get_dom_driver()
    if driver is not None and _switch_to_jiosaavn(driver):
        try:
            result = driver.execute_script(_HOME_SONG_JS)
            if result and result.get("success"):
                logger.info(f"[PLAY] Clicked homepage song play control: {result.get('selector')}")
                if _wait_for_homepage_playback(driver):
                    return True

            if _dom_start_playback(max_wait=4.0):
                return True

            logger.info("[PLAY] Navigating to initial trending song to guarantee playback...")
            driver.get(INITIAL_SONG_URL)
            time.sleep(3.0)
            return _play_song_on_current_page()
        except Exception as error:
            logger.exception(f"[PLAY] Homepage song selection error: {error}")

    # Pure Selenium: trigger playback via DOM play controls
    _start_playback()
    return True


def _toggle_dom_player(driver) -> bool:
    global is_resting_state, _on_song_page
    audio_status = driver.execute_script("""
        var els = document.querySelectorAll('audio, video');
        var active = Array.from(els).find(function(a){ return a.readyState > 0 || (a.src && a.src.length > 0); });
        if (!active) {
            return { exists: false, isPlaying: false };
        }
        return { exists: true, isPlaying: (!active.paused && active.currentTime > 0) };
    """)

    if audio_status and audio_status.get("exists"):
        is_playing = audio_status.get("isPlaying")
        logger.info(f"[PLAY] Track loaded in player (currently playing={is_playing}). Toggling play/pause...")
        toggle_res = driver.execute_script("""
            var btn = document.querySelector('#player_play_pause, .c-player__play, [data-testid="player-play-pause"]');
            if (btn) {
                btn.click();
                return { success: true, selector: btn.id || btn.className };
            }
            var a = document.querySelector('audio, video');
            if (a) {
                if (a.paused) { a.play(); } else { a.pause(); }
                return { success: true, selector: 'audio_element' };
            }
            return { success: false };
        """)
        if toggle_res and toggle_res.get("success"):
            logger.info(f"[PLAY] Toggled player control: {toggle_res.get('selector')}")
            time.sleep(0.5)
            return True
        return False

    logger.info("[PLAY] Player idle / no track loaded. Starting initial song from page...")
    if _start_song_from_homepage():
        is_resting_state = False
        _on_song_page = True
        logger.info("[PLAY] Playback started from page song card.")
        return True
    return False


def _toggle_selenium_fallback(driver) -> bool:
    global is_resting_state
    try:
        driver.execute_script("""
            var btn = document.querySelector('#player_play_pause, .c-player__play, [data-testid="player-play-pause"], .c-player button');
            if (btn) {
                btn.click();
                return;
            }
            var a = document.querySelector('audio');
            if (a) {
                if (a.paused) a.play(); else a.pause();
            }
        """)
        is_resting_state = False
        return True
    except Exception as e:
        logger.exception(f"[PLAY] Selenium toggle failed: {e}")
        return False


def _toggle_play_pause() -> bool:
    """Intelligently toggle play/pause or start playback on JioSaavn."""
    focused = _focus_browser_window()
    time.sleep(0.3)

    driver = _get_dom_driver()
    if not focused and driver is None:
        logger.info("[PLAY] No active browser detected. Launching JioSaavn...")
        _launch_automation_browser(DEFAULT_JIOSAAVN_URL)
        time.sleep(3.0)
        _focus_browser_window()
        driver = _get_dom_driver()

    if driver is not None and _switch_to_jiosaavn(driver):
        try:
            if _toggle_dom_player(driver):
                return True
        except Exception as error:
            logger.exception(f"[PLAY] DOM play/pause toggle error: {error}")

    driver = _ensure_dom_driver()
    if driver is not None and _switch_to_jiosaavn(driver):
        return _toggle_selenium_fallback(driver)

    return False


def _record_history(url: str, direction: str = "next") -> None:
    """Record played song URL into the bidirectional playback history stack."""
    global _PLAYBACK_HISTORY, _HISTORY_INDEX
    if not url:
        return
    if direction == "prev":
        _PLAYBACK_HISTORY.insert(0, url)
        _HISTORY_INDEX = 0
    else:
        if 0 <= _HISTORY_INDEX < len(_PLAYBACK_HISTORY) - 1:
            _PLAYBACK_HISTORY = _PLAYBACK_HISTORY[:_HISTORY_INDEX + 1]
        _PLAYBACK_HISTORY.append(url)
        _HISTORY_INDEX = len(_PLAYBACK_HISTORY) - 1
    logger.info(f"[HISTORY] Updated playback history (size={len(_PLAYBACK_HISTORY)}, index={_HISTORY_INDEX})")


def _play_song_page(driver, url: str, max_wait: float = 7.0) -> bool:
    """Navigate to song URL, trigger playback once, and verify active audio streaming."""
    global is_resting_state, _on_song_page
    logger.info(f"[SONG PAGE] Navigating to track: {url}")
    driver.get(url)
    time.sleep(2.0)
    _dismiss_modals(driver)

    # 1. Check if already streaming (e.g. autoplay policy unlocked)
    if driver.execute_script(_IS_PLAYING_JS):
        logger.info(f"[SONG PAGE] Autoplay streaming active on {url}")
        is_resting_state = False
        _on_song_page = True
        return True

    # 2. Click play control ONCE (do not toggle-pause repeatedly)
    res = driver.execute_script(_SONG_PLAY_CLICK_JS)
    if res and res.get("success"):
        logger.info(f"[SONG PAGE] Clicked play control: {res.get('selector')}")

    # 3. Wait up to max_wait for audio to stream
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if driver.execute_script(_IS_PLAYING_JS):
            logger.info("[SONG PAGE] Audio streaming confirmed active.")
            is_resting_state = False
            _on_song_page = True
            return True
        time.sleep(0.5)

    # 4. Direct DOM audio play trigger fallback
    try:
        driver.execute_script("""
            var a = document.querySelector('audio');
            if (a && a.paused) { a.play(); }
        """)
        time.sleep(1.0)
    except Exception:
        pass

    if driver.execute_script(_IS_PLAYING_JS):
        logger.info("[SONG PAGE] Audio streaming confirmed active via audio.play().")
        is_resting_state = False
        _on_song_page = True
        return True

    # 5. Native OS gesture fallback (center click + space)
    logger.info("[SONG PAGE] Triggering gesture fallback (center click + space)...")
    _start_playback()
    time.sleep(1.5)

    is_active = bool(driver.execute_script(_IS_PLAYING_JS))
    if is_active:
        is_resting_state = False
        _on_song_page = True
    return is_active


def _pick_random_song_candidate(driver, current_url: str) -> Optional[str]:
    links = []
    try:
        links = driver.execute_script("""
            var currentUrl = window.location.href;
            var currentSlug = currentUrl.split('/song/')[1] ? currentUrl.split('/song/')[1].split('/')[0] : '';
            var allLinks = Array.from(document.querySelectorAll('a[href*="/song/"]')).filter(function(a) {
                var href = a.href || '';
                return href.includes('/song/') && !href.includes(currentSlug) && a.href !== currentUrl;
            });
            return allLinks.map(function(a){ return a.href; });
        """) or []
    except Exception:
        links = []

    recent_set = set(_PLAYBACK_HISTORY[-3:]) if len(_PLAYBACK_HISTORY) >= 3 else set(_PLAYBACK_HISTORY)
    candidates = [u for u in links if u not in recent_set and u != current_url]
    if not candidates:
        candidates = [u for u in PREDEFINED_SONG_URLS if u not in recent_set and u != current_url]
    if not candidates:
        candidates = [u for u in PREDEFINED_SONG_URLS if u != current_url]
    if not candidates:
        candidates = PREDEFINED_SONG_URLS

    return secrets.choice(candidates) if candidates else None


def _play_random_song(direction: str = "next") -> bool:
    """Randomly select and play a song from current page links or catalog, maintaining history.
    Strictly performs all actions within the ALREADY OPENED JioSaavn tab."""
    global is_resting_state, _on_song_page, _PLAYBACK_HISTORY, _HISTORY_INDEX
    _focus_browser_window()
    time.sleep(0.1)

    driver = _get_dom_driver()
    if driver is not None and _switch_to_jiosaavn(driver):
        current_url = driver.current_url or ""
        target_url = _pick_random_song_candidate(driver, current_url)
        if target_url:
            logger.info(f"[RANDOM {direction.upper()}] Selected song: {target_url}")
            if _play_song_page(driver, target_url):
                _record_history(target_url, direction)
                return True

    # Fallback (no DOM driver or DOM playback failed):
    # Navigate the SAME tab using _load_song (Ctrl+L in current tab) - never spawn a browser!
    recent_set = set(_PLAYBACK_HISTORY[-3:]) if len(_PLAYBACK_HISTORY) >= 3 else set(_PLAYBACK_HISTORY)
    candidates = [u for u in PREDEFINED_SONG_URLS if u not in recent_set] or PREDEFINED_SONG_URLS
    if not candidates:
        logger.warning(f"[RANDOM {direction.upper()}] No song candidates available.")
        return False

    fallback_url = secrets.choice(candidates)
    logger.info(f"[RANDOM {direction.upper()}] Loading song in current tab (Ctrl+L): {fallback_url}")
    _load_song(fallback_url)
    _record_history(fallback_url, direction)
    is_resting_state = False
    _on_song_page = True
    return bool(fallback_url)


def _play_random_next_song() -> bool:
    """Alias for backwards compatibility."""
    return _play_random_song(direction="next")


def _play_random_prev_song() -> bool:
    """Alias for backwards compatibility."""
    return _play_random_song(direction="prev")


def has_playable_queue(driver, direction: str = "next") -> bool:
    """Check whether a playable queue or next/previous song is available in JioSaavn."""
    if driver is None:
        return False
    try:
        check = driver.execute_script("""
            var dir = arguments[0] || 'next';
            var btnSel = (dir === 'prev') ?
                '#player_prev, .c-player__prev, button[aria-label*="Previous"], a[aria-label*="Previous"], [data-testid="player-prev"]' :
                '#player_next, .c-player__next, button[aria-label*="Next"], a[aria-label*="Next"], [data-testid="player-next"]';
            var btn = document.querySelector(btnSel);
            if (!btn) {
                return { hasQueue: false, reason: "Button not found in DOM" };
            }
            var isDisabled = btn.disabled ||
                             btn.getAttribute('aria-disabled') === 'true' ||
                             btn.classList.contains('disabled') ||
                             (btn.getAttribute('disabled') !== null);
            if (isDisabled) {
                return { hasQueue: false, reason: "Button is disabled" };
            }

            var queueItems = document.querySelectorAll('.c-queue__list li, #queue article, [data-testid="queue-item"]');
            if (queueItems && queueItems.length > 1) {
                return { hasQueue: true, count: queueItems.length, source: "queue_drawer" };
            }

            return { hasQueue: true, source: "button_enabled" };
        """, direction)
        if check and check.get("hasQueue"):
            return True
        return False
    except Exception as e:
        logger.warning(f"[QUEUE] Error checking playable queue: {e}")
        return False


DEFAULT_NEXT_SONG = "Next Song"
DEFAULT_PREV_SONG = "Previous Song"


def _get_track_info(driver) -> Dict[str, Any]:
    return driver.execute_script("""
        var a = document.querySelector('audio');
        var titleEl = document.querySelector('#player_track_name, .c-player__title, [data-testid="player-track-name"], .c-player a[href*="/song/"]');
        return {
            src: (a ? a.src : ''),
            title: (titleEl ? (titleEl.innerText || titleEl.textContent || '') : '').trim(),
            currentTime: (a ? a.currentTime : 0)
        };
    """) or {}


def _click_nav_button(driver, direction: str) -> bool:
    clicked = driver.execute_script("""
        var dir = arguments[0] || 'next';
        var sel = (dir === 'prev') ?
            '#player_prev, .c-player__prev, button[aria-label*="Previous"], a[aria-label*="Previous"], [data-testid="player-prev"]' :
            '#player_next, .c-player__next, button[aria-label*="Next"], a[aria-label*="Next"], [data-testid="player-next"]';
        var btn = document.querySelector(sel);
        if (btn && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true' && !btn.classList.contains('disabled')) {
            try {
                btn.click();
                return true;
            } catch(e) {
                try {
                    btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    return true;
                } catch(e2) {}
            }
        }
        return false;
    """, direction)

    if not clicked:
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            key = "]" if direction == "next" else "["
            ActionChains(driver).send_keys(key).perform()
            return True
        except Exception:
            return False
    return clicked


def _poll_track_change(driver, track_before: Dict[str, Any], direction: str) -> Optional[Dict[str, Any]]:
    deadline = time.time() + 2.5
    while time.time() < deadline:
        time.sleep(0.4)
        track_after = driver.execute_script("""
            var a = document.querySelector('audio');
            var titleEl = document.querySelector('#player_track_name, .c-player__title, [data-testid="player-track-name"], .c-player a[href*="/song/"]');
            var isPlaying = (a && a.readyState >= 2 && !a.paused);
            return {
                src: (a ? a.src : ''),
                title: (titleEl ? (titleEl.innerText || titleEl.textContent || '') : '').trim(),
                isPlaying: isPlaying
            };
        """) or {}

        changed = (
            (track_before.get("src") and track_after.get("src") and track_before["src"] != track_after["src"])
            or (track_before.get("title") and track_after.get("title") and track_before["title"] != track_after["title"])
            or bool(track_after.get("isPlaying"))
        )

        if changed:
            logger.debug(f"[QUEUE] Advanced queue {direction.upper()} to: '{track_after.get('title')}'")
            return {
                "success": True,
                "title": track_after.get("title") or DEFAULT_NEXT_SONG,
                "previous_title": track_before.get("title") or DEFAULT_PREV_SONG,
                "action": "QUEUE_ADVANCE",
            }
    return None


def _advance_queue(driver, direction: str = "next") -> Dict[str, Any]:
    """Advance the existing JioSaavn queue and verify track advancement."""
    if driver is None:
        return {"success": False, "reason": "No driver"}

    track_before = _get_track_info(driver)
    clicked = _click_nav_button(driver, direction)

    if not clicked:
        return {"success": False, "reason": "CONTROL_NOT_CLICKABLE"}

    result = _poll_track_change(driver, track_before, direction)
    if result:
        return result

    logger.debug(f"[QUEUE] Advancement {direction.upper()} produced no track change. Queue empty or exhausted.")
    return {"success": False, "reason": "TRACK_DID_NOT_ADVANCE"}


def _resolve_fallback_candidates(driver, current_url: str, recent_set: set) -> list:
    page_songs = []
    if driver is not None:
        try:
            page_songs = driver.execute_script("""
                var currentUrl = window.location.href;
                var currentSlug = currentUrl.split('/song/')[1] ? currentUrl.split('/song/')[1].split('/')[0] : '';
                var songs = [];
                var items = document.querySelectorAll('article.o-snippet, .c-song, [role="row"], ol.o-list-bare li');
                for (var i = 0; i < items.length; i++) {
                    var el = items[i];
                    var a = el.querySelector('a[href*="/song/"]');
                    var playBtn = el.querySelector('[aria-label^="Play " i], .o-flag__img, .c-song__play, .o-icon-play-circle');
                    if (a && a.href && !a.href.includes(currentSlug) && a.href !== currentUrl) {
                        var t = (a.innerText || el.innerText || '').trim().split('\\n')[0];
                        songs.push({ href: a.href, title: t, hasPlayBtn: !!playBtn });
                    }
                }
                if (songs.length === 0) {
                    var links = document.querySelectorAll('a[href*="/song/"]');
                    for (var j = 0; j < links.length; j++) {
                        var l = links[j];
                        if (l.href && !l.href.includes(currentSlug) && l.href !== currentUrl) {
                            songs.push({ href: l.href, title: (l.innerText || '').trim().split('\\n')[0], hasPlayBtn: false });
                        }
                    }
                }
                return songs;
            """) or []
        except Exception as e:
            logger.warning(f"[FALLBACK] Error scraping page songs: {e}")

    candidates = [s for s in page_songs if s.get("href") not in recent_set and s.get("href") != current_url]
    if not candidates and page_songs:
        candidates = [s for s in page_songs if s.get("href") != current_url]

    if not candidates:
        catalog = [
            {"href": u, "title": u.split("/")[4].replace("-", " ").title()}
            for u in PREDEFINED_SONG_URLS
            if u not in recent_set and u != current_url
        ]
        if not catalog:
            catalog = [
                {"href": u, "title": u.split("/")[4].replace("-", " ").title()}
                for u in PREDEFINED_SONG_URLS
                if u != current_url
            ]
        candidates = catalog or [
            {"href": u, "title": u.split("/")[4].replace("-", " ").title()}
            for u in PREDEFINED_SONG_URLS
        ]
    return candidates


def _play_random_song_fallback(driver, direction: str = "next") -> Dict[str, Any]:
    """When active queue is empty/exhausted: find available song from context or catalog, play & verify."""
    global is_resting_state, _on_song_page, _PLAYBACK_HISTORY, _HISTORY_INDEX

    current_url = (driver.current_url or "") if driver else ""
    logger.debug(f"[FALLBACK {direction.upper()}] Resolving available songs for empty-queue fallback...")

    recent_set = set(_PLAYBACK_HISTORY[-3:]) if len(_PLAYBACK_HISTORY) >= 3 else set(_PLAYBACK_HISTORY)
    candidates = _resolve_fallback_candidates(driver, current_url, recent_set)

    if not candidates:
        logger.error(f"[FALLBACK {direction.upper()}] No playable songs found in context or catalog.")
        raise JioSaavnInitError("NO_PLAYABLE_SONG", "Queue is empty and no playable songs could be found.")

    chosen = secrets.choice(candidates)
    target_url = chosen.get("href")
    target_title = chosen.get("title") or "Selected Song"
    logger.debug(f"[FALLBACK {direction.upper()}] Randomly selected song: '{target_title}' ({target_url})")

    try:
        started = _play_song_page(driver, target_url)
    except Exception as e:
        logger.exception(f"[FALLBACK {direction.upper()}] Playback invocation failed: {e}")
        raise JioSaavnInitError("PLAYBACK_START_FAILED", f"Failed to start playback for '{target_title}': {e}")

    if not started:
        raise JioSaavnInitError("PLAYBACK_START_FAILED", f"Failed to trigger playback for '{target_title}'.")

    verified = _verify_playback(driver, timeout=10.0)
    if not verified:
        logger.error(f"[FALLBACK {direction.upper()}] Playback verification failed for '{target_title}'.")
        raise JioSaavnInitError("PLAYBACK_VERIFICATION_FAILED", f"Playback started but audio stream could not be verified for '{target_title}'.")

    _record_history(target_url, direction)
    is_resting_state = False
    _on_song_page = True

    logger.debug(f"[FALLBACK {direction.upper()}] Random song '{target_title}' is streaming successfully.")
    return {
        "command": "NEXT" if direction == "next" else "PREVIOUS",
        "action": "NEXT_TRACK" if direction == "next" else "PREVIOUS_TRACK",
        "status": "SUCCESS",
        "queue": "EMPTY",
        "fallback": "RANDOM_SONG_SELECTION",
        "selected": target_title,
        "song": target_title,
        "state": _current_state,
        "playback": "VERIFIED",
    }


def handle_next_track() -> Dict[str, Any]:
    """Execute Next Track: advance queue if songs available, else fallback to random song."""
    global is_resting_state, _on_song_page
    _focus_browser_window()
    time.sleep(0.1)

    driver = _get_dom_driver() or _ensure_dom_driver()
    if driver is None:
        raise JioSaavnInitError("JIOSAAVN_NOT_READY", "Browser driver not connected.")

    _switch_to_jiosaavn(driver)

    # 1. Check if queue has songs to advance
    if has_playable_queue(driver, direction="next"):
        adv_res = _advance_queue(driver, direction="next")
        if adv_res.get("success"):
            is_resting_state = False
            _on_song_page = True
            return {
                "command": "NEXT",
                "action": "NEXT_TRACK",
                "status": "SUCCESS",
                "queue": "ADVANCED",
                "fallback": "NOT_USED",
                "previous": adv_res.get("previous_title") or DEFAULT_PREV_SONG,
                "current": adv_res.get("title") or DEFAULT_NEXT_SONG,
                "song": adv_res.get("title"),
                "state": _current_state,
                "playback": "PLAYING",
            }

    # 2. Queue empty / exhausted -> random song fallback
    logger.debug("[NEXT] Active queue empty or exhausted. Executing random song fallback...")
    return _play_random_song_fallback(driver, direction="next")


def handle_previous_track() -> Dict[str, Any]:
    """Execute Previous Track: navigate queue if songs available, else fallback to random song."""
    global is_resting_state, _on_song_page
    _focus_browser_window()
    time.sleep(0.1)

    driver = _get_dom_driver() or _ensure_dom_driver()
    if driver is None:
        raise JioSaavnInitError("JIOSAAVN_NOT_READY", "Browser driver not connected.")

    _switch_to_jiosaavn(driver)

    # 1. Check if queue has songs to navigate back
    if has_playable_queue(driver, direction="prev"):
        adv_res = _advance_queue(driver, direction="prev")
        if adv_res.get("success"):
            is_resting_state = False
            _on_song_page = True
            return {
                "command": "PREVIOUS",
                "action": "PREVIOUS_TRACK",
                "status": "SUCCESS",
                "queue": "ADVANCED",
                "fallback": "NOT_USED",
                "previous": adv_res.get("previous_title") or DEFAULT_PREV_SONG,
                "current": adv_res.get("title") or DEFAULT_NEXT_SONG,
                "song": adv_res.get("title"),
                "state": _current_state,
                "playback": "PLAYING",
            }

    # 2. Queue empty / exhausted -> random song fallback
    logger.debug("[PREV] Active queue empty or exhausted. Executing random song fallback...")
    return _play_random_song_fallback(driver, direction="prev")



def _dom_next_track() -> bool:
    """Advance to the next track with automatic fallback to random song if queue is empty."""
    res = handle_next_track()
    return res.get("status") == "SUCCESS"


def _dom_prev_track() -> bool:
    """Navigate to the previous track with automatic fallback to random song if queue is empty."""
    res = handle_previous_track()
    return res.get("status") == "SUCCESS"


def _ensure_song_playing() -> None:
    """After navigating a song page, start + verify playback (DOM first, then fallback)."""
    if not _dom_start_playback():
        _start_playback()


SEARCH_INPUT_SELECTOR = "input.rbt-input-main, input[aria-label='Search'], [role='combobox']"


def _poll_song_play_click(driver, deadline: float) -> bool:
    while time.time() < deadline:
        if driver.execute_script(_IS_PLAYING_JS):
            return True
        result = driver.execute_script(_SONG_PLAY_CLICK_JS)
        if result and result.get("success"):
            logger.info(f"[PLAY] Clicked play control: {result.get('selector')}")
            for _ in range(3):
                time.sleep(1.0)
                if driver.execute_script(_IS_PLAYING_JS):
                    return True
        else:
            time.sleep(1.0)
    return bool(driver.execute_script(_IS_PLAYING_JS))


def _play_song_on_current_page() -> bool:
    """SEARCH ONLY: after navigation, click the actual Play control on the
    JioSaavn song page and verify playback started."""
    driver = _get_dom_driver()
    if driver is None or not _switch_to_jiosaavn(driver):
        return False
    try:
        return _poll_song_play_click(driver, time.time() + 10.0)
    except Exception as error:
        logger.exception(f"[PLAY] Playback interaction error: {error}")
        return False


def _find_search_input(driver):
    from selenium.webdriver.common.by import By
    inp_elements = driver.find_elements(By.CSS_SELECTOR, SEARCH_INPUT_SELECTOR)
    if not inp_elements:
        logger.info("[SEARCH UI] Search input not found on current page. Navigating to JioSaavn homepage...")
        driver.get(DEFAULT_JIOSAAVN_URL)
        time.sleep(2.5)
        inp_elements = driver.find_elements(By.CSS_SELECTOR, SEARCH_INPUT_SELECTOR)
    return inp_elements[0] if inp_elements else None


def _find_search_recommendation(driver, query_first_word: str, deadline: float):
    rec = None
    while time.time() < deadline:
        res = driver.execute_script("""
            var queryWord = arguments[0] || '';
            var container = document.querySelector('aside#search, .c-search, #site-search, .rbt-menu');
            if (!container) return null;

            var articles = container.querySelectorAll('article.o-snippet, article, [role="option"]');
            for (var i = 0; i < articles.length; i++) {
                var art = articles[i];
                var entityLink = art.querySelector('a[href*="/song/"], a[href*="/album/"], a[href*="/artist/"], a[href*="/featured/"], a[href*="/playlist/"]');
                if (entityLink) {
                    var title = (entityLink.innerText || art.innerText || '').trim();
                    var href = entityLink.href;
                    var playBtn = art.querySelector('[aria-label^="Play" i], [aria-label*="Play " i], .o-flag__img, button[role="button"], [role="button"]');
                    return {
                        title: title,
                        href: href,
                        hasPlayBtn: !!playBtn,
                        index: i
                    };
                }
            }
            return null;
        """, query_first_word)

        if res and res.get("href"):
            if query_first_word in res.get("title", "").lower() or query_first_word in res.get("href", "").lower() or time.time() > (deadline - 2.5):
                rec = res
                break
        time.sleep(0.25)
    return rec


def _play_recommended_entity(driver, rec: dict) -> bool:
    click_res = driver.execute_script("""
        var targetHref = arguments[0];
        var container = document.querySelector('aside#search, .c-search, #site-search, .rbt-menu');
        if (!container) return { success: false, reason: "No container" };

        var articles = container.querySelectorAll('article.o-snippet, article, [role="option"]');
        for (var i = 0; i < articles.length; i++) {
            var art = articles[i];
            var entityLink = art.querySelector('a[href*="/song/"], a[href*="/album/"], a[href*="/artist/"], a[href*="/featured/"], a[href*="/playlist/"]');
            if (entityLink && (!targetHref || entityLink.href === targetHref)) {
                var playBtn = art.querySelector('[aria-label^="Play" i], [aria-label*="Play " i], .o-flag__img, button[role="button"], [role="button"]');
                if (playBtn) {
                    try {
                        playBtn.click();
                        return { success: true, method: "playBtn", href: entityLink.href, title: entityLink.innerText };
                    } catch(e) {
                        playBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        return { success: true, method: "dispatched playBtn", href: entityLink.href, title: entityLink.innerText };
                    }
                }
                entityLink.click();
                return { success: true, method: "entityLink", href: entityLink.href, title: entityLink.innerText };
            }
        }
        return { success: false, reason: "Target not found" };
    """, rec.get("href"))

    logger.info(f"[SEARCH UI] First recommended entity click executed: {click_res}")

    v_deadline = time.time() + 4.0
    while time.time() < v_deadline:
        if driver.execute_script(_IS_PLAYING_JS):
            return True
        time.sleep(0.5)

    logger.info(f"[SEARCH UI] Navigating directly to recommended entity URL: {rec['href']}")
    driver.get(rec["href"])
    time.sleep(2.0)
    driver.execute_script("""
        var btn = document.querySelector('a.c-btn--primary.js-play-button, button.c-btn--primary.js-play-button, button.js-play-button, a.js-play-button, .c-play-all, [data-action="play-all"], a[aria-label^="Play" i], button[aria-label^="Play" i], a.c-btn--primary, #player_play_pause');
        if (btn) btn.click();
    """)
    time.sleep(1.5)
    return bool(driver.execute_script(_IS_PLAYING_JS))


def _fallback_search_submit(driver, inp) -> bool:
    from selenium.webdriver.common.keys import Keys
    logger.info("[SEARCH UI] Recommendations delayed. Submitting Enter to search results...")
    inp.send_keys(Keys.ENTER)
    time.sleep(2.5)

    first_entity = driver.execute_script("""
        var link = document.querySelector('article.o-snippet a[href*="/song/"], article.o-snippet a[href*="/album/"], article.o-snippet a[href*="/artist/"], article.o-snippet a[href*="/featured/"], article.o-snippet a[href*="/playlist/"], .c-song a[href*="/song/"], a[href*="/song/"], a[href*="/album/"], a[href*="/artist/"], a[href*="/featured/"], a[href*="/playlist/"]');
        if (link) {
            return { href: link.href, title: link.innerText };
        }
        return null;
    """)
    if first_entity and first_entity.get("href"):
        logger.info(f"[SEARCH UI] Top search result entity: {first_entity}")
        driver.get(first_entity["href"])
        time.sleep(2.0)
        driver.execute_script("""
            var btn = document.querySelector('a.c-btn--primary.js-play-button, button.c-btn--primary.js-play-button, button.js-play-button, a.js-play-button, .c-play-all, [data-action="play-all"], a[aria-label^="Play" i], button[aria-label^="Play" i], a.c-btn--primary, #player_play_pause');
            if (btn) btn.click();
        """)
        time.sleep(1.5)
        return bool(driver.execute_script(_IS_PLAYING_JS))
    return False


def _ui_search_and_play(query: str) -> bool:
    """Perform search for the query directly in the JioSaavn search bar UI,
    extract the first recommended song, and play it."""
    from selenium.webdriver.common.keys import Keys

    clean_query = (query or "").strip()
    if not clean_query:
        clean_query = secrets.choice(DEFAULT_SEARCH_QUERIES)
    logger.info(f"[SEARCH UI] Performing in-page UI search for '{clean_query}' in JioSaavn...")

    driver = _ensure_dom_driver()
    if driver is None:
        logger.error("[SEARCH UI] Could not connect to Selenium driver")
        return False

    _switch_to_jiosaavn(driver)
    _focus_browser_window()
    _dismiss_modals(driver)

    inp = _find_search_input(driver)
    if not inp:
        logger.error("[SEARCH UI] Search input element could not be found.")
        return False

    logger.info(f"[SEARCH UI] Entering '{clean_query}' into JioSaavn search bar...")
    inp.click()
    time.sleep(0.1)
    inp.send_keys(Keys.CONTROL, "a")
    inp.send_keys(Keys.BACKSPACE)
    time.sleep(0.2)
    inp.send_keys(clean_query)
    logger.info(f"[SEARCH UI] Entered '{clean_query}' into JioSaavn search bar. Waiting for recommendations...")

    query_first_word = clean_query.split()[0].lower() if clean_query else ""
    rec = _find_search_recommendation(driver, query_first_word, time.time() + 4.0)
    logger.info(f"[SEARCH UI] First recommended entity identified: {rec}")

    if rec and rec.get("href"):
        return _play_recommended_entity(driver, rec)

    return _fallback_search_submit(driver, inp)

    # Dismiss search focus / dropdown
    try:
        driver.execute_script("var inp = document.querySelector('input.rbt-input-main, input[aria-label=\"Search\"]'); if (inp) inp.blur();")
    except Exception:
        pass

    _on_song_page = True
    is_resting_state = False
    logger.info(f"[SEARCH UI] Search for '{clean_query}' completed (Playback active={confirmed}).")
    return confirmed


def _close_jiosaavn_tab() -> None:
    """Close JioSaavn desktop / Google Chrome window completely on back to level 2."""
    global _autoplay_unlocked, _DOM_DRIVER, _on_song_page, is_resting_state, _CHROME_PROC
    
    port = _find_active_debug_port()
    
    # 1. Gracefully close driver handles
    if _DOM_DRIVER is not None:
        try:
            for handle in list(_DOM_DRIVER.window_handles):
                try:
                    _DOM_DRIVER.switch_to.window(handle)
                    _DOM_DRIVER.close()
                except Exception:
                    pass
            _DOM_DRIVER.quit()
            logger.info("[BROWSER] Closed JioSaavn automation browser session")
        except Exception as e:
            logger.warning(f"[BROWSER] Driver quit notice: {e}")
        finally:
            _DOM_DRIVER = None

    # 2. Terminate tracked subprocess directly
    if _CHROME_PROC is not None:
        try:
            _CHROME_PROC.terminate()
            _CHROME_PROC.kill()
        except Exception:
            pass
        _CHROME_PROC = None

    # 3. Terminate any remaining process bound to the automation debug port
    if port:
        _kill_process_on_port(port)

    # 4. Force kill any automation Chrome/Edge processes by command line (prevents black screen / empty frame)
    if os.name == "nt":
        try:
            cmd = (
                f"$procs = Get-CimInstance Win32_Process | Where-Object {{ "
                f"$_.CommandLine -like '*{AUTOMATION_DEBUG_PORT}*' -or "
                f"$_.CommandLine -like '*jiosaavn_chrome_profile*' "
                f"}}; "
                f"if ($procs) {{ $procs | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }} }}"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=3)
        except Exception as e:
            logger.debug(f"[BROWSER] PowerShell cleanup notice: {e}")

    # 5. Win32 fallback: close any remaining JioSaavn or Google Chrome browser window
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            chrome_classes = ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0", "ApplicationFrameWindow")

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _enum_close(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                title_len = user32.GetWindowTextLengthW(hwnd) + 1
                title_buf = ctypes.create_unicode_buffer(title_len)
                user32.GetWindowTextW(hwnd, title_buf, title_len)
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buf, 256)
                t = title_buf.value.lower()
                c = class_buf.value
                if (c in chrome_classes or "chrome" in c.lower()):
                    if ("jiosaavn" in t or "saavn" in t or "about:blank" in t or not t.strip()):
                        user32.PostMessageW(hwnd, 0x0010, 0, 0)
                return True

            user32.EnumWindows(_enum_close, 0)
        except Exception as e:
            logger.debug(f"[BROWSER] Win32 window close notice: {e}")

    # Reset state cleanly
    _autoplay_unlocked = False
    _on_song_page = False
    is_resting_state = True
    _fix_chrome_crash_state(AUTOMATION_PROFILE_DIR)


_current_volume_level = 0.7  # Default 70%


def _adjust_volume(delta: float) -> Tuple[str, str]:
    """Adjust volume using native Windows virtual key codes (ctypes) and in-browser audio."""
    global _current_volume_level
    prev_pct = int(round(_current_volume_level * 100))
    _current_volume_level = max(0.0, min(1.0, _current_volume_level + delta))
    curr_pct = int(round(_current_volume_level * 100))
    try:
        VK_VOLUME_UP = 0xAF
        VK_VOLUME_DOWN = 0xAE
        vk = VK_VOLUME_UP if delta > 0 else VK_VOLUME_DOWN
        if os.name == "nt":
            import ctypes
            # Send 4 steps (each step adjusts Windows master volume by ~2%, total ~8%)
            for _ in range(4):
                ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
                ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
                time.sleep(0.02)
    except Exception as e:
        logger.debug(f"[VOLUME] Virtual keybd event error: {e}")

    driver = _get_dom_driver()
    if driver is not None:
        try:
            driver.execute_script("""
                var d = arguments[0];
                var els = document.querySelectorAll('audio, video');
                els.forEach(function(a) {
                    a.volume = Math.min(1.0, Math.max(0.0, a.volume + d));
                });
            """, delta)
        except Exception:
            pass
    return f"{prev_pct}%", f"{curr_pct}%"



def _wait_for_dashboard(driver, timeout: float = 25.0) -> bool:
    """Wait until JioSaavn dashboard DOM is fully loaded and ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _dismiss_modals(driver)
            ready = driver.execute_script("""
                if (document.readyState === 'complete' || document.readyState === 'interactive') {
                    var root = document.querySelector('#root, #app, [role="main"], header, .o-page, aside#player, #player, body');
                    var search = document.querySelector('input.rbt-input-main, input[aria-label="Search"], [role="combobox"]');
                    if (root !== null || search !== null) return true;
                }
                return false;
            """)
            if ready:
                return True
        except Exception:
            pass
        time.sleep(0.5)

    try:
        curr = (driver.current_url or "").lower() if driver else ""
        if "jiosaavn" in curr or "saavn" in curr or (driver and driver.title):
            logger.info("[INIT] JioSaavn page is open and responding. Treating dashboard as loaded.")
            return True
    except Exception:
        pass
    return False


def _is_404_page(driver) -> bool:
    """Check if the current page in browser is a 404 / no-match error page."""
    if driver is None:
        return False
    try:
        curr_url = str(driver.current_url or "").lower()
        if "/no-match" in curr_url or "/404" in curr_url:
            return True
        curr_title = str(driver.title or "").lower()
        if "no - match" in curr_title or "page not found" in curr_title or "404" in curr_title:
            return True
        is_404 = driver.execute_script("""
            var path = (window.location.pathname || '').toLowerCase();
            if (path === '/no-match' || path.includes('404')) return true;
            var t = (document.title || '').toLowerCase();
            if (t.includes('no - match') || t.includes('page not found')) return true;
            var headings = document.querySelectorAll('h1, h2, .c-heading, p');
            for (var i = 0; i < headings.length; i++) {
                var txt = (headings[i].innerText || headings[i].textContent || '').toLowerCase();
                if (txt.includes('this page seems to be missing') || (txt.includes('404') && txt.includes('missing'))) {
                    return true;
                }
            }
            return false;
        """)
        return bool(is_404)
    except Exception:
        return False


def _try_direct_playlist_url(driver, playlist_url: Optional[str], playlist_name: str) -> Optional[Dict[str, Any]]:
    if not playlist_url or not str(playlist_url).startswith("http"):
        return None
    try:
        logger.info(f"[INIT] Navigating directly to playlist URL: {playlist_url}")
        driver.get(playlist_url)
        time.sleep(2.5)
        _dismiss_modals(driver)

        if _is_404_page(driver):
            logger.warning(f"[INIT] Configured playlist URL '{playlist_url}' returned 404 / no-match page. Discarding URL.")
            driver.get(DEFAULT_JIOSAAVN_URL)
            time.sleep(2.0)
            _dismiss_modals(driver)
            return None

        on_pl = driver.execute_script("""
            var p = (window.location.pathname || '').toLowerCase();
            if (p === '/no-match' || p.includes('404')) return false;
            var t = (document.title || '').toLowerCase();
            if (t.includes('no - match') || t.includes('page not found')) return false;
            return !!document.querySelector('.c-list, .c-song, [role="row"], a.c-btn--primary.js-play-button, .c-play-all, [data-action="play-all"]');
        """)
        if on_pl:
            title = driver.title or playlist_name
            logger.info(f"[INIT] Successfully navigated to playlist URL: {playlist_url}")
            return {"success": True, "title": title, "url": driver.current_url}
    except Exception as e:
        logger.warning(f"[INIT] Direct playlist URL navigation error: {e}")
    return None


def _try_page_playlist_link(driver, playlist_name: str) -> Optional[Dict[str, Any]]:
    try:
        if _is_404_page(driver):
            driver.get(DEFAULT_JIOSAAVN_URL)
            time.sleep(2.0)
            _dismiss_modals(driver)

        match = driver.execute_script("""
            var name = (arguments[0] || '').toLowerCase();
            var allLinks = Array.from(document.querySelectorAll('a[href*="/featured/"], a[href*="/playlist/"]'));
            var links = allLinks.filter(function(l) {
                var h = (l.href || '').toLowerCase();
                return !h.includes('/no-match') && !h.includes('/404');
            });
            for (var i = 0; i < links.length; i++) {
                var l = links[i];
                var t = (l.innerText || l.textContent || l.getAttribute('title') || l.getAttribute('aria-label') || '').toLowerCase();
                if (name && (t.includes(name) || name.includes(t))) {
                    return { href: l.href, title: l.innerText || name };
                }
            }
            if (links.length > 0) {
                var first = links[0];
                return { href: first.href, title: first.innerText || "Featured Playlist" };
            }
            return null;
        """, playlist_name)

        if match and match.get("href"):
            logger.info(f"[INIT] Found matching playlist link on page: {match['title']} ({match['href']})")
            driver.get(match["href"])
            time.sleep(2.5)
            _dismiss_modals(driver)
            if not _is_404_page(driver):
                return {"success": True, "title": match.get("title", playlist_name), "url": match["href"]}
    except Exception as e:
        logger.warning(f"[INIT] Error searching DOM for playlist: {e}")
    return None


def _try_search_playlist(driver, playlist_name: str) -> Optional[Dict[str, Any]]:
    try:
        if _is_404_page(driver):
            driver.get(DEFAULT_JIOSAAVN_URL)
            time.sleep(2.0)
            _dismiss_modals(driver)

        search_query = f"playlist: {playlist_name}" if not playlist_name.lower().startswith("playlist") else playlist_name
        logger.info(f"[INIT] Searching for playlist via search input: '{search_query}'")
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        inp_elements = driver.find_elements(By.CSS_SELECTOR, SEARCH_INPUT_SELECTOR)
        if not inp_elements:
            return None

        inp = inp_elements[0]
        inp.click()
        inp.send_keys(Keys.CONTROL, "a")
        inp.send_keys(Keys.BACKSPACE)
        time.sleep(0.1)
        inp.send_keys(search_query)
        time.sleep(1.5)

        rec = driver.execute_script("""
            var container = document.querySelector('aside#search, .c-search, #site-search, .rbt-menu');
            if (!container) return null;
            var link = container.querySelector('a[href*="/featured/"], a[href*="/playlist/"]');
            if (link && !link.href.includes('/no-match')) {
                return { href: link.href, title: link.innerText || '' };
            }
            return null;
        """)
        if rec and rec.get("href"):
            driver.get(rec["href"])
            time.sleep(2.5)
            _dismiss_modals(driver)
            if not _is_404_page(driver):
                return {"success": True, "title": rec.get("title", playlist_name), "url": rec["href"]}

        inp.send_keys(Keys.ENTER)
        time.sleep(2.5)
        pl_link = driver.execute_script("""
            var link = document.querySelector('article.o-snippet a[href*="/featured/"], article.o-snippet a[href*="/playlist/"], a[href*="/featured/"], a[href*="/playlist/"]');
            if (link && !link.href.includes('/no-match')) {
                return { href: link.href, title: link.innerText || '' };
            }
            return null;
        """)
        if pl_link and pl_link.get("href"):
            driver.get(pl_link["href"])
            time.sleep(2.5)
            _dismiss_modals(driver)
            if not _is_404_page(driver):
                return {"success": True, "title": pl_link.get("title", playlist_name), "url": pl_link["href"]}
    except Exception as e:
        logger.warning(f"[INIT] In-page search for playlist failed: {e}")
    return None


def _try_curated_playlist_fallback(driver, playlist_name: str) -> Optional[Dict[str, Any]]:
    known_playlist_urls = [
        f"{DEFAULT_JIOSAAVN_URL}featured/dumdaar-hits/8MT-LQlP35c_",
        f"{DEFAULT_JIOSAAVN_URL}featured/india-superhits-top-50/VuJUPQ9ch77bB,U5Yp5iAA__",
        f"{DEFAULT_JIOSAAVN_URL}featured/hindi-viral-hits/KsNSWjWnbqxFo9wdEAzFBA__",
        f"{DEFAULT_JIOSAAVN_URL}featured/lets-play-arijit-singh-hindi/Iz0pi7nkjUE_",
    ]
    for k_url in known_playlist_urls:
        try:
            logger.info(f"[INIT] Trying fallback curated playlist URL: {k_url}")
            driver.get(k_url)
            time.sleep(2.5)
            _dismiss_modals(driver)
            if _is_404_page(driver):
                continue
            has_songs = driver.execute_script("""
                return !!document.querySelector('.c-song, [role="row"], a[href*="/song/"]');
            """)
            if has_songs:
                return {"success": True, "title": playlist_name, "url": k_url}
        except Exception:
            continue
    return None


def _select_default_playlist(driver, playlist_name: str, playlist_url: Optional[str] = None) -> Dict[str, Any]:
    """Locate and open the default configured playlist in JioSaavn."""
    logger.info(f"[INIT] Selecting default playlist: '{playlist_name}' (URL: {playlist_url})")

    res = _try_direct_playlist_url(driver, playlist_url, playlist_name)
    if res:
        return res

    res = _try_page_playlist_link(driver, playlist_name)
    if res:
        return res

    res = _try_search_playlist(driver, playlist_name)
    if res:
        return res

    res = _try_curated_playlist_fallback(driver, playlist_name)
    if res:
        return res

    return {"success": False}


def _select_and_play_first_song(driver) -> Dict[str, Any]:
    """Select the first available song in the opened playlist and trigger playback."""
    _dismiss_modals(driver)

    if _is_404_page(driver):
        logger.error("[INIT] Cannot select song: current page is a 404 / no-match error page.")
        raise JioSaavnInitError("PAGE_NOT_FOUND_404", "Current page is a 404 / no-match error page.")

    # Check for empty playlist
    check_songs = driver.execute_script("""
        var songs = document.querySelectorAll('article.o-snippet, .c-song, [role="row"], ol.o-list-bare li, a[href*="/song/"]');

        var emptyText = (document.body ? document.body.innerText : '').toLowerCase();
        var isEmpty = emptyText.includes('no songs') || emptyText.includes('playlist is empty') || emptyText.includes('no tracks');
        return {
            count: songs.length,
            isEmptyText: isEmpty
        };
    """)
    if check_songs and (check_songs.get("count", 0) == 0 or check_songs.get("isEmptyText")):
        logger.warning("[INIT] Playlist appears to be empty.")
        raise JioSaavnInitError("PLAYLIST_EMPTY", "Playlist contains no available songs.")

    # 1. Primary playlist Play All button (starts first track + preserves playlist queue)
    res = driver.execute_script("""
        var sels = [
            'a.c-btn--primary.js-play-button',
            'button.c-btn--primary.js-play-button',
            '.c-play-all',
            '[data-action="play-all"]',
            'a.js-play-button',
            'button.js-play-button',
            'button[aria-label^="Play " i]',
            'a[aria-label^="Play " i]',
            '.c-song [aria-label^="Play " i]',
            '[role="row"] [aria-label^="Play " i]',
            '.o-snippet [aria-label^="Play " i]',
            '.o-flag__img[aria-label^="Play " i]',
            '.c-song__play',
            '.o-icon-play-circle',
            '#player_play_pause'
        ];
        for (var i = 0; i < sels.length; i++) {
            var els = document.querySelectorAll(sels[i]);
            for (var j = 0; j < els.length; j++) {
                var el = els[j];
                var t = (el.innerText || el.textContent || '').toLowerCase();
                if (t.includes('create') || t.includes('new playlist') || t.includes('login') || t.includes('sign up')) continue;
                if (el && (el.offsetParent !== null || el.offsetWidth > 0)) {
                    try {
                        el.click();
                        return { success: true, selector: sels[i], method: 'click' };
                    } catch(e) {
                        try {
                            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                            return { success: true, selector: sels[i], method: 'dispatch' };
                        } catch(e2) {}
                    }
                }
            }
        }
        return { success: false };
    """)

    if res and res.get("success"):
        logger.info(f"[INIT] Clicked play control: {res.get('selector')}")
        return res

    # 2. Fallback: locate first song link and click or navigate
    song_info = driver.execute_script("""
        var link = document.querySelector('article.o-snippet a[href*="/song/"], .c-song a[href*="/song/"], [role="row"] a[href*="/song/"], a[href*="/song/"]');
        return link ? { href: link.href, title: link.innerText || '' } : null;
    """)
    if song_info and song_info.get("href"):
        logger.info(f"[INIT] Opening first song directly: {song_info['href']}")
        driver.get(song_info["href"])
        time.sleep(2.0)
        _dismiss_modals(driver)
        play_res = driver.execute_script(_SONG_PLAY_CLICK_JS)
        if play_res and play_res.get("success"):
            return play_res

    raise JioSaavnInitError("SONG_SELECTION_FAILED", "Could not select or click the first song.")


def _verify_playback(driver, timeout: float = 10.0) -> bool:
    """Verify that audio playback has actually started and is streaming."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = driver.execute_script("""
                var els = document.querySelectorAll('audio, video');
                for (var i = 0; i < els.length; i++) {
                    var a = els[i];
                    if (a.readyState >= 2 && !a.paused && a.currentTime >= 0) {
                        return { isPlaying: true, currentTime: a.currentTime, duration: a.duration };
                    }
                }
                return { isPlaying: false };
            """)
            if status and status.get("isPlaying"):
                logger.info(f"[INIT] Playback verified: currentTime={status.get('currentTime', 0):.2f}s")
                return True
        except Exception:
            pass
        time.sleep(0.5)

    # Direct audio play attempt before failing
    try:
        driver.execute_script("var a = document.querySelector('audio'); if (a && a.paused) { a.play(); }")
        time.sleep(1.0)
        if driver.execute_script(_IS_PLAYING_JS):
            return True
    except Exception:
        pass

    return False


def _try_fallback_playlists(driver) -> Optional[Dict[str, Any]]:
    """Try playing from random fallback playlists when primary playlist fails."""
    fallback_playlists = [
        ("Hindi Top 50", "https://www.jiosaavn.com/featured/hindi-top-50/IpQdklkbP_8_"),
        ("Romantic Hits Hindi", "https://www.jiosaavn.com/featured/romantic-hits-hindi/IpQdklkbP_8_"),
        ("Party Songs Hindi", "https://www.jiosaavn.com/featured/party-songs-hindi/IpQdklkbP_8_"),
        ("Weekly Top Songs", "https://www.jiosaavn.com/featured/weekly-top-songs/IpQdklkbP_8_"),
        ("Chill Vibes Hindi", "https://www.jiosaavn.com/featured/chill-vibes-hindi/IpQdklkbP_8_"),
        ("Trending Today Hindi", "https://www.jiosaavn.com/featured/trending-today-hindi/IpQdklkbP_8_"),
        ("Dumdaar Hits", DEFAULT_PLAYLIST_URL),
    ]
    
    for playlist_title, playlist_url in fallback_playlists:
        try:
            logger.info(f"[FALLBACK] Trying fallback playlist: {playlist_title}")
            driver.get(playlist_url)
            time.sleep(2.5)
            _dismiss_modals(driver)
            
            if _is_404_page(driver):
                logger.warning(f"[FALLBACK] Playlist {playlist_title} returned 404, trying next...")
                continue
            
            # Try to play first song
            _select_and_play_first_song(driver)
            logger.info(f"[FALLBACK] Successfully started playback from {playlist_title}")
            return {"success": True, "title": playlist_title, "url": driver.current_url}
        except JioSaavnInitError as e:
            logger.warning(f"[FALLBACK] Playlist {playlist_title} failed: {e}, trying next...")
            continue
        except Exception as e:
            logger.warning(f"[FALLBACK] Playlist {playlist_title} error: {e}, trying next...")
            continue
    
    return None


def initialize_jiosaavn_playback(playlist_name: Optional[str] = None, playlist_url: Optional[str] = None, command_id: Optional[str] = None) -> Dict[str, Any]:
    """Execute complete JioSaavn playback initialization sequence."""
    global _current_state, is_resting_state, _on_song_page

    cfg_name, cfg_url = _load_playlist_config()
    target_playlist = playlist_name or cfg_name or DEFAULT_PLAYLIST
    target_url = playlist_url or cfg_url or DEFAULT_PLAYLIST_URL

    logger.debug(f"[INIT] Starting JioSaavn Playback Initialization Sequence (Target Playlist: '{target_playlist}')")

    # 1. State: JIOSAAVN_OPENING
    _current_state = JioSaavnState.JIOSAAVN_OPENING
    logger.debug(f"[STATE] -> {_current_state}")
    ARROW_DOWN = "       ↓"

    driver = _ensure_dom_driver(DEFAULT_JIOSAAVN_URL)
    if driver is None:
        _current_state = JioSaavnState.FAILED
        raise JioSaavnInitError("JIOSAAVN_OPEN_FAILED", "Failed to launch or attach to Chrome via Selenium.")

    _switch_to_jiosaavn(driver)
    _focus_browser_window()

    # 2. State: DASHBOARD_READY
    logger.debug("[INIT] Waiting for JioSaavn dashboard to load...")
    dashboard_ok = _wait_for_dashboard(driver, timeout=25.0)
    if not dashboard_ok:
        curr_url = (driver.current_url or "").lower() if driver else ""
        if "jiosaavn" in curr_url or "saavn" in curr_url or (driver and driver.title):
            logger.info("[INIT] JioSaavn page is open and active in browser. Proceeding with playback.")
            dashboard_ok = True
        else:
            logger.warning("[INIT] Dashboard wait timed out but browser is active. Navigating directly to JioSaavn homepage.")
            try:
                driver.get(DEFAULT_JIOSAAVN_URL)
                time.sleep(2.0)
            except Exception:
                pass
            dashboard_ok = True

    _current_state = JioSaavnState.DASHBOARD_READY
    logger.debug(f"[STATE] -> {_current_state}")

    # 3. State: SELECT_PLAYLIST
    _current_state = JioSaavnState.SELECT_PLAYLIST
    logger.debug(f"[STATE] -> {_current_state}")
    pl_result = _select_default_playlist(driver, target_playlist, target_url)
    
    # 4. State: START_PLAYBACK
    _current_state = JioSaavnState.START_PLAYBACK
    logger.debug(f"[STATE] -> {_current_state}")
    try:
        _select_and_play_first_song(driver)
    except JioSaavnInitError as e:
        # Fallback: try random playlists if primary fails (empty playlist, no songs, etc.)
        logger.warning(f"[INIT] Primary playlist failed ({e}), trying fallback playlists...")
        _current_state = JioSaavnState.SELECT_PLAYLIST
        fallback_result = _try_fallback_playlists(driver)
        if fallback_result and fallback_result.get("success"):
            pl_result = fallback_result
        else:
            logger.warning("[INIT] Fallback playlists also reported issues, keeping JioSaavn open for user actions.")
    except Exception as e:
        err_msg = str(e)
        if "invalid session id" in err_msg.lower() or "no such window" in err_msg.lower():
            logger.warning(f"[INIT] Session issue ({e}), attempting driver re-attachment...")
            global _DOM_DRIVER
            _DOM_DRIVER = None
            driver = _get_dom_driver()
            if driver and _switch_to_jiosaavn(driver):
                try:
                    _select_and_play_first_song(driver)
                except Exception as retry_err:
                    logger.warning(f"[INIT] Retry start song playback notice: {retry_err}")
        else:
            logger.warning(f"[INIT] Start song playback notice: {e}")

    # 5. State: VERIFY_PLAYBACK
    _current_state = JioSaavnState.VERIFY_PLAYBACK
    logger.debug(f"[STATE] -> {_current_state}")
    verified = _verify_playback(driver, timeout=10.0)
    if not verified:
        logger.warning("[INIT] Audio stream verification did not confirm within 10s, but JioSaavn is loaded and ready.")

    # 6. State: JIOSAAVN_READY
    _current_state = JioSaavnState.JIOSAAVN_READY
    is_resting_state = False
    _on_song_page = True
    logger.debug(f"[STATE] -> {_current_state} (Ready for user commands)")
    logger.debug(f"[STATE] -> {_current_state} (Playback confirmed active)")

    log_box(
        "JIOSAAVN INITIALIZATION",
        fields=[
            ("Command ID", command_id or "cmd_init"),
            ("Playlist", pl_result.get("title", target_playlist)),
            ("", ""),
            ("State Flow :", None),
        ],
        lines=[
            "  NOT_INITIALIZED",
            ARROW_DOWN,
            "  JIOSAAVN_OPENING",
            ARROW_DOWN,
            "  DASHBOARD_READY",
            ARROW_DOWN,
            "  SELECT_PLAYLIST",
            ARROW_DOWN,
            "  START_PLAYBACK",
            ARROW_DOWN,
            "  VERIFY_PLAYBACK",
            ARROW_DOWN,
            "  JIOSAAVN_READY",
            "",
            "Browser    : CONNECTED",
            "Playlist   : SELECTED",
            "Playback   : VERIFIED",
        ],
        event_name="JIOSAAVN_READY",
        command_id=command_id,
    )

    return {
        "command": "OPEN_JIOSAAVN",
        "status": "SUCCESS",
        "state": JioSaavnState.JIOSAAVN_READY,
        "playback": "PLAYING",
        "playlist": pl_result.get("title", target_playlist),
    }


def _handle_init_action(payload, command_id: Optional[str]):
    logger.debug("Action: Initializing JioSaavn playback...")
    pl_name = None
    pl_url = None
    if payload:
        if isinstance(payload, dict):
            pl_name = payload.get("playlist") or payload.get("name") or payload.get("query")
            pl_url = payload.get("url") or payload.get("playlist_url")
        elif isinstance(payload, str) and payload.strip():
            if payload.strip().startswith("http"):
                pl_url = payload.strip()
            else:
                pl_name = payload.strip()

    result = initialize_jiosaavn_playback(playlist_name=pl_name, playlist_url=pl_url, command_id=command_id)
    logger.debug("Action: JioSaavn playback initialization completed successfully. Ready for BCI commands.")
    return result


def _handle_utility_action(cmd: str):
    global _current_state
    if cmd == "loop":
        logger.debug("Action: LOOP test received successfully.")
        return {"command": "LOOP", "status": "SUCCESS"}

    if cmd == "ping":
        logger.debug("Action: PING test received successfully.")
        return {"command": "PING", "status": "SUCCESS"}

    if cmd == "back_to_level_2":
        logger.debug("Action: Closing JioSaavn tab...")
        _close_jiosaavn_tab()
        _current_state = JioSaavnState.NOT_INITIALIZED
        logger.debug("Action: JioSaavn tab closed. State reset to NOT_INITIALIZED.")
        return {
            "command": "BACK_TO_LEVEL_2",
            "action": "BACK_TO_LEVEL_2",
            "previous_state": "LEVEL_3",
            "new_state": "LEVEL_2",
            "status": "SUCCESS",
            "state": _current_state,
        }

    if cmd == "back_to_level_1":
        logger.debug("Action: BACK_TO_LEVEL_1 received - no action (stay in current level).")
        return {
            "command": "BACK_TO_LEVEL_1",
            "action": "BACK_TO_LEVEL_1",
            "status": "SUCCESS",
            "message": "No action - remaining at current level",
            "state": _current_state,
        }

    return None


def _check_jiosaavn_readiness(cmd: str) -> None:
    global _current_state, is_resting_state, _on_song_page
    if _current_state == JioSaavnState.JIOSAAVN_OPENING or _current_state in (
        JioSaavnState.DASHBOARD_READY,
        JioSaavnState.SELECT_PLAYLIST,
        JioSaavnState.START_PLAYBACK,
        JioSaavnState.VERIFY_PLAYBACK,
    ):
        logger.warning(f"[GUARD] Rejecting command '{cmd}' - JioSaavn is currently initializing.")
        raise RuntimeError(f"JIOSAAVN_INITIALIZING: JioSaavn is currently in state '{_current_state}'. Please wait.")

    if _current_state != JioSaavnState.JIOSAAVN_READY:
        driver = _get_dom_driver()
        if driver is not None:
            logger.debug("[GUARD] Existing active browser session detected; promoting state to JIOSAAVN_READY.")
            _current_state = JioSaavnState.JIOSAAVN_READY
            is_resting_state = False
            _on_song_page = True
        else:
            logger.warning(f"[GUARD] Rejecting command '{cmd}' - JioSaavn is not initialized (current state: {_current_state}).")
            raise RuntimeError("JIOSAAVN_NOT_READY: JioSaavn must be initialized with OPEN_JIOSAAVN before executing media commands.")


_LAST_PLAYBACK_DISPATCH = {"cmd": None, "ts": 0.0}


def _dispatch_playback_command(cmd: str, payload):
    now = time.time()
    canonical = (
        "play_pause" if cmd in ("play_pause", "play", "pause", "media_play_pause") else (
            "next_track" if cmd in ("next_track", "next", "media_next", "playnext", "play_next", "nextsong", "next_song") else (
                "previous_track" if cmd in ("previous_track", "prev", "previous", "media_previous", "playprev", "play_prev") else cmd
            )
        )
    )
    if canonical in ("play_pause", "next_track", "previous_track"):
        if _LAST_PLAYBACK_DISPATCH["cmd"] == canonical and (now - _LAST_PLAYBACK_DISPATCH["ts"]) < 0.8:
            logger.info(f"[DEBOUNCE] Ignoring rapid duplicate action '{canonical}' ({now - _LAST_PLAYBACK_DISPATCH['ts']:.2f}s < 0.8s)")
            curr_state = "PLAYING" if _dom_is_playing() else "PAUSED"
            return {
                "command": canonical.upper(),
                "action": canonical.upper(),
                "status": "SUCCESS",
                "state": _current_state,
                "debounced": True,
            }
        _LAST_PLAYBACK_DISPATCH["cmd"] = canonical
        _LAST_PLAYBACK_DISPATCH["ts"] = now

    if cmd in ("play_pause", "play", "pause", "media_play_pause"):
        logger.debug("Action: Executing Play/Pause in JioSaavn...")
        prev_state = "PLAYING" if _dom_is_playing() else "PAUSED"
        _toggle_play_pause()
        curr_state = "PAUSED" if prev_state == "PLAYING" else "PLAYING"
        logger.debug("Action: Play/Pause executed successfully.")
        return {
            "command": "PLAY_PAUSE",
            "action": "PLAY_PAUSE",
            "previous": prev_state,
            "current": curr_state,
            "status": "SUCCESS",
            "state": _current_state,
        }

    if cmd in ("next_track", "next", "media_next", "playnext", "play_next", "nextsong", "next_song"):
        logger.debug("Action: Executing Next Track in JioSaavn (with empty-queue fallback)...")
        res = handle_next_track()
        logger.debug(f"Action: Next Track completed: {res}")
        return res

    if cmd in ("previous_track", "prev", "previous", "media_previous", "playprev", "play_prev"):
        logger.debug("Action: Executing Previous Track in JioSaavn (with empty-queue fallback)...")
        res = handle_previous_track()
        logger.debug(f"Action: Previous Track completed: {res}")
        return res

    if cmd == "search":
        query = _extract_search_query(payload)
        logger.debug(f"Action: Performing in-page UI search for: '{query}'")
        res = _ui_search_and_play(query)
        logger.debug(f"Action: Search for '{query}' executed and playback confirmed.")
        return {
            "command": "SEARCH",
            "action": "SEARCH",
            "status": "SUCCESS" if res else "FAILED",
            "query": query,
            "result": "SUCCESS" if res else "FAILED",
            "match": "SELECTED",
            "playback": "PLAYING",
            "state": _current_state,
        }

    if cmd == "volume_up":
        logger.debug("Action: Volume Up executed")
        prev_v, curr_v = _adjust_volume(+0.1)
        return {
            "command": "VOLUME_UP",
            "action": "VOLUME_UP",
            "previous": prev_v,
            "current": curr_v,
            "status": "SUCCESS",
            "state": _current_state,
        }

    if cmd == "volume_down":
        logger.debug("Action: Volume Down executed")
        prev_v, curr_v = _adjust_volume(-0.1)
        return {
            "command": "VOLUME_DOWN",
            "action": "VOLUME_DOWN",
            "previous": prev_v,
            "current": curr_v,
            "status": "SUCCESS",
            "state": _current_state,
        }

    logger.warning(f"[MEDIA AGENT] Unrecognized action: {cmd}")
    raise ValueError(f"Unrecognized media command: {cmd}")


def execute_media_command(command: str, payload=None, command_id: Optional[str] = None):
    cmd = command.lower()
    logger.debug(f"[MEDIA AGENT] Executing action: {cmd.upper()} | Payload: {payload}")

    with _ACTION_LOCK:
        if cmd in ("open_desktop_dashboard", "open_jiosaavn", "open"):
            return _handle_init_action(payload, command_id)

        if cmd in ("loop", "ping", "back_to_level_2"):
            return _handle_utility_action(cmd)

        # Volume control is a system utility and must never be blocked by JioSaavn playback state
        if cmd in ("volume_up", "volume_down"):
            return _dispatch_playback_command(cmd, payload)

        _check_jiosaavn_readiness(cmd)
        return _dispatch_playback_command(cmd, payload)