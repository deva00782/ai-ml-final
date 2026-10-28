"""
Cross-platform OS operations abstraction module.
Provides unified interface for system volume control, media keys, and media status polling
across Windows, macOS, and Linux.

All classes are imported on any OS — actual system calls are guarded so the
module can be imported on any platform for testing / diagnostics.
"""

import platform
import subprocess
import time
import shutil
import asyncio
import logging
from logger import logger

# Silence noisy COM debug logs from comtypes
logging.getLogger("comtypes").setLevel(logging.WARNING)

# Log winrt ImportError only once so the 1s media-status poller doesn't spam.
_winrt_missing_logged = False

CMD_VOLUME_UP = "Volume Up"
CMD_VOLUME_DOWN = "Volume Down"
CMD_NEXT_TRACK = "Next Track"
CMD_PREVIOUS_TRACK = "Previous Track"
CMD_PLAY_PAUSE = "Play / Pause"

STATUS_NOT_SUPPORTED = "Not Supported"
STATUS_WAITING_FOR_TRACK = "Waiting for Track..."
STATUS_UNKNOWN_ARTIST = "Unknown Artist"

DBUS_TYPE_METHOD_CALL = "--type=method_call"
MPRIS_PATH = "/org/mpris/MediaPlayer2"


class OSOperations:
    """Base class for OS-specific operations"""
    
    def __init__(self):
        self.system = platform.system()
        logger.info("OSOperations", f"Initialized for {self.system}")
    
    def get_system_volume(self):
        """Get current system volume percentage (0-100)"""
        raise NotImplementedError("Subclasses must implement get_system_volume")
    
    def set_system_volume(self, volume_pct):
        """Set system volume percentage (0-100). Returns actual volume set."""
        raise NotImplementedError("Subclasses must implement set_system_volume")
    
    def send_media_key(self, key_name):
        """Send media key command."""
        raise NotImplementedError("Subclasses must implement send_media_key")
    
    async def get_media_status(self):
        """Get current media status (title, artist, is_playing). Returns dict."""
        raise NotImplementedError("Subclasses must implement get_media_status")


