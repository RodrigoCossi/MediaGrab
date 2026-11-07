"""Per-product settings and download history, stored as JSON under ~/.config/mediagrab/."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mediagrab"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mediagrab"

VIDEO_QUALITIES = [4320, 2160, 1440, 1080, 720, 480, 360, 240, 144]
AUDIO_FORMATS = ["mp3", "m4a", "ogg", "opus", "flac", "wav"]
AUDIO_BITRATES = ["320", "256", "192", "160", "128"]
COOKIE_BROWSERS = ["auto", "", "firefox", "chrome", "chromium", "brave", "edge", "opera", "vivaldi"]


@dataclass
class Settings:
    product: str = "video"
    download_dir: str = str(Path.home() / "Downloads" / "MediaGrab")
    max_parallel: int = 3
    # "Smart Mode": skip the options dialog and reuse these values for every link.
    smart_mode: bool = False
    video_quality: int = 1080           # max height
    video_container: str = "mp4"        # mp4 | mkv | mp3 | m4a (audio only)
    subtitles: bool = False
    subtitle_lang: str = "en"
    audio_format: str = "mp3"
    audio_bitrate: str = "320"
    embed_cover: bool = True
    # Sites that refuse anonymous visitors (YouTube on server IPs, Vimeo) need a login.
    cookies_browser: str = "auto"      # one of COOKIE_BROWSERS; "auto" = newest browser login, "" = none
    cookies_file: str = ""             # Netscape cookies.txt, used when no browser is chosen
    pot_server: str = ""               # optional bgutil PO-token server, e.g. http://127.0.0.1:4416
    history: list = field(default_factory=list)

    @property
    def path(self) -> Path:
        return CONFIG_DIR / f"{self.product}.json"

    @classmethod
    def load(cls, product: str) -> "Settings":
        s = cls(product=product)
        try:
            data = json.loads(s.path.read_text())
        except (OSError, ValueError):
            data = {}
        for k, v in data.items():
            if hasattr(s, k) and k != "product":
                setattr(s, k, v)
        if product == "audio" and "download_dir" not in data:
            s.download_dir = str(Path.home() / "Music" / "MediaGrab")
        return s

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(self.path)
