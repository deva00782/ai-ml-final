"""
Cross-platform BrowserManager for JioSaavn Master Hub.

Works on Windows, macOS, and Linux. All Chrome interactions go through
the remote-debugging port (127.0.0.1:9222) so no OS-specific accessibility
hacks are needed. OS differences are isolated to:
  - detecting / launching the debug Chrome (via start_chrome.py)
  - building ChromeOptions for Selenium attach
  - fallback media-key handling (delegated to os_operations.py)
"""
import time
import socket
import platform
import shutil
import subprocess
import os
import sys
from pathlib import Path

from selenium import webdriver
from logger import logger

URL_JIOSAAVN = "https://www.jiosaavn.com"
DOMAIN_JIOSAAVN = "jiosaavn.com"
PREFIX_HTTP = "http://"
PREFIX_HTTPS = "https://"


class BrowserManager:
    MODULE_NAME = "BrowserManager"
    DEBUG_PORT = 9222
    LAUNCH_COOLDOWN = 15.0

    def __init__(self, headless=False):
        self.headless = headless
        self.driver = None
        self._chrome_proc = None
        self._last_launch_attempt = 0.0
        self._system = platform.system()  # Darwin / Windows / Linux

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _is_port_open(self) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                return s.connect_ex(("127.0.0.1", self.DEBUG_PORT)) == 0
        except Exception:
            return False

    def _is_debug_chrome_windows(self) -> bool:
        try:
            r = subprocess.run(
                ['wmic', 'process', 'where', 'name="chrome.exe"', 'get', 'CommandLine'],
                capture_output=True, text=True, timeout=3
            )
            if "remote-debugging-port" in r.stdout:
                return True
        except Exception:
            pass
        try:
            r = subprocess.run(
                ['powershell', '-Command',
                 "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Select-Object -ExpandProperty CommandLine"],
                capture_output=True, text=True, timeout=3
            )
            if "remote-debugging-port" in r.stdout:
                return True
        except Exception:
            pass
        return False

    def _is_debug_chrome_unix(self) -> bool:
        pgrep = shutil.which("pgrep")
        if pgrep:
            r = subprocess.run([pgrep, "-f", f"remote-debugging-port={self.DEBUG_PORT}"],
                               capture_output=True, text=True, timeout=2)
            return r.returncode == 0 and bool(r.stdout.strip())
        ps = shutil.which("ps")
        if ps:
            r = subprocess.run([ps, "aux"], capture_output=True, text=True, timeout=2)
            if f"remote-debugging-port={self.DEBUG_PORT}" in r.stdout:
                return True
        return False

    def _is_debug_chrome_process_running(self) -> bool:
        """
        Cross-platform check: is a chrome process with --remote-debugging-port running?
        Returns False on error (so caller falls through to launch).
        """
        try:
            if self._system == "Windows":
                return self._is_debug_chrome_windows()
            return self._is_debug_chrome_unix()
        except Exception:
            return False

    def _build_chrome_options(self) -> webdriver.ChromeOptions:
        opts = webdriver.ChromeOptions()
        opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{self.DEBUG_PORT}")
        # Headless is NOT used when attaching to existing Chrome; flag kept for
        # future use and for explicit driver creation outside debug mode.
        # On Linux, remote debugging still benefits from a few stability flags
        # when the user runs start_chrome.py with those flags.
        if self._system == "Linux":
            # These are harmless when attaching (they are ignored) but document intent
            pass
        return opts

    # ------------------------------------------------------------------
    # Chrome status + auto-start
    # ------------------------------------------------------------------
    def _check_chrome_status(self, url=URL_JIOSAAVN):
        """
        Smart Chrome Status Detection with guarded auto-start:
        - Checks if port 9222 is open -> already debugging.
        - If not, checks cooldown to avoid spamming launches.
        - Optionally checks if a debug chrome process is already starting.
        - Otherwise launches start_chrome.py (cross-platform) or legacy .sh.
        Never kills or restarts the user's own Chrome.
        """
        if self._is_port_open():
            logger.debug(self.MODULE_NAME, "✅ Chrome is running with remote debugging enabled")
            return "chrome_debugging"

        now = time.monotonic()
        if now - self._last_launch_attempt < self.LAUNCH_COOLDOWN:
            logger.debug(self.MODULE_NAME, "Chrome launch cooldown active — waiting")
            return "chrome_start_pending"

        # If a debug process seems to be starting, just wait
        if self._is_debug_chrome_process_running():
            self._last_launch_attempt = now
            logger.debug(self.MODULE_NAME, "Chrome is starting with remote debugging...")
            return "chrome_starting"

        # Need to launch a dedicated debug Chrome
        self._last_launch_attempt = now
        try:
            try:
                from start_chrome import (
                    get_chrome_binary,
                    get_debug_dir,
                    get_default_profile_dir,
                    prepare_debug_profile,
                    launch_chrome,
                )
            except Exception:
                from .start_chrome import (
                    get_chrome_binary,
                    get_debug_dir,
                    get_default_profile_dir,
                    prepare_debug_profile,
                    launch_chrome,
                )

            chrome_bin = get_chrome_binary()
            if not chrome_bin:
                logger.error(self.MODULE_NAME, "❌ Chrome/Chromium binary not found.")
                return "chrome_not_running"

            debug_dir = get_debug_dir()
            profile_src = get_default_profile_dir()
            prepare_debug_profile(profile_src, debug_dir)
            self._chrome_proc = launch_chrome(chrome_bin, debug_dir, self.DEBUG_PORT, url=url)
            logger.success(self.MODULE_NAME, "✅ Chrome launched directly (no console window)")
            logger.info(self.MODULE_NAME, "⏳ Waiting for Chrome to start...")
            return "chrome_started"

        except Exception as e:
            logger.error(self.MODULE_NAME, f"❌ Failed to launch Chrome: {e}")
            return "chrome_not_running"

    # ------------------------------------------------------------------
    # Selenium driver
    # ------------------------------------------------------------------
    def _wait_for_port(self, timeout: float):
        t_start = time.monotonic()
        while time.monotonic() - t_start < timeout:
            time.sleep(0.5)
            if self._is_port_open():
                break

    def _get_attach_hint(self) -> str:
        if self._system == "Windows":
            return " (Windows: ensure Chrome was started with --remote-debugging-port=9222 and no other Chrome is blocking the port)"
        if self._system == "Linux":
            return " (Linux: ensure chrome --remote-debugging-port=9222 is running; check that /tmp or ~/chrome-debug-profile is writable)"
        return ""

    def get_or_create_driver(self, wait_if_starting: bool = False, timeout: float = 15.0):
        """
        Persistent Driver Singleton:
        Attaches strictly to 127.0.0.1:9222. Reuses self.driver if active.
        NEVER spawns any new Chrome process or window.
        Works identically on Windows / macOS / Linux.
        """
        if self.driver is not None:
            try:
                _ = self.driver.title
                _ = len(self.driver.window_handles)
                return self.driver
            except Exception:
                logger.warn(self.MODULE_NAME, "Existing Chrome session lost. Attempting re-attachment...")
                self.driver = None

        if not self._is_port_open():
            self._check_chrome_status()
            if wait_if_starting:
                self._wait_for_port(timeout)

        if not self._is_port_open():
            logger.debug(self.MODULE_NAME, f"Chrome port {self.DEBUG_PORT} is inactive. DOM automation disabled, using OS media keys instead.")
            return None

        try:
            debug_opts = self._build_chrome_options()
            self.driver = webdriver.Chrome(options=debug_opts)
            logger.success(self.MODULE_NAME, f"✅ Attached to Chrome instance on port {self.DEBUG_PORT} ({self._system})!")
            return self.driver
        except Exception as attach_err:
            hint = self._get_attach_hint()
            logger.error(self.MODULE_NAME, f"Failed to attach to Chrome on port {self.DEBUG_PORT}{hint}: {attach_err}")
            self.driver = None
            return None

    def focus_or_get_jiosaavn_tab(self):
        """
        Strict Tab Detection:
        Iterates through driver.window_handles.
        Switches to each handle and checks if 'jiosaavn.com' in driver.current_url.lower().
        If found, switches to it and returns True.
        NEVER opens new windows or tabs if not found.
        Cross-platform: same DOM, same Selenium API on all OSes.
        """
        driver = self.get_or_create_driver()
        if not driver:
            logger.debug(self.MODULE_NAME, "No active Chrome driver connected on port 9222. Using OS media keys instead.")
            return False

        try:
            handles = driver.window_handles
            for handle in handles:
                try:
                    driver.switch_to.window(handle)
                    curr_url = (driver.current_url or "").lower()
                    if DOMAIN_JIOSAAVN in curr_url:
                        logger.info(self.MODULE_NAME, f"Focused existing JioSaavn tab: {driver.current_url}")
                        return True
                except Exception:
                    continue

            logger.warn(self.MODULE_NAME, "No active JioSaavn tab found among open Chrome windows. Please navigate to JioSaavn in your browser.")
            return False
        except Exception as e:
            logger.error(self.MODULE_NAME, f"Error detecting JioSaavn tab: {e}")
            return False

    def execute_js_click(self, selectors, description="element"):
        """Reuses self.driver to execute DOM clicks on existing tab without spawning windows. Cross-platform."""
        if not self.focus_or_get_jiosaavn_tab():
            return False

        try:
            js_script = """
            const selectors = arguments[0];
            for (let sel of selectors) {
                let elems = document.querySelectorAll(sel);
                for (let el of elems) {
                    let text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (text.includes('new playlist') || text.includes('create')) continue;
                    if (el && (el.offsetParent !== null || el.offsetWidth > 0)) {
                        try {
                            el.click();
                            return { success: true, selector: sel };
                        } catch (e) {
                            try {
                                el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                                return { success: true, selector: sel };
                            } catch (e2) {}
                        }
                    }
                }
            }
            return { success: false };
            """
            result = self.driver.execute_script(js_script, selectors)
            if result and result.get("success"):
                logger.success(self.MODULE_NAME, f"✅ Clicked '{result.get('selector')}' for {description}")
                return True
            else:
                logger.warn(self.MODULE_NAME, f"DOM click found no matching elements for {description}")
                return False
        except Exception as e:
            logger.warn(self.MODULE_NAME, f"JS click execution error for {description}: {e}")
            return False

    def open_new_tab(self, url=URL_JIOSAAVN):
        """
        Explicitly opens a new browser tab with the specified URL and switches focus to it.
        Cross-platform: uses Selenium new window API, fallback to window.open JS.
        """
        driver = self.get_or_create_driver(wait_if_starting=True, timeout=15.0)
        if not driver:
            return False

        try:
            # If JioSaavn tab already exists, focus it
            if self.focus_or_get_jiosaavn_tab():
                logger.info(self.MODULE_NAME, f"Focused active JioSaavn tab: {driver.current_url}")
                return True

            initial_handles = set(driver.window_handles)
            try:
                driver.switch_to.new_window('tab')
                driver.get(url)
            except Exception:
                driver.execute_script(f"window.open('{url}', '_blank');")
                time.sleep(0.5)
                new_handles = set(driver.window_handles) - initial_handles
                if new_handles:
                    driver.switch_to.window(next(iter(new_handles)))
                elif driver.window_handles:
                    driver.switch_to.window(driver.window_handles[-1])
            
            logger.success(self.MODULE_NAME, f"🚀 Opened new browser tab: {url}")
            return True
        except Exception as e:
            logger.error(self.MODULE_NAME, f"Failed to open new tab for {url}: {e}")
            return False

    def check_queue_status(self) -> dict:
        """
        Inspects the active JioSaavn tab's player queue and controls.
        Returns a dict:
        {
            "is_empty": bool,
            "is_single_track": bool,
            "current_title": str,
            "next_disabled": bool,
            "prev_disabled": bool,
            "queue_length": int
        }
        """
        default_res = {
            "is_empty": True,
            "is_single_track": False,
            "current_title": "",
            "next_disabled": True,
            "prev_disabled": True,
            "queue_length": 0
        }
        if not self.focus_or_get_jiosaavn_tab():
            return default_res

        try:
            js_script = """
            const result = {
                is_empty: true,
                is_single_track: false,
                current_title: '',
                next_disabled: true,
                prev_disabled: true,
                queue_length: 0,
                is_playing: false
            };

            // 1. Get current track title from bottom player bar
            const titleEl = document.querySelector(
                '#player_song, ' +
                '#player .c-player__title, ' +
                '.c-player__title, ' +
                '.c-player__song, ' +
                '[data-testid="player-song-title"], ' +
                '#player a[href*="/song/"], ' +
                '#player_song_name'
            );
            if (titleEl) {
                result.current_title = (titleEl.innerText || titleEl.textContent || '').trim();
            }

            const cleanTitle = (result.current_title || '').toLowerCase();
            const hasTitle = cleanTitle.length > 0 && 
                             !cleanTitle.includes('waiting') &&
                             !cleanTitle.includes('welcome') &&
                             !cleanTitle.includes('history') &&
                             !cleanTitle.includes('home') &&
                             cleanTitle !== 'jiosaavn';

            if (!hasTitle) {
                result.current_title = '';
            }

            // 2. Audio element stream check (strictly active audio with duration or unpaused)
            const audios = document.querySelectorAll('audio');
            let activeAudio = null;
            let isPlaying = false;
            for (let a of audios) {
                if ((a.src && a.src.length > 10 && !a.src.endsWith('/') && !a.src.endsWith('#')) ||
                    (a.currentSrc && a.currentSrc.length > 10)) {
                    if (a.duration > 0 || !a.paused || a.currentTime > 0) {
                        activeAudio = a;
                        if (!a.paused && a.currentTime > 0) {
                            isPlaying = true;
                        }
                        break;
                    }
                }
            }

            // 3. Play/Pause button check - 'pause' state means music is actively playing
            const playPauseBtn = document.querySelector('#player_play_pause, .c-player__play, [data-testid="play-pause-btn"]');
            if (playPauseBtn) {
                const btnTitle = (playPauseBtn.getAttribute('title') || playPauseBtn.getAttribute('aria-label') || '').toLowerCase();
                const btnCls = playPauseBtn.className.toLowerCase();
                if (btnTitle.includes('pause') || btnCls.includes('pause') || btnCls.includes('playing') || btnCls.includes('is-playing')) {
                    isPlaying = true;
                }
            }
            result.is_playing = isPlaying;

            // 4. Queue items check
            const queueItems = document.querySelectorAll(
                '#player_queue_list > li, ' +
                '.c-queue__list > li, ' +
                'aside.c-queue .c-list__item, ' +
                'aside.c-queue li.c-queue__item, ' +
                '.c-queue__item'
            );
            const hasQueueItems = queueItems && queueItems.length > 0;

            // Only mark not empty if there is an actual track title, active audio, active playback, or queue items
            if (hasTitle || activeAudio || isPlaying || hasQueueItems) {
                result.is_empty = false;
            } else {
                result.is_empty = true;
            }

            // If empty, buttons are disabled and queue length is 0
            if (result.is_empty) {
                result.next_disabled = true;
                result.prev_disabled = true;
                result.queue_length = 0;
                result.is_single_track = false;
                result.is_playing = false;
                return result;
            }

            // 5. Check next button state
            const nextBtn = document.querySelector('#player_next, [title="Next"], .c-player__next, .js-next, #next');
            if (nextBtn) {
                result.next_disabled = nextBtn.classList.contains('is-disabled') ||
                                       nextBtn.classList.contains('disabled') ||
                                       nextBtn.disabled ||
                                       nextBtn.getAttribute('aria-disabled') === 'true';
            } else {
                result.next_disabled = true;
            }

            // 6. Check prev button state
            const prevBtn = document.querySelector('#player_prev, [title="Previous"], .c-player__prev, .js-prev, #prev');
            if (prevBtn) {
                result.prev_disabled = prevBtn.classList.contains('is-disabled') ||
                                       prevBtn.classList.contains('disabled') ||
                                       prevBtn.disabled ||
                                       prevBtn.getAttribute('aria-disabled') === 'true';
            } else {
                result.prev_disabled = true;
            }

            // 7. Check queue length from DOM
            if (hasQueueItems) {
                result.queue_length = queueItems.length;
                if (queueItems.length === 1) {
                    result.is_single_track = true;
                }
            } else {
                // If player has a song but next button is disabled (or both next & prev disabled), queue only has 1 song
                if (result.next_disabled || (result.next_disabled && result.prev_disabled)) {
                    result.is_single_track = true;
                    result.queue_length = 1;
                } else {
                    result.queue_length = -1;
                }
            }

            return result;
            """
            data = self.driver.execute_script(js_script)
            if isinstance(data, dict):
                # Safeguard: if title is empty and not playing and no queue items, it is empty
                if not data.get("current_title") and not data.get("is_playing") and data.get("queue_length", 0) <= 0:
                    data["is_empty"] = True
                    data["next_disabled"] = True
                    data["prev_disabled"] = True
                    data["queue_length"] = 0
                return data
            return default_res
        except Exception as e:
            logger.debug(self.MODULE_NAME, f"check_queue_status error: {e}")
            return default_res

    def is_queue_empty(self) -> bool:
        """Returns True if no song or queue is currently loaded in the media player."""
        status = self.check_queue_status()
        if status.get("is_empty", True):
            return True
        if not status.get("current_title") and not status.get("is_playing"):
            return True
        return False

    def toggle_play_pause(self) -> bool:
        """
        Deterministically toggles JioSaavn audio play/pause state.
        Uses exact activeAudio inspection and single-click execution from JioSaavan controller.
        Prevents double-triggering or state reversals.
        """
        if not self.focus_or_get_jiosaavn_tab():
            return False

        js_toggle = """
        const audios = document.querySelectorAll('audio');
        let activeAudio = null;
        let isCurrentlyPlaying = false;

        for (let a of audios) {
            if ((a.src && a.src.length > 10 && !a.src.endsWith('/') && !a.src.endsWith('#')) ||
                (a.currentSrc && a.currentSrc.length > 10)) {
                activeAudio = a;
                if (!a.paused && a.currentTime > 0) {
                    isCurrentlyPlaying = true;
                }
                break;
            }
        }

        const bottomBtn = document.querySelector('#player_play_pause') || 
                          document.querySelector('.c-player__btn--play') || 
                          document.querySelector('.c-player__btn--pause') || 
                          document.querySelector('.c-player__play') ||
                          document.querySelector('#player_play');

        if (isCurrentlyPlaying) {
            // Currently playing -> PAUSE
            if (bottomBtn) { try { bottomBtn.click(); } catch(e) {} }
            if (activeAudio && !activeAudio.paused) { activeAudio.pause(); }
            return { success: true, mode: 'paused', is_playing: false };
        } else {
            // Currently paused -> PLAY / RESUME
            if (bottomBtn) { try { bottomBtn.click(); } catch(e) {} }
            if (activeAudio && activeAudio.paused) { activeAudio.play().catch(e => {}); }
            return { success: true, mode: 'resumed', is_playing: true };
        }
        """
        try:
            res = self.driver.execute_script(js_toggle)
            if res and res.get("success"):
                logger.info(self.MODULE_NAME, f"Playback state toggled: mode={res.get('mode')}")
                return True
        except Exception as e:
            logger.debug(self.MODULE_NAME, f"toggle_play_pause JS error: {e}")

        # Fallback to selectors if needed
        return self.execute_js_click(["#player_play_pause", ".c-player__play"], description="Play / Pause Button")

    def pause(self) -> bool:
        """
        Immediately pauses any active JioSaavn audio playback.
        Ensures player is strictly paused without toggling back to play.
        """
        if not self.focus_or_get_jiosaavn_tab():
            return False

        js_pause = """
        let paused = false;
        const audios = document.querySelectorAll('audio');
        for (let a of audios) {
            try {
                if (!a.paused) {
                    a.pause();
                    paused = true;
                }
            } catch(e) {}
        }

        const bottomBtn = document.querySelector('#player_play_pause') || 
                          document.querySelector('.c-player__btn--pause') || 
                          document.querySelector('.c-player__play');

        if (bottomBtn) {
            const btnTitle = (bottomBtn.getAttribute('title') || bottomBtn.getAttribute('aria-label') || '').toLowerCase();
            const btnCls = bottomBtn.className.toLowerCase();
            if (btnTitle.includes('pause') || btnCls.includes('pause') || btnCls.includes('playing')) {
                try {
                    bottomBtn.click();
                    paused = true;
                } catch(e) {}
            }
        }
        return { success: true, paused: paused };
        """
        try:
            res = self.driver.execute_script(js_pause)
            if res and res.get("paused"):
                logger.info(self.MODULE_NAME, "Paused active playback.")
            return True
        except Exception as e:
            logger.debug(self.MODULE_NAME, f"pause JS error: {e}")
            return False

    def _click_primary_play_button(self, description="Play Button", retries=4) -> bool:
        """
        Robustly clicks the primary play button on the active JioSaavn page (playlist, album, song).
        Retries up to `retries` times to allow React/DOM to mount.
        """
        if not self.focus_or_get_jiosaavn_tab():
            return False

        js_click_play = """
        const playSelectors = [
            '.c-hero .c-btn--primary',
            '.c-hero a.c-btn--primary',
            '.c-hero button.c-btn--primary',
            '.c-hero [title*="Play" i]',
            '.c-hero [aria-label*="Play" i]',
            '.c-hero .o-icon-play',
            '.c-hero__actions button',
            '.c-hero__actions a',
            'a.c-btn--primary.js-play-button',
            'button.c-btn--primary.js-play-button',
            'a.js-play-button',
            'button.js-play-button',
            '.js-play-button',
            '[data-action="play-playlist"]',
            '[data-action="play-album"]',
            '[data-action="play"]',
            '.c-list__item:first-child .c-song__play',
            '.c-list__item:first-child [title*="Play" i]',
            '.c-list__item:first-child button[aria-label*="Play" i]',
            '.c-list__item:first-child .o-icon-play',
            '.c-song:first-of-type .c-song__play',
            '.c-song__play',
            '.c-btn--play',
            'button.c-btn--play',
            'a.c-btn--play',
            'a[aria-label*="Play" i]',
            'button[aria-label*="Play" i]',
            'span[aria-label*="Play" i]',
            '[title*="Play" i]',
            '[data-testid*="play" i]',
            '.c-play'
        ];

        for (let sel of playSelectors) {
            let elems = document.querySelectorAll(sel);
            for (let el of elems) {
                // Ignore elements inside bottom persistent player bar
                if (el.closest && el.closest('.c-player, #player, #player_play_pause, .c-player__controls, footer')) {
                    continue;
                }
                let text = (el.innerText || el.textContent || '').trim().toLowerCase();
                if (text.includes('new playlist') || text.includes('create') || text.includes('log in') || text.includes('sign up')) {
                    continue;
                }
                if (el && (el.offsetParent !== null || el.offsetWidth > 0)) {
                    try {
                        el.click();
                        return { success: true, selector: sel, text: text };
                    } catch(e) {
                        try {
                            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                            return { success: true, selector: sel, text: text };
                        } catch(e2) {}
                    }
                }
            }
        }
        return { success: false };
        """
        for attempt in range(retries):
            try:
                result = self.driver.execute_script(js_click_play)
                if result and result.get("success"):
                    logger.success(self.MODULE_NAME, f"✅ Triggered {description} via '{result.get('selector')}'")
                    return True
            except Exception as e:
                logger.debug(self.MODULE_NAME, f"Play button attempt {attempt+1} error: {e}")
            time.sleep(1.0)

        logger.warn(self.MODULE_NAME, f"Play button click did not register for {description}.")
        return False

    def _add_target_to_queue(self, description="Searched Song", is_queue_empty=False) -> bool:
        """
        Adds the song/album to the existing queue without clearing the queue.
        Attempts 'Add to Queue' button, 'More (...)' menu -> 'Add to Queue',
        or song row actions. If the queue was empty, falls back to playing.
        """
        if not self.focus_or_get_jiosaavn_tab():
            return False

        js_add_queue = """
        function clickSingle(el) {
            if (!el) return false;
            try { el.focus(); } catch(e) {}
            try { el.click(); return true; } catch(e) {}
            try {
                el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                return true;
            } catch(e2) {
                return false;
            }
        }

        // Strategy 1: Check direct "Add to Queue" buttons in hero or action bar
        const directQueueSelectors = [
            'button[title*="Add to queue" i]',
            'a[title*="Add to queue" i]',
            'button[aria-label*="Add to queue" i]',
            'a[aria-label*="Add to queue" i]',
            '[data-action="add-to-queue"]',
            '.c-card__btn-queue',
            '.c-btn--queue',
            'button[aria-label*="Queue" i]',
            'button[title*="Queue" i]'
        ];

        for (let sel of directQueueSelectors) {
            let el = document.querySelector(sel);
            if (el && (el.offsetParent !== null || el.offsetWidth > 0)) {
                if (clickSingle(el)) {
                    return { success: true, method: 'direct_button', selector: sel };
                }
            }
        }

        // Strategy 2: Check 3-dots / More menu in hero / actions
        const moreSelectors = [
            '.c-hero__actions button[aria-label*="More" i]',
            '.c-hero__actions [title*="More" i]',
            '.c-hero__actions .o-icon-more',
            '.c-hero button[aria-label*="More" i]',
            '.c-hero [title*="More" i]',
            '.c-hero .o-icon-more',
            'button[aria-label*="More options" i]',
            'button[title*="More options" i]',
            'button[aria-label*="More" i]',
            'button[title*="More" i]',
            '.o-icon-more',
            '.c-btn--more',
            'button.c-btn--more'
        ];

        for (let sel of moreSelectors) {
            let moreBtn = document.querySelector(sel);
            if (moreBtn && (moreBtn.offsetParent !== null || moreBtn.offsetWidth > 0)) {
                clickSingle(moreBtn);
                const menuItems = document.querySelectorAll('.c-dropdown__item, [role="menuitem"], .o-menu__item, li button, div[role="dialog"] button, .c-dropdown a, .o-menu a, .c-menu__item');
                for (let item of menuItems) {
                    const txt = (item.innerText || item.textContent || '').trim().toLowerCase();
                    if (txt.includes('add to queue') || txt.includes('queue') || txt.includes('play next')) {
                        if (clickSingle(item)) {
                            return { success: true, method: 'more_menu', text: txt };
                        }
                    }
                }
            }
        }

        // Strategy 3: Target first track row on page
        const rowSelectors = ['.c-list__item', 'article.o-snippet', 'figure.o-snippet', '.c-song', 'tr.c-list__item'];
        for (let rowSel of rowSelectors) {
            let row = document.querySelector(rowSel);
            if (row && (row.offsetParent !== null || row.offsetWidth > 0)) {
                try {
                    row.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
                    row.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true, cancelable: true, view: window }));
                } catch(e) {}

                let rowQueue = row.querySelector('[title*="Add to queue" i], [aria-label*="Add to queue" i], [data-action="add-to-queue"], .c-card__btn-queue');
                if (rowQueue && clickSingle(rowQueue)) {
                    return { success: true, method: 'row_direct_queue' };
                }

                let rowMore = row.querySelector('[aria-label*="More" i], [title*="More" i], .o-icon-more, button.c-btn--more');
                if (rowMore && clickSingle(rowMore)) {
                    const menuItems = document.querySelectorAll('.c-dropdown__item, [role="menuitem"], .o-menu__item, li button, div[role="dialog"] button, .c-dropdown a, .o-menu a, .c-menu__item');
                    for (let item of menuItems) {
                        const txt = (item.innerText || item.textContent || '').trim().toLowerCase();
                        if (txt.includes('add to queue') || txt.includes('queue') || txt.includes('play next')) {
                            if (clickSingle(item)) {
                                return { success: true, method: 'row_more_queue', text: txt };
                            }
                        }
                    }
                }
            }
        }

        return { success: false };
        """

        for attempt in range(4):
            try:
                res = self.driver.execute_script(js_add_queue)
                if res and res.get("success"):
                    logger.success(self.MODULE_NAME, f"✅ Added '{description}' to existing queue via {res.get('method')}")
                    return True
            except Exception as e:
                logger.debug(self.MODULE_NAME, f"Add to queue attempt {attempt+1} error: {e}")
            time.sleep(0.8)

        # If queue was already empty, fallback to primary play button to initialize player
        if is_queue_empty:
            logger.info(self.MODULE_NAME, f"Queue was empty; starting playback for '{description}'...")
            return self._click_primary_play_button(description=description, retries=3)

        logger.warn(self.MODULE_NAME, f"Could not find 'Add to Queue' button for {description}.")
        return False

    def _add_playlist_to_queue(self, description="Playlist") -> bool:
        """
        Attempts to add the playlist/album on the current page to the queue
        WITHOUT starting playback (via 'Add to Queue' button or hero 3-dots menu).
        """
        if not self.focus_or_get_jiosaavn_tab():
            return False

        # Phase 1: Direct "Add to Queue" button on the playlist hero
        js_direct_queue = """
        const directSelectors = [
            '.c-hero button[title*="Add to queue" i]',
            '.c-hero a[title*="Add to queue" i]',
            '.c-hero button[aria-label*="Add to queue" i]',
            '.c-hero a[aria-label*="Add to queue" i]',
            '.c-hero__actions button[title*="Add to queue" i]',
            '.c-hero__actions a[title*="Add to queue" i]',
            '.c-hero__actions [data-action="add-to-queue"]',
            '[data-action="add-to-queue"]',
            '.c-btn--queue',
            '.c-card__btn-queue',
            'button[title*="Add to queue" i]',
            'a[title*="Add to queue" i]'
        ];
        for (let sel of directSelectors) {
            let el = document.querySelector(sel);
            if (el && (el.offsetParent !== null || el.offsetWidth > 0)) {
                try { el.focus(); } catch(e) {}
                try { el.click(); return { success: true, method: 'direct_button', selector: sel }; } catch(e) {}
                try {
                    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    return { success: true, method: 'direct_button_event', selector: sel };
                } catch(e2) {}
            }
        }
        return { success: false };
        """
        try:
            res = self.driver.execute_script(js_direct_queue)
            if res and res.get("success"):
                logger.success(self.MODULE_NAME, f"✅ Added {description} to queue via direct button '{res.get('selector')}'")
                return True
        except Exception as e:
            logger.debug(self.MODULE_NAME, f"Direct playlist queue error: {e}")

        # Phase 2: Hero actions 3-dots menu -> 'Add to Queue'
        js_open_hero_more = """
        const heroMoreSelectors = [
            '.c-hero__actions .o-icon-more',
            '.c-hero__actions button[aria-label*="More" i]',
            '.c-hero__actions button[title*="More" i]',
            '.c-hero button[aria-label*="More" i]',
            '.c-hero button[title*="More" i]',
            '.c-hero .o-icon-more',
            '.c-hero .c-btn--more',
            '.c-hero button.c-btn--more',
            'button[aria-label*="More options" i]',
            'button[title*="More options" i]'
        ];
        for (let sel of heroMoreSelectors) {
            let el = document.querySelector(sel);
            if (el && (el.offsetParent !== null || el.offsetWidth > 0)) {
                try { el.focus(); } catch(e) {}
                try { el.click(); return { success: true, selector: sel }; } catch(e) {}
                try {
                    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    return { success: true, selector: sel };
                } catch(e2) {}
            }
        }
        return { success: false };
        """

        js_click_queue_item = """
        const menuSelectors = [
            '.c-dropdown__item',
            '[role="menuitem"]',
            '.o-menu__item',
            '.c-menu__item',
            'div[role="dialog"] button',
            'div[role="dialog"] a',
            '.c-dropdown a',
            '.o-menu a',
            'li button',
            'li a'
        ];
        const items = document.querySelectorAll(menuSelectors.join(', '));
        for (let item of items) {
            const txt = (item.innerText || item.textContent || '').trim().toLowerCase();
            if (txt.includes('add to queue') || txt.includes('queue') || txt.includes('play next')) {
                try { item.focus(); } catch(e) {}
                try { item.click(); return { success: true, text: txt }; } catch(e) {}
                try {
                    item.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    return { success: true, text: txt };
                } catch(e2) {}
            }
        }
        return { success: false, count: items.length };
        """

        for attempt in range(2):
            try:
                opened = self.driver.execute_script(js_open_hero_more)
                if opened and opened.get("success"):
                    time.sleep(0.4)
                    clicked = self.driver.execute_script(js_click_queue_item)
                    if clicked and clicked.get("success"):
                        logger.success(self.MODULE_NAME, f"✅ Added {description} to queue via hero menu: '{clicked.get('text')}'")
                        return True
            except Exception as e:
                logger.debug(self.MODULE_NAME, f"Hero menu queue attempt {attempt+1} error: {e}")
            time.sleep(0.5)

        return False

    def load_first_trending_playlist(self, start_playback=True) -> bool:
        """
        Finds the first playlist in 'Trending Now' / 'Top Trending' on JioSaavn,
        navigates into it, and queues the ENTIRE playlist using 'Play All'.
        Follows the exact implementation from JioSaavan/jiosaavn_controller.py.
        If start_playback is False, pauses playback immediately after queuing.
        """
    def _navigate_to_trending_playlist(self, driver) -> bool:
        curr_url = (driver.current_url or "").lower()
        if DOMAIN_JIOSAAVN not in curr_url or any(x in curr_url for x in ["/song/", "/search", "/artist/", "/my-music/"]):
            logger.info(self.MODULE_NAME, "Navigating to JioSaavn homepage to find first trending playlist...")
            driver.get(URL_JIOSAAVN)
            time.sleep(2.0)

        js_find_playlist = """
        const sections = document.querySelectorAll('section, .u-margin-bottom-large, .c-content__item, [class*="module"], [class*="section"], div[class*="content"]');
        let trendingSection = null;

        for (let sec of sections) {
            let heading = sec.querySelector('h2, h3, .c-content__title, .u-color-js-gray');
            if (heading) {
                let headingText = (heading.innerText || heading.textContent || '').toLowerCase();
                if (headingText.includes('trending') || headingText.includes('top chart') || headingText.includes('hot right now') || headingText.includes('new release')) {
                    trendingSection = sec;
                    break;
                }
            }
        }

        let searchRoot = trendingSection || document;
        const trendingLinks = searchRoot.querySelectorAll('a[href*="/featured/"], a[href*="/album/"], a[href*="/playlist/"], a[href*="/mix/"]');

        for (let el of trendingLinks) {
            let href = el.getAttribute('href') || '';
            let text = (el.innerText || el.textContent || '').trim().toLowerCase();
            if (text.includes('log in') || text.includes('sign up') || text.includes('create')) continue;
            if (!href || href === '#' || href === '/') continue;
            if (el && (el.offsetParent !== null || el.offsetWidth > 0)) {
                let fullUrl = href.startsWith('http') ? href : 'https://www.jiosaavn.com' + href;
                return { success: true, url: fullUrl, source: trendingSection ? 'trending_section' : 'first_visible' };
            }
        }

        const anyLink = document.querySelector('a[href*="/featured/"], a[href*="/album/"], a[href*="/playlist/"], a[href*="/song/"]');
        if (anyLink) {
            let href = anyLink.getAttribute('href') || '';
            let fullUrl = href.startsWith('http') ? href : 'https://www.jiosaavn.com' + href;
            return { success: true, url: fullUrl, source: 'fallback' };
        }

        return { success: false };
        """
        res = driver.execute_script(js_find_playlist)
        if not res or not res.get("success"):
            logger.warn(self.MODULE_NAME, "Could not find any playlist link on JioSaavn homepage.")
            return False

        playlist_url = res.get("url", "")
        source = res.get("source", "unknown")
        logger.info(self.MODULE_NAME, f"Navigating to playlist from '{source}': {playlist_url}")
        driver.get(playlist_url)
        time.sleep(2.0)
        return True

    def _click_play_all(self, driver) -> bool:
        js_play_all = """
        function clickEl(el) {
            if (!el) return false;
            try { el.focus(); } catch(e) {}
            try { el.click(); return true; } catch(e) {}
            try {
                el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                return true;
            } catch(e2) { return false; }
        }

        const playAllSelectors = [
            'a.c-btn--primary.js-play-button',
            'button.c-btn--primary.js-play-button',
            'a.c-btn--primary',
            'button.c-btn--primary',
            'a.js-play-button',
            'button.js-play-button',
            '.c-hero__action button',
            '.c-hero__action a',
            'button[aria-label*="Play All"]',
            'a[aria-label*="Play All"]',
            'button[aria-label*="Play"]',
            'a[aria-label*="Play"]',
            '#album_play',
            '[data-testid="play-all-btn"]',
            '[data-testid="play-btn"]'
        ];

        for (let sel of playAllSelectors) {
            let elems = document.querySelectorAll(sel);
            for (let el of elems) {
                let text = (el.innerText || el.textContent || '').trim().toLowerCase();
                if (text.includes('new playlist') || text.includes('create') || text.includes('log in') || text.includes('sign up')) continue;
                if (el && (el.offsetParent !== null || el.offsetWidth > 0)) {
                    if (clickEl(el)) {
                        return { success: true, clicked: true, selector: sel };
                    }
                }
            }
        }
        return { success: false, clicked: false };
        """

        for _ in range(4):
            play_res = driver.execute_script(js_play_all)
            if play_res and play_res.get("clicked"):
                logger.success(self.MODULE_NAME, f"✅ Clicked Play All button ({play_res.get('selector')})")
                return True
            time.sleep(0.5)

        logger.warn(self.MODULE_NAME, "Could not click Play All button on playlist page.")
        return False

    def load_first_trending_playlist(self, start_playback=True) -> bool:
        """
        Finds the first playlist in 'Trending Now' / 'Top Trending' on JioSaavn,
        navigates into it, and queues the ENTIRE playlist using 'Play All'.
        Follows the exact implementation from JioSaavan/jiosaavn_controller.py.
        If start_playback is False, pauses playback immediately after queuing.
        """
        driver = self.get_or_create_driver(wait_if_starting=True, timeout=15.0)
        if not driver:
            logger.warn(self.MODULE_NAME, "No driver available for load_first_trending_playlist")
            return False

        if not self.focus_or_get_jiosaavn_tab():
            logger.info(self.MODULE_NAME, "Opening JioSaavn tab...")
            self.focus_or_open_tab(URL_JIOSAAVN)
            time.sleep(2.5)

        try:
            curr_url = (driver.current_url or "").lower()
            is_playlist_page = any(x in curr_url for x in ["/featured/", "/album/", "/playlist/", "/mix/"])
            if not is_playlist_page:
                if not self._navigate_to_trending_playlist(driver):
                    return False
            else:
                logger.info(self.MODULE_NAME, f"Already on playlist page ({driver.current_url}). Playing all tracks directly...")

            if not self._click_play_all(driver):
                return False

            time.sleep(0.8)
            if not start_playback:
                self.pause()
                logger.info(self.MODULE_NAME, "Queued entire playlist and paused playback.")
            else:
                logger.success(self.MODULE_NAME, "✅ Queued ENTIRE playlist and started playback.")
            return True

        except Exception as err:
            logger.error(self.MODULE_NAME, f"Error in load_first_trending_playlist: {err}")
            return False

    def ensure_queue_populated(self, wait_seconds: float = 0.5, start_playback: bool = True) -> bool:
        """
        Ensures the JioSaavn queue has songs loaded and starts playback cleanly.
        If empty, arms the queue with the first trending playlist.
        """
        if wait_seconds > 0:
            time.sleep(wait_seconds)

        if not self.is_queue_empty():
            return True

        return self.load_first_trending_playlist(start_playback=start_playback)

    def focus_or_open_tab(self, url=URL_JIOSAAVN):
        """
        Focuses an existing JioSaavn tab, or navigates current tab / opens url.
        Cross-platform.
        """
        driver = self.get_or_create_driver(wait_if_starting=True, timeout=15.0)
        if not driver:
            return False

        try:
            for handle in driver.window_handles:
                try:
                    driver.switch_to.window(handle)
                    if DOMAIN_JIOSAAVN in (driver.current_url or "").lower():
                        if url and (driver.current_url or "").rstrip('/') != url.rstrip('/'):
                            driver.get(url)
                        logger.info(self.MODULE_NAME, f"Focused JioSaavn tab: {driver.current_url}")
                        return True
                except Exception:
                    continue

            # If no JioSaavn tab exists, use first handle
            if driver.window_handles:
                driver.switch_to.window(driver.window_handles[0])
                driver.get(url)
                logger.info(self.MODULE_NAME, f"Opened {url} in active tab")
                return True

            return False
        except Exception as e:
            logger.error(self.MODULE_NAME, f"Error in focus_or_open_tab: {e}")
            return False

    def _resolve_jiosaavn_autocomplete(self, query):
        """
        Queries JioSaavn's official autocomplete API to resolve exact target URL.
        Returns tuple: (target_url, title, type_name) or (None, None, None)
        Network operation — OS independent.
        """
        import urllib.request
        import urllib.parse
        import json

        try:
            encoded_query = urllib.parse.quote(query.strip())
            api_url = f"{URL_JIOSAAVN}/api.php?__call=autocomplete.get&query={encoded_query}&_format=json&_marker=0&ctx=web6dot0"
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode('utf-8'))

            sections = [
                ('topquery', 'Top Match'),
                ('songs', 'Song'),
                ('albums', 'Album'),
                ('playlists', 'Playlist')
            ]
            for section, default_type in sections:
                sec_data = data.get(section)
                if isinstance(sec_data, dict):
                    items = sec_data.get('data')
                    if isinstance(items, list) and len(items) > 0:
                        item = items[0]
                        url = item.get('url', '').replace(PREFIX_HTTP, PREFIX_HTTPS)
                        if url:
                            return (url, item.get('title', query), item.get('type', default_type))

        except Exception as err:
            logger.debug(self.MODULE_NAME, f"Autocomplete API query error: {err}")

        return (None, None, None)

    def _search_via_in_page_input(self, driver, query: str):
        logger.info(self.MODULE_NAME, f"No direct URL match. Using in-page search bar for '{query}'...")
        js_in_page_search = """
        const query = arguments[0];
        const input = document.querySelector('input[type="search"], input[placeholder*="Search" i], header input, .c-header__search input');
        if (input) {
            input.focus();
            input.value = query;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
            return true;
        }
        return false;
        """
        searched = driver.execute_script(js_in_page_search, query)
        time.sleep(2.0)
        if searched:
            js_click_first_result = """
            const resultSelectors = [
                '.c-header__search-results a',
                '.c-search__result a',
                '[role="listbox"] [role="option"] a',
                '.o-snippet a',
                '.c-song a'
            ];
            for (let sel of resultSelectors) {
                let el = document.querySelector(sel);
                if (el && (el.offsetParent !== null || el.offsetWidth > 0)) {
                    el.click();
                    return true;
                }
            }
            return false;
            """
            driver.execute_script(js_click_first_result)
            time.sleep(2.0)

    def in_tab_search_and_play(self, query):
        """
        404-Proof In-Tab Autocomplete Search & Direct Play:
        1. Resolves exact target URL using JioSaavn Autocomplete API.
        2. Navigates directly to the song/album/playlist in the active JioSaavn tab.
        3. Auto-clicks the dedicated Play button (.js-play-button / [aria-label='Play']).
        4. Gracefully falls back to search page navigation if needed.
        Cross-platform: pure Selenium + network.
        """
        if not query or not str(query).strip():
            logger.warn(self.MODULE_NAME, "Empty search query received.")
            return False

        query = str(query).strip()

        # Ensure JioSaavn tab is active
        if not self.focus_or_get_jiosaavn_tab():
            if not self.focus_or_open_tab(URL_JIOSAAVN):
                logger.warn(self.MODULE_NAME, "Cannot perform search: No active JioSaavn tab found on port 9222.")
                return False

        driver = self.driver
        logger.info(self.MODULE_NAME, f"🔍 Resolving search query: '{query}'...")

        # Check queue status before search to decide whether to add-to-queue or start playback
        queue_status = self.check_queue_status()
        is_queue_empty = queue_status.get("is_empty", True)
        logger.info(self.MODULE_NAME, f"Pre-search queue status: is_empty={is_queue_empty}, current='{queue_status.get('current_title', '')}'")

        try:
            # Phase 1: Resolve target URL via Autocomplete API
            target_url, title, item_type = self._resolve_jiosaavn_autocomplete(query)

            if target_url:
                logger.success(self.MODULE_NAME, f"🎯 Resolved {item_type}: '{title}' -> {target_url}")
                driver.get(target_url)
                time.sleep(2.0)
            else:
                self._search_via_in_page_input(driver, query)

            # Phase 2: Start playback directly via the page's primary Play button
            logger.info(self.MODULE_NAME, f"Loading '{title or query}' into player and starting playback...")
            clicked = self._click_primary_play_button(description=f"'{title or query}'", retries=4)
            if not clicked:
                logger.info(self.MODULE_NAME, f"Primary play button not found, falling back to play/pause toggle for '{title or query}'...")
                clicked = self.click_play_pause_button()

            logger.success(self.MODULE_NAME, f"✅ Started playback for '{title or query}'")
            return clicked or True

        except Exception as e:
            logger.error(self.MODULE_NAME, f"Search & play execution error for '{query}': {e}")
            return False

    def _try_add_to_queue_via_track_row(self) -> bool:
        """
        On the current JioSaavn page (song/album/playlist detail), finds the first
        track row's 3-dots (more) menu or direct 'Add to Queue' button and clicks it.
        This adds the song to the existing queue without replacing it.
        """
        if not self.driver:
            return False

        # Phase 1: Direct button check (some layouts have direct Add to Queue button)
        js_direct_button = """
        const directSelectors = [
            'button[title*="Add to queue" i]',
            'a[title*="Add to queue" i]',
            'button[aria-label*="Add to queue" i]',
            'a[aria-label*="Add to queue" i]',
            '[data-action="add-to-queue"]',
            '.c-card__btn-queue',
            '.c-btn--queue'
        ];
        for (let sel of directSelectors) {
            let el = document.querySelector(sel);
            if (el && (el.offsetParent !== null || el.offsetWidth > 0)) {
                try { el.focus(); } catch(e) {}
                try { el.click(); return { success: true, method: 'direct_button' }; } catch(e) {}
                try {
                    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    return { success: true, method: 'direct_button_event' };
                } catch(e2) {}
            }
        }
        return { success: false };
        """
        try:
            res = self.driver.execute_script(js_direct_button)
            if res and res.get("success"):
                logger.success(self.MODULE_NAME, "✅ Added to queue via direct button")
                return True
        except Exception as e:
            logger.debug(self.MODULE_NAME, f"Direct queue button error: {e}")

        # Phase 2: Open 3-dots menu on first track row or hero
        js_open_more = """
        function clickEl(el) {
            if (!el) return false;
            try { el.focus(); } catch(e) {}
            try { el.click(); return true; } catch(e) {}
            try {
                el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                return true;
            } catch(e2) { return false; }
        }

        const rowSelectors = [
            '.c-list__item',
            'figure.o-snippet',
            'article.o-snippet',
            '.c-song',
            'tr[data-testid]',
            '.o-snippet'
        ];
        const allRows = document.querySelectorAll(rowSelectors.join(', '));
        for (let row of allRows) {
            if (row.offsetWidth === 0 && row.offsetHeight === 0 && row.offsetParent === null) continue;

            try {
                row.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                row.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
            } catch(e) {}

            const moreSelectors = [
                '.o-icon-more',
                'button[aria-label*="More" i]',
                'button[title*="More" i]',
                '.c-btn--more',
                'button.c-btn--more',
                '[data-testid="more-button"]',
                '.c-song__more',
                '.o-snippet__more',
                'button[aria-label*="options" i]'
            ];

            for (let mSel of moreSelectors) {
                let moreBtn = row.querySelector(mSel);
                if (moreBtn && (moreBtn.offsetParent !== null || moreBtn.offsetWidth > 0)) {
                    if (clickEl(moreBtn)) {
                        return { success: true, source: 'row_more' };
                    }
                }
            }
        }

        // Also check hero actions 3-dots
        const heroMoreSelectors = [
            '.c-hero__actions .o-icon-more',
            '.c-hero__actions button[aria-label*="More" i]',
            '.c-hero button[aria-label*="More" i]',
            '.c-hero .o-icon-more'
        ];
        for (let sel of heroMoreSelectors) {
            let heroMore = document.querySelector(sel);
            if (heroMore && (heroMore.offsetParent !== null || heroMore.offsetWidth > 0)) {
                if (clickEl(heroMore)) {
                    return { success: true, source: 'hero_more' };
                }
            }
        }

        return { success: false };
        """

        js_click_queue_option = """
        function clickEl(el) {
            if (!el) return false;
            try { el.focus(); } catch(e) {}
            try { el.click(); return true; } catch(e) {}
            try {
                el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                return true;
            } catch(e2) { return false; }
        }

        const menuSelectors = [
            '.c-dropdown__item',
            '[role="menuitem"]',
            '.o-menu__item',
            '.c-menu__item',
            'div[role="dialog"] button',
            'div[role="dialog"] a',
            '.c-dropdown a',
            '.o-menu a',
            'li button',
            'li a'
        ];
        const menuItems = document.querySelectorAll(menuSelectors.join(', '));
        for (let item of menuItems) {
            const txt = (item.innerText || item.textContent || '').trim().toLowerCase();
            if (txt.includes('add to queue') || txt.includes('play next') || txt === 'queue') {
                if (clickEl(item)) {
                    return { success: true, text: txt };
                }
            }
        }
        return { success: false, menuCount: menuItems.length };
        """

        for attempt in range(3):
            try:
                opened = self.driver.execute_script(js_open_more)
                if opened and opened.get("success"):
                    time.sleep(0.4)
                    clicked = self.driver.execute_script(js_click_queue_option)
                    if clicked and clicked.get("success"):
                        logger.success(self.MODULE_NAME, f"✅ Added to queue via menu: '{clicked.get('text', '')}'")
                        return True
            except Exception as e:
                logger.debug(self.MODULE_NAME, f"Add-to-queue attempt {attempt+1} error: {e}")
            time.sleep(0.6)

        return False

    def quit_driver(self):
        if self.driver is not None:
            self.driver = None
            logger.info(self.MODULE_NAME, "Detached driver reference.")

    def close_browser(self) -> bool:
        """
        Closes the active debug Chrome browser instance and cleans up processes.
        Gracefully closes tabs and terminates only the Chrome instance bound to DEBUG_PORT (9222).
        """
        logger.info(self.MODULE_NAME, "Closing opened Chrome browser session...")
        
    def _terminate_chrome_proc(self):
        if getattr(self, '_chrome_proc', None):
            try:
                self._chrome_proc.terminate()
                self._chrome_proc.poll()
            except Exception:
                pass
            self._chrome_proc = None

    def _close_driver_tabs(self):
        if self.driver is None:
            return
        try:
            for h in list(self.driver.window_handles):
                try:
                    self.driver.switch_to.window(h)
                    self.driver.close()
                except Exception:
                    pass
        except Exception as e:
            logger.debug(self.MODULE_NAME, f"Tab close notice: {e}")
        try:
            self.driver.quit()
        except Exception as e:
            logger.debug(self.MODULE_NAME, f"Driver quit notice: {e}")
        self.driver = None

    def _kill_debug_port_process(self):
        try:
            if self._system == "Windows":
                cmd = (
                    f"$p = (Get-NetTCPConnection -LocalPort {self.DEBUG_PORT} -ErrorAction SilentlyContinue).OwningProcess; "
                    f"if ($p) {{ $p | ForEach-Object {{ Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }} }}; "
                    f"$procs = Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*remote-debugging-port={self.DEBUG_PORT}*' }}; "
                    f"if ($procs) {{ $procs | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }} }}"
                )
                subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=3)
            elif self._system in ("Darwin", "Linux"):
                subprocess.run(["pkill", "-9", "-f", f"remote-debugging-port={self.DEBUG_PORT}"], capture_output=True, timeout=2)
        except Exception as e:
            logger.debug(self.MODULE_NAME, f"Process kill notice: {e}")

    def close_browser(self) -> bool:
        """
        Closes the active debug Chrome browser instance and cleans up processes.
        Gracefully closes tabs and terminates only the Chrome instance bound to DEBUG_PORT (9222).
        """
        logger.info(self.MODULE_NAME, "Closing opened Chrome browser session...")
        self._terminate_chrome_proc()
        self._close_driver_tabs()
        self._kill_debug_port_process()
        self._last_launch_attempt = 0.0
        logger.success(self.MODULE_NAME, "✅ Opened Chrome browser closed successfully.")
        return True

    # ------------------------------------------------------------------
    # Diagnostics (useful for UI / health checks)
    # ------------------------------------------------------------------
    def get_diagnostics(self) -> dict:
        return {
            "os": self._system,
            "port": self.DEBUG_PORT,
            "port_open": self._is_port_open(),
            "debug_process": self._is_debug_chrome_process_running(),
            "driver_attached": self.driver is not None,
        }