class WindowsOperations(OSOperations):
    """Windows-specific operations using PyCaw/WinRT with ctypes fallbacks"""
    
    def get_system_volume(self):
        if self.system != "Windows":
            return 100
        # Try PyCaw first
        try:
            import comtypes
            comtypes.CoInitialize()
            from pycaw.pycaw import AudioUtilities
            spk = AudioUtilities.GetSpeakers()
            return round(spk.EndpointVolume.GetMasterVolumeLevelScalar() * 100)
        except ImportError:
            logger.debug("WindowsOperations", "pycaw/comtypes not installed — trying winmm fallback")
        except Exception as e:
            logger.warn("WindowsOperations", f"PyCaw get volume failed: {e} — trying fallback")

        # Fallback: winmm waveOutGetVolume via ctypes
        try:
            import ctypes
            winmm = ctypes.windll.winmm
            vol = ctypes.c_uint()
            # waveOutGetVolume(0, &vol) — low word is left, high is right
            if winmm.waveOutGetVolume(0, ctypes.byref(vol)) == 0:
                left = vol.value & 0xFFFF
                # 0xFFFF == max
                return round((left / 0xFFFF) * 100)
        except Exception as e:
            logger.warn("WindowsOperations", f"winmm fallback failed: {e}")
        return 50  # unknown, return mid-level
    
    def set_system_volume(self, volume_pct):
        if self.system != "Windows":
            return volume_pct
        try:
            import comtypes
            comtypes.CoInitialize()
            from pycaw.pycaw import AudioUtilities
            spk = AudioUtilities.GetSpeakers()
            scalar = max(0.0, min(1.0, float(volume_pct) / 100.0))
            spk.EndpointVolume.SetMasterVolumeLevelScalar(scalar, None)
            actual = round(spk.EndpointVolume.GetMasterVolumeLevelScalar() * 100)
            logger.success("WindowsOperations", f"System volume set to {actual}%")
            return actual
        except ImportError:
            logger.debug("WindowsOperations", "pycaw/comtypes not installed — trying winmm/nircmd fallback")
        except Exception as e:
            logger.warn("WindowsOperations", f"PyCaw set volume failed: {e} — trying fallback")

        # Fallback: try nircmd if present (common on Windows)
        try:
            nircmd = shutil.which("nircmd")
            if nircmd:
                vol = int(max(0, min(100, int(volume_pct))) * 65535 / 100)
                # nircmd setsysvolume expects 0-65535
                subprocess.run([nircmd, "setsysvolume", str(vol)], check=True, timeout=3)
                logger.success("WindowsOperations", f"System volume set via nircmd to {volume_pct}%")
                return int(volume_pct)
        except Exception as e:
            logger.debug("WindowsOperations", f"nircmd fallback failed: {e}")

        # Last resort: waveOutSetVolume
        try:
            import ctypes
            winmm = ctypes.windll.winmm
            v = int(max(0, min(100, int(volume_pct))) * 0xFFFF / 100)
            packed = (v & 0xFFFF) | ((v & 0xFFFF) << 16)
            winmm.waveOutSetVolume(0, packed)
            logger.success("WindowsOperations", f"System volume set via winmm to {volume_pct}%")
            return int(volume_pct)
        except Exception as e:
            logger.warn("WindowsOperations", f"All volume fallbacks failed: {e}")
            return volume_pct
    
    def send_media_key(self, key_name):
        if self.system != "Windows":
            return False
        try:
            import ctypes
            VK_CODES = {
                CMD_VOLUME_UP: 0xAF,
                CMD_VOLUME_DOWN: 0xAE,
                CMD_NEXT_TRACK: 0xB0,
                CMD_PREVIOUS_TRACK: 0xB1,
                CMD_PLAY_PAUSE: 0xB3
            }
            vk_code = VK_CODES.get(key_name)
            if vk_code:
                ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
                time.sleep(0.05)
                ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)
                logger.success("WindowsOperations", f"Sent media key: {key_name}")
                return True
        except Exception as e:
            logger.error("WindowsOperations", f"Failed to send media key {key_name}: {e}")
        # Fallback: try PowerShell SendKeys for media
        try:
            ps_map = {
                CMD_PLAY_PAUSE: "{MEDIA_PLAY_PAUSE}",
                CMD_NEXT_TRACK: "{MEDIA_NEXT_TRACK}",
                CMD_PREVIOUS_TRACK: "{MEDIA_PREV_TRACK}",
            }
            if key_name in ps_map:
                subprocess.run(["powershell", "-Command",
                    f"$w=New-Object -ComObject WScript.Shell; $w.SendKeys('{ps_map[key_name]}')"],
                    timeout=3, capture_output=True)
                logger.success("WindowsOperations", f"Sent media key via PowerShell: {key_name}")
                return True
        except Exception:
            pass
        return False
    
    async def get_media_status(self):
        if self.system != "Windows":
            return {"title": STATUS_NOT_SUPPORTED, "artist": "Unknown", "is_playing": False}
        
        try:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
            manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
            current_session = manager.get_current_session()
            
            if current_session:
                playback_info = current_session.get_playback_info()
                is_playing = playback_info.playback_status == 4
                media_props = await current_session.try_get_media_properties_async()
                
                return {
                    "title": media_props.title or STATUS_WAITING_FOR_TRACK,
                    "artist": media_props.artist or STATUS_UNKNOWN_ARTIST,
                    "is_playing": is_playing
                }
        except ImportError:
            global _winrt_missing_logged
            if not _winrt_missing_logged:
                _winrt_missing_logged = True
                logger.debug("WindowsOperations", "winrt not installed — media status unavailable (install winsdk)")
        except Exception as e:
            logger.warn("WindowsOperations", f"Failed to get media status: {e}")
        
        return {"title": STATUS_WAITING_FOR_TRACK, "artist": STATUS_UNKNOWN_ARTIST, "is_playing": False}


