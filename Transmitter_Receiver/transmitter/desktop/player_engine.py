import time
import random
from logger import logger
from os_operations import send_os_media_key, get_system_master_volume, set_system_master_volume

try:
    from bci_pipeline import RANDOM_SONGS_POOL
except Exception:
    RANDOM_SONGS_POOL = [
        "Starboy", "Hukum Jailer", "Devara Chuttamalle", "Arijit Singh Sad Songs",
        "Naatu Naatu", "Beauty and a Beat", "Believer Imagine Dragons",
        "Shape of You Ed Sheeran", "Blinding Lights The Weeknd", "Kesariya Brahmastra"
    ]

class PlayerEngine:
    MODULE_NAME = "PlayerEngine"

    def __init__(self, browser_manager):
        self.browser_manager = browser_manager
        self._last_play_pause_time = 0.0
        self.song_card_index = 0

    def _send_os_media_key(self, action_name):
        return send_os_media_key(action_name)

    def play_random_track(self, exclude=""):
        """
        Picks and plays a random track from RANDOM_SONGS_POOL,
        excluding the track currently playing.
        """
        exclude_clean = str(exclude or "").strip().lower()
        candidates = [
            s for s in RANDOM_SONGS_POOL
            if exclude_clean not in s.lower() and s.lower() not in exclude_clean
        ]
        choice = random.choice(candidates if candidates else RANDOM_SONGS_POOL)
        logger.info(self.MODULE_NAME, f"🎲 Selected random fallback track: '{choice}' (Current: '{exclude}')")
        return self.search_and_play(choice)

    def play_pause(self):
        """
        Clean, synchronized Play/Pause following JioSaavan controller:
        1. If queue has songs: toggles between active playback pause and resume cleanly.
        2. If fresh launch / empty queue: navigates to first trending playlist, clicks Play All,
           and queues the entire playlist into JioSaavn.
        """
        logger.info(self.MODULE_NAME, "Executing Action: Play / Pause")

        # 1-second debounce to prevent accidental double-toggle flips
        now = time.time()
        if now - self._last_play_pause_time < 1.0:
            logger.info(self.MODULE_NAME, "Debouncing rapid duplicate Play/Pause request (ignored)")
            return True
        self._last_play_pause_time = now

        if not self.browser_manager.focus_or_get_jiosaavn_tab():
            logger.info(self.MODULE_NAME, "No active JioSaavn tab. Dispatching OS media key...")
            return self._send_os_media_key("Play / Pause")

        js_play_pause = """
        // Step 1: Determine if the QUEUE has songs (exact JioSaavan implementation)
        const audios = document.querySelectorAll('audio');
        let activeAudio = null;
        let isCurrentlyPlaying = false;

        for (let a of audios) {
            if ((a.src && a.src.length > 10 && !a.src.endsWith('/') && !a.src.endsWith('#')) ||
                (a.currentSrc && a.currentSrc.length > 10)) {
                if (a.duration > 0 || !a.paused || a.currentTime > 0) {
                    activeAudio = a;
                    if (!a.paused && a.currentTime > 0) {
                        isCurrentlyPlaying = true;
                    }
                    break;
                }
            }
        }

        const playerTitle = document.querySelector('#player .c-player__title') || 
                            document.querySelector('.c-player__title') || 
                            document.querySelector('#player a[href*="/song/"]') ||
                            document.querySelector('#player_song_name');
        const songName = playerTitle ? (playerTitle.innerText || playerTitle.textContent || '').trim() : '';
        const cleanName = songName.toLowerCase();
        const hasSongInPlayerBar = songName.length > 0 && 
                                   !cleanName.includes('welcome') && 
                                   !cleanName.includes('jiosaavn') &&
                                   !cleanName.includes('history') &&
                                   !cleanName.includes('home');

        const queueItems = document.querySelectorAll('.c-queue__item, .c-queue__list li, #player_queue_list li, [class*="queue"] li, [class*="queue"] .c-list__item');
        const hasQueueItems = queueItems.length > 0;

        const queueHasSongs = !!(activeAudio || (hasSongInPlayerBar && songName.length > 1) || hasQueueItems);

        const bottomBtn = document.querySelector('#player_play_pause') || 
                          document.querySelector('.c-player__btn--play') || 
                          document.querySelector('.c-player__btn--pause') || 
                          document.querySelector('.c-player__play') ||
                          document.querySelector('#player_play');

        // Step 2: Queue has songs -> toggle play / pause
        if (queueHasSongs) {
            if (isCurrentlyPlaying) {
                if (bottomBtn) { try { bottomBtn.click(); } catch(e) {} }
                if (activeAudio && !activeAudio.paused) { activeAudio.pause(); }
                return { success: true, mode: 'paused', is_playing: false };
            } else {
                if (bottomBtn) { try { bottomBtn.click(); } catch(e) {} }
                if (activeAudio && activeAudio.paused) { activeAudio.play().catch(e => {}); }
                return { success: true, mode: 'resumed', is_playing: true };
            }
        }

        return { success: false, mode: 'empty_queue' };
        """

        try:
            res = self.browser_manager.driver.execute_script(js_play_pause)
            if res and res.get("success"):
                logger.info(self.MODULE_NAME, f"Play/Pause executed via DOM: {res.get('mode')}")
                return True
            elif res and res.get("mode") == "empty_queue":
                logger.info(self.MODULE_NAME, "Queue is empty on Play/Pause. Loading first trending playlist...")
                return self.browser_manager.load_first_trending_playlist(start_playback=True)
        except Exception as e:
            logger.debug(self.MODULE_NAME, f"DOM Play/Pause notice: {e}")

        # Fallback: if queue empty, load first trending playlist
        if self.browser_manager.is_queue_empty():
            logger.info(self.MODULE_NAME, "Empty queue fallback. Loading first trending playlist...")
            return self.browser_manager.ensure_queue_populated(wait_seconds=0.5)

        return self._send_os_media_key("Play / Pause")

    def next_track(self):
        """
        Next Track following JioSaavan controller:
        1. If active and not disabled, clicks #player_next in bottom bar.
        2. Fallback: finds playable cards on current page and advances index.
        3. Fallback on empty queue: loads first trending playlist and starts playback.
        """
        logger.info(self.MODULE_NAME, "Executing Action: Next Track")
        self.song_card_index += 1

        if not self.browser_manager.focus_or_get_jiosaavn_tab():
            return self._send_os_media_key("Next Track")

        js_next = """
        const targetIdx = arguments[0];

        function clickElement(el) {
            if (!el) return false;
            try { el.focus(); } catch(e) {}
            try { el.click(); return true; } catch(e) {}
            try {
                const evt = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
                el.dispatchEvent(evt);
                return true;
            } catch(e2) {
                return false;
            }
        }

        // 1. Try bottom player bar next button if active & not disabled
        const nextSelectors = [
            '#player_next',
            '.c-player__btn--next',
            'button[aria-label*="Next"]',
            'button[title*="Next"]',
            'a[aria-label*="Next"]',
            '.o-icon-next',
            '[data-testid="player-next"]'
        ];
        for (let sel of nextSelectors) {
            let el = document.querySelector(sel);
            if (el && !el.disabled && !el.classList.contains('is-disabled') && !el.classList.contains('disabled') && (el.offsetParent !== null || el.offsetWidth > 0)) {
                if (clickElement(el)) {
                    return { success: true, mode: 'player_bar_next', selector: sel };
                }
            }
        }

        // 2. Fallback: Find playable tracks / cards on current page and advance index
        const playableSelectors = [
            '.o-snippet__action-final',
            '.c-song__play',
            '.o-icon-play-circle',
            'button.c-btn--primary.js-play-button',
            'a.c-btn--primary.js-play-button',
            '.js-play-button',
            'figure.o-snippet [aria-label*="Play"]',
            'article.o-snippet [aria-label*="Play"]',
            'figure.o-snippet',
            'article.o-snippet',
            '.c-card--song',
            '.c-song'
        ];

        let allPlayables = [];
        for (let sel of playableSelectors) {
            let found = document.querySelectorAll(sel);
            if (found.length > 0) {
                allPlayables = Array.from(found).filter(e => {
                    let text = (e.innerText || e.textContent || '').toLowerCase();
                    return !text.includes('new playlist') && !text.includes('create') && (e.offsetParent !== null || e.offsetWidth > 0);
                });
                if (allPlayables.length > 1) break;
            }
        }

        if (allPlayables.length > 0) {
            const targetCard = allPlayables[targetIdx % allPlayables.length];
            if (clickElement(targetCard)) {
                return { success: true, mode: 'card_skip_next', index: targetIdx % allPlayables.length };
            }
        }

        return { success: false, mode: 'empty_queue' };
        """

        try:
            res = self.browser_manager.driver.execute_script(js_next, self.song_card_index)
            if res and res.get("success"):
                mode = res.get("mode")
                if mode == "card_skip_next":
                    time.sleep(0.6)
                    self.browser_manager.driver.execute_script("""
                    const bottomBtn = document.querySelector('#player_play_pause') || document.querySelector('.c-player__btn--play');
                    if (bottomBtn) { try { bottomBtn.click(); } catch(e) {} }
                    """)
                logger.info(self.MODULE_NAME, f"Next Track executed via DOM: {mode}")
                return True
            elif not res or res.get("mode") == "empty_queue":
                logger.info(self.MODULE_NAME, "Queue is empty or next disabled. Loading first trending playlist...")
                return self.browser_manager.load_first_trending_playlist(start_playback=True)
        except Exception as e:
            logger.debug(self.MODULE_NAME, f"Next Track script error: {e}")

        if self.browser_manager.is_queue_empty():
            return self.browser_manager.ensure_queue_populated(wait_seconds=0.5)

        return self._send_os_media_key("Next Track")

    def previous_track(self):
        """
        Previous Track following JioSaavan controller:
        1. If active and not disabled, clicks #player_prev in bottom bar.
        2. Fallback: finds playable cards on current page and moves index backwards.
        3. Fallback on empty queue: loads first trending playlist and starts playback.
        """
        logger.info(self.MODULE_NAME, "Executing Action: Previous Track")
        self.song_card_index = max(0, self.song_card_index - 1)

        if not self.browser_manager.focus_or_get_jiosaavn_tab():
            return self._send_os_media_key("Previous Track")

        js_prev = """
        const targetIdx = arguments[0];

        function clickElement(el) {
            if (!el) return false;
            try { el.focus(); } catch(e) {}
            try { el.click(); return true; } catch(e) {}
            try {
                const evt = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
                el.dispatchEvent(evt);
                return true;
            } catch(e2) {
                return false;
            }
        }

        // 1. Try bottom player bar prev button if active & not disabled
        const prevSelectors = [
            '#player_prev',
            '.c-player__btn--prev',
            'button[aria-label*="Previous"]',
            'button[title*="Previous"]',
            'a[aria-label*="Previous"]',
            '.o-icon-prev',
            '[data-testid="player-prev"]'
        ];
        for (let sel of prevSelectors) {
            let el = document.querySelector(sel);
            if (el && !el.disabled && !el.classList.contains('is-disabled') && !el.classList.contains('disabled') && (el.offsetParent !== null || el.offsetWidth > 0)) {
                if (clickElement(el)) {
                    return { success: true, mode: 'player_bar_prev', selector: sel };
                }
            }
        }

        // 2. Fallback: Find playable tracks / cards on current page and move index backwards
        const playableSelectors = [
            '.o-snippet__action-final',
            '.c-song__play',
            '.o-icon-play-circle',
            'button.c-btn--primary.js-play-button',
            'a.c-btn--primary.js-play-button',
            '.js-play-button',
            'figure.o-snippet [aria-label*="Play"]',
            'article.o-snippet [aria-label*="Play"]',
            'figure.o-snippet',
            'article.o-snippet',
            '.c-card--song',
            '.c-song'
        ];

        let allPlayables = [];
        for (let sel of playableSelectors) {
            let found = document.querySelectorAll(sel);
            if (found.length > 0) {
                allPlayables = Array.from(found).filter(e => {
                    let text = (e.innerText || e.textContent || '').toLowerCase();
                    return !text.includes('new playlist') && !text.includes('create') && (e.offsetParent !== null || e.offsetWidth > 0);
                });
                if (allPlayables.length > 1) break;
            }
        }

        if (allPlayables.length > 0) {
            const targetCard = allPlayables[targetIdx % allPlayables.length];
            if (clickElement(targetCard)) {
                return { success: true, mode: 'card_skip_prev', index: targetIdx % allPlayables.length };
            }
        }

        return { success: false, mode: 'empty_queue' };
        """

        try:
            res = self.browser_manager.driver.execute_script(js_prev, self.song_card_index)
            if res and res.get("success"):
                mode = res.get("mode")
                if mode == "card_skip_prev":
                    time.sleep(0.6)
                    self.browser_manager.driver.execute_script("""
                    const bottomBtn = document.querySelector('#player_play_pause') || document.querySelector('.c-player__btn--play');
                    if (bottomBtn) { try { bottomBtn.click(); } catch(e) {} }
                    """)
                logger.info(self.MODULE_NAME, f"Previous Track executed via DOM: {mode}")
                return True
            elif not res or res.get("mode") == "empty_queue":
                logger.info(self.MODULE_NAME, "Queue is empty or prev disabled. Loading first trending playlist...")
                return self.browser_manager.load_first_trending_playlist(start_playback=True)
        except Exception as e:
            logger.debug(self.MODULE_NAME, f"Previous Track script error: {e}")

        if self.browser_manager.is_queue_empty():
            return self.browser_manager.ensure_queue_populated(wait_seconds=0.5)

        return self._send_os_media_key("Previous Track")

    def adjust_volume(self, action_name):
        logger.info(self.MODULE_NAME, f"Executing Windows Master Volume action: {action_name}")
        try:
            import ctypes
            user32 = ctypes.windll.user32
            VK_VOLUME_UP = 0xAF
            VK_VOLUME_DOWN = 0xAE
            KEYEVENTF_EXTENDEDKEY = 0x0001
            KEYEVENTF_KEYUP = 0x0002

            vk = VK_VOLUME_UP if action_name == 'Volume Up' else VK_VOLUME_DOWN
            scan_code = user32.MapVirtualKeyW(vk, 0)

            # Send 3 hardware key steps (each step adjusts volume by 2% in Windows, total ~6%)
            for _ in range(3):
                user32.keybd_event(vk, scan_code, KEYEVENTF_EXTENDEDKEY, 0)
                time.sleep(0.02)
                user32.keybd_event(vk, scan_code, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
                time.sleep(0.02)

            logger.success(self.MODULE_NAME, f"✅ Windows master volume adjusted via native hardware key: {action_name}")
            return True
        except Exception as err:
            logger.warn(self.MODULE_NAME, f"Windows master volume adjustment failed: {err}")
            return False

    def search_and_play(self, song_name):
        logger.info(self.MODULE_NAME, f"Executing In-Tab Search & Play for: '{song_name}'")
        return self.browser_manager.in_tab_search_and_play(song_name)