class MacOSOperations(OSOperations):
    """macOS-specific operations using AppleScript and subprocess"""
    
    def get_system_volume(self):
        if self.system != "Darwin":
            return 100
        try:
            script = 'output volume of (get volume settings)'
            result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=3)
            if result.returncode == 0 and result.stdout.strip().isdigit():
                return int(result.stdout.strip())
            return 100
        except Exception as e:
            logger.warn("MacOSOperations", f"Failed to get system volume: {e}")
            return 100
    
    def set_system_volume(self, volume_pct):
        if self.system != "Darwin":
            return volume_pct
        try:
            volume = max(0, min(100, int(volume_pct)))
            script = f'set volume output volume {volume}'
            subprocess.run(['osascript', '-e', script], check=True, timeout=3)
            logger.success("MacOSOperations", f"System volume set to {volume}%")
            return volume
        except Exception as e:
            logger.warn("MacOSOperations", f"Failed to set system volume: {e}")
            return volume_pct
    
    def send_media_key(self, key_name):
        if self.system != "Darwin":
            return False
        try:
            if key_name in [CMD_VOLUME_UP, CMD_VOLUME_DOWN]:
                logger.warn("MacOSOperations", "Volume keys should be handled via set_system_volume")
                return False
            
            key_commands = {
                CMD_PLAY_PAUSE: 'tell application "System Events" to key code 49',
                CMD_NEXT_TRACK: 'tell application "Music" to next track',
                CMD_PREVIOUS_TRACK: 'tell application "Music" to previous track',
            }
            if key_name in key_commands:
                cmd = key_commands[key_name]
                res = subprocess.run(['osascript', '-e', cmd], capture_output=True, timeout=2)
                if res.returncode == 0:
                    logger.success("MacOSOperations", f"Sent media command via AppleScript: {key_name}")
                    return True

            logger.warn("MacOSOperations", "⚠️  Browser media control requires Chrome remote debugging")
            logger.info("MacOSOperations", "💡 To enable JioSaavn control:")
            logger.info("MacOSOperations", "   1. Run: python desktop_dashboard/desktop_modules/start_chrome.py  (cross-platform)")
            logger.info("MacOSOperations", "      (legacy: ./start_chrome.sh on macOS/Linux)")
            logger.info("MacOSOperations", "   2. Navigate to JioSaavn in that Chrome instance")
            logger.info("MacOSOperations", "   3. Chrome remote debugging bypasses OS security restrictions")
            return False
            
        except Exception as e:
            logger.error("MacOSOperations", f"Failed to send media key {key_name}: {e}")
            return False
    
    async def get_media_status(self):
        if self.system != "Darwin":
            return {"title": STATUS_NOT_SUPPORTED, "artist": "Unknown", "is_playing": False}
        
        try:
            script = '''
            tell application "System Events"
                set isPlaying to false
                set trackTitle to "Waiting for Track..."
                set trackArtist to "Unknown Artist"
                
                if (name of processes) contains "Music" then
                    tell application "Music"
                        if player state is playing then
                            set isPlaying to true
                            set trackTitle to name of current track
                            set trackArtist to artist of current track
                        end if
                    end tell
                else if (name of processes) contains "Spotify" then
                    tell application "Spotify"
                        if player state is playing then
                            set isPlaying to true
                            set trackTitle to name of current track
                            set trackArtist to artist of current track
                        end if
                    end tell
                end if
                
                return {trackTitle, trackArtist, isPlaying}
            end tell
            '''
            
            proc = await asyncio.create_subprocess_exec(
                'osascript', '-e', script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                parts = stdout.decode('utf-8', errors='replace').strip().split(', ')
                if len(parts) >= 3:
                    return {
                        "title": parts[0].strip(),
                        "artist": parts[1].strip(),
                        "is_playing": parts[2].strip().lower() == 'true'
                    }
        except Exception as e:
            logger.warn("MacOSOperations", f"Failed to get media status: {e}")
        
        return {"title": STATUS_WAITING_FOR_TRACK, "artist": STATUS_UNKNOWN_ARTIST, "is_playing": False}


async def _async_exec(args, timeout=3):
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        if hasattr(asyncio, "timeout"):
            async with asyncio.timeout(timeout):
                stdout, _ = await proc.communicate()
        else:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode('utf-8', errors='replace')
    except Exception:
        return -1, ""


class LinuxOperations(OSOperations):
    """Linux-specific operations using pulseaudio/pipewire and dbus with graceful fallbacks"""
    
    def get_system_volume(self):
        if self.system != "Linux":
            return 100
        # Try pactl (PulseAudio / PipeWire)
        try:
            result = subprocess.run(
                ['pactl', 'get-sink-volume', '@DEFAULT_SINK@'],
                capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0:
                import re
                match = re.search(r'(\d{1,3})%', result.stdout[:256])
                if match:
                    return int(match.group(1))
        except FileNotFoundError:
            logger.debug("LinuxOperations", "pactl not found — trying amixer")
        except Exception as e:
            logger.debug("LinuxOperations", f"pactl failed: {e} — trying amixer")

        # Fallback: amixer
        try:
            result = subprocess.run(['amixer', 'get', 'Master'], capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                import re
                m = re.search(r'\[(\d{1,3})%\]', result.stdout[:256])
                if m:
                    return int(m.group(1))
        except FileNotFoundError:
            logger.debug("LinuxOperations", "amixer not found")
        except Exception as e:
            logger.warn("LinuxOperations", f"amixer failed: {e}")

        # Fallback: wpctl (WirePlumber)
        try:
            result = subprocess.run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'], capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                import re
                m = re.search(r'([0-9.]+)', result.stdout[:256])
                if m:
                    return int(float(m.group(1)) * 100)
        except Exception:
            pass

        logger.warn("LinuxOperations", "No volume backend found (pactl/amixer/wpctl missing), returning 50")
        return 50
    
    def set_system_volume(self, volume_pct):
        if self.system != "Linux":
            return volume_pct
        volume = max(0, min(100, int(volume_pct)))
        # Try pactl
        try:
            subprocess.run(
                ['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{volume}%'],
                check=True, timeout=3
            )
            logger.success("LinuxOperations", f"System volume set to {volume}% via pactl")
            return volume
        except FileNotFoundError:
            logger.debug("LinuxOperations", "pactl not found — trying amixer")
        except Exception as e:
            logger.debug("LinuxOperations", f"pactl set failed: {e} — trying amixer")
        
        # Fallback: amixer
        try:
            subprocess.run(['amixer', 'set', 'Master', f'{volume}%'], check=True, timeout=3,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.success("LinuxOperations", f"System volume set to {volume}% via amixer")
            return volume
        except FileNotFoundError:
            logger.debug("LinuxOperations", "amixer not found — trying wpctl")
        except Exception as e:
            logger.debug("LinuxOperations", f"amixer failed: {e} — trying wpctl")

        # Fallback: wpctl
        try:
            # wpctl expects 0.0-1.0
            subprocess.run(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', f'{volume/100:.2f}'],
                           check=True, timeout=3)
            logger.success("LinuxOperations", f"System volume set to {volume}% via wpctl")
            return volume
        except Exception as e:
            logger.warn("LinuxOperations", f"All volume backends failed: {e}")
            return volume_pct
    
    def _send_playerctl(self, key_name, mpris_map) -> bool:
        playerctl = shutil.which("playerctl")
        if playerctl and key_name in mpris_map:
            try:
                subprocess.run([playerctl, mpris_map[key_name]], check=True, timeout=3,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.success("LinuxOperations", f"Sent media key via playerctl: {key_name}")
                return True
            except Exception as e:
                logger.debug("LinuxOperations", f"playerctl failed for {key_name}: {e}")
        return False

    def _send_dbus_mpris(self, key_name, mpris_map) -> bool:
        if key_name not in mpris_map:
            return False
        try:
            r = subprocess.run(
                ['dbus-send', '--session', '--dest=org.freedesktop.DBus',
                 DBUS_TYPE_METHOD_CALL, '/org/freedesktop/DBus',
                 'org.freedesktop.DBus.ListNames'],
                capture_output=True, text=True, timeout=3
            )
            if r.returncode != 0:
                return False
            import re
            candidates = []
            for line in r.stdout.splitlines():
                if "org.mpris.MediaPlayer2." in line:
                    for m in re.finditer(r'"(org\.mpris\.MediaPlayer2\.[^"]+)"', line):
                        candidates.append(m.group(1))
            priority = ["chrome", "chromium", "firefox", "spotify", "vlc"]
            candidates.sort(key=lambda n: next((i for i, p in enumerate(priority) if p in n.lower()), 99))
            for player in candidates:
                try:
                    subprocess.run([
                        'dbus-send', '--session', '--dest', player,
                        DBUS_TYPE_METHOD_CALL, MPRIS_PATH,
                        f'org.mpris.MediaPlayer2.Player.{mpris_map[key_name]}'
                    ], check=True, timeout=2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    logger.success("LinuxOperations", f"Sent media key via dbus MPRIS ({player}): {key_name}")
                    return True
                except Exception:
                    continue
        except Exception as e:
            logger.debug("LinuxOperations", f"dbus MPRIS failed: {e}")
        return False

    def _send_xdotool_fallback(self, key_name, key_symbols) -> bool:
        key_sym = key_symbols.get(key_name)
        if not key_sym:
            return False
        for tool, argv in [
            ("xdotool", ["xdotool", "key", key_sym]),
            ("wtype", ["wtype", "-k", key_sym]),
            ("ydotool", ["ydotool", "key", key_sym]),
        ]:
            if shutil.which(tool):
                try:
                    subprocess.run(argv, check=True, timeout=3)
                    logger.success("LinuxOperations", f"Sent media key via {tool}: {key_name}")
                    return True
                except Exception as e:
                    logger.debug("LinuxOperations", f"{tool} failed for {key_name}: {e}")
        return False

    def send_media_key(self, key_name):
        if self.system != "Linux":
            return False
        key_symbols = {
            CMD_PLAY_PAUSE: "XF86AudioPlay",
            CMD_NEXT_TRACK: "XF86AudioNext",
            CMD_PREVIOUS_TRACK: "XF86AudioPrev",
            CMD_VOLUME_UP: "XF86AudioRaiseVolume",
            CMD_VOLUME_DOWN: "XF86AudioLowerVolume"
        }
        mpris_map = {
            CMD_PLAY_PAUSE: "PlayPause",
            CMD_NEXT_TRACK: "Next",
            CMD_PREVIOUS_TRACK: "Previous",
        }
        if self._send_playerctl(key_name, mpris_map):
            return True
        if self._send_dbus_mpris(key_name, mpris_map):
            return True
        if self._send_xdotool_fallback(key_name, key_symbols):
            return True

        logger.warn("LinuxOperations", f"No media-key backend found for '{key_name}'. Install playerctl (recommended) or xdotool/wtype.")
        return False
    
    async def _get_media_status_playerctl(self):
        playerctl = shutil.which("playerctl")
        if not playerctl:
            return None
        code, out = await _async_exec([playerctl, "metadata", "--format", "{{title}}|{{artist}}|{{status}}"], timeout=3)
        if code == 0 and out.strip():
            parts = out.strip().split("|")
            if len(parts) >= 3:
                return {
                    "title": parts[0].strip() or STATUS_WAITING_FOR_TRACK,
                    "artist": parts[1].strip() or STATUS_UNKNOWN_ARTIST,
                    "is_playing": parts[2].strip().lower() == "playing"
                }
            if parts and parts[0].strip():
                return {"title": parts[0].strip(), "artist": STATUS_UNKNOWN_ARTIST, "is_playing": True}
        return None

    async def _get_media_status_dbus(self):
        code, out = await _async_exec([
            'dbus-send', '--session', '--dest=org.freedesktop.DBus',
            DBUS_TYPE_METHOD_CALL, '/org/freedesktop/DBus',
            'org.freedesktop.DBus.ListNames'
        ], timeout=3)
        if code != 0 or not out:
            return None
        import re
        players = re.findall(r'"(org\.mpris\.MediaPlayer2\.[^"]+)"', out)
        priority = ["chrome", "chromium", "firefox", "spotify", "vlc", "mpv"]
        players.sort(key=lambda n: next((i for i, p in enumerate(priority) if p in n.lower()), 99))
        for player in players:
            code_st, out_st = await _async_exec([
                'dbus-send', '--session', '--dest', player,
                DBUS_TYPE_METHOD_CALL, MPRIS_PATH,
                'org.freedesktop.DBus.Properties.Get',
                'string:org.mpris.MediaPlayer2.Player', 'string:PlaybackStatus'
            ], timeout=2)
            is_playing = "Playing" in out_st if code_st == 0 else False

            code_meta, out_meta = await _async_exec([
                'dbus-send', '--session', '--dest', player,
                DBUS_TYPE_METHOD_CALL, MPRIS_PATH,
                'org.freedesktop.DBus.Properties.Get',
                'string:org.mpris.MediaPlayer2.Player', 'string:Metadata'
            ], timeout=2)
            if code_meta == 0 and "xesam:title" in out_meta:
                title_m = re.search(r'"xesam:title".*?string\s+"([^"]+)"', out_meta, re.DOTALL)
                artist_m = re.search(r'"xesam:artist".*?string\s+"([^"]+)"', out_meta, re.DOTALL)
                return {
                    "title": title_m.group(1) if title_m else "Linux Media",
                    "artist": artist_m.group(1) if artist_m else STATUS_UNKNOWN_ARTIST,
                    "is_playing": is_playing
                }
            elif code_meta == 0:
                return {"title": "Linux Media", "artist": STATUS_UNKNOWN_ARTIST, "is_playing": is_playing}
        return None

    async def get_media_status(self):
        if self.system != "Linux":
            return {"title": STATUS_NOT_SUPPORTED, "artist": "Unknown", "is_playing": False}
        
        status = await self._get_media_status_playerctl()
        if status:
            return status

        try:
            status = await self._get_media_status_dbus()
            if status:
                return status
        except Exception as e:
            logger.warn("LinuxOperations", f"Failed to get media status: {e}")
        
        return {"title": STATUS_WAITING_FOR_TRACK, "artist": STATUS_UNKNOWN_ARTIST, "is_playing": False}


def get_os_operations():
    """Factory function to get the appropriate OS operations instance"""
    system = platform.system()
    
    if system == "Windows":
        return WindowsOperations()
    elif system == "Darwin":
        return MacOSOperations()
    elif system == "Linux":
        return LinuxOperations()
    else:
        logger.warn("OSOperations", f"Unsupported platform: {system}, using basic fallback")
        return OSOperations()


# Global instance
os_ops = get_os_operations()


# Convenience functions that match the original API
def get_system_master_volume():
    return os_ops.get_system_volume()


def set_system_master_volume(volume_pct):
    return os_ops.set_system_volume(volume_pct)


def send_os_media_key(key_name):
    return os_ops.send_media_key(key_name)


async def get_media_status():
    return await os_ops.get_media_status()
