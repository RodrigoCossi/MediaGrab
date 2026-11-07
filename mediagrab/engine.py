"""Download / conversion engine. Qt-free so it can be tested from the command line.

Every long call takes `on_progress(dict)` and `should_stop() -> bool`; stopping raises Cancelled.
A stopped yt-dlp download leaves its .part file, and the next run resumes from it (Pause/Resume).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import yt_dlp

from .config import CACHE_DIR, Settings

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Cancelled(Exception):
    """`partials` = files a stopped download left (kept for Resume; removed via remove_partials)."""
    def __init__(self, partials=()):
        super().__init__("cancelled")
        self.partials = sorted(set(partials))


def remove_partials(paths) -> None:
    """Delete what a removed (not paused) download left: .part/.ytdl/fragments and unmerged streams."""
    for p in paths or ():
        p = Path(p)
        for f in [p, Path(str(p) + ".part"), Path(str(p) + ".ytdl"), *p.parent.glob(p.name + ".part-Frag*")]:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            if p.parent.name == ".mediagrab-parts":
                p.parent.rmdir()   # only succeeds when empty
        except OSError:
            pass


class EngineError(Exception):
    """An error with a message meant for the user."""


# ---------------------------------------------------------------- helpers

def friendly_error(raw: str) -> str:
    msg = ANSI.sub("", str(raw)).replace("ERROR: ", "").strip()
    low = msg.lower()
    if "confirm you" in low and "not a bot" in low:
        return "YouTube asks this computer to sign in. Choose a login in Settings → Login, then retry."
    if "only works when logged-in" in low or "login required" in low or "--cookies" in low:
        return "This site needs a login. Open Settings → Login and choose your browser, then retry."
    if "unsupported url" in low:
        return "This link isn't supported. Check that it points to a video page."
    if "private video" in low:
        return "This video is private."
    if "video unavailable" in low or "not available" in low:
        return "This video is unavailable (removed, private or blocked in this country)."
    if "429" in low or "too many requests" in low:
        return "The site is rate-limiting this computer (HTTP 429). Wait a while or sign in (Settings → Login)."
    if "timed out" in low or "name or service not known" in low or "network is unreachable" in low:
        return "Network problem: the site didn't answer. Check the connection and retry."
    m = re.sub(r"^\[[^\]]+\]\s*[\w-]+:\s*", "", msg)
    return m.split(" See  https://")[0].split(" See https://")[0][:300]


class _Log:
    def __init__(self):
        self.errors: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.errors.append(ANSI.sub("", msg))


def base_opts(s: Settings, log: _Log) -> dict:
    o = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": log,
        "noplaylist": True,
        "js_runtimes": {"node": {}} if shutil.which("node") else {},
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
        "continuedl": True,
        "windowsfilenames": False,
        "trim_file_name": 180,
    }
    browser = resolve_browser(s)
    if browser:
        o["cookiesfrombrowser"] = (browser, _firefox_snapshot() if browser == "firefox" else None,
                                   None, None)
    elif s.cookies_file and Path(s.cookies_file).is_file():  # also used by "auto" when set
        o["cookiefile"] = s.cookies_file
    if s.pot_server:
        o["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [s.pot_server]}}
    return o


HOME = Path.home()
BROWSER_STORES = {   # where each browser keeps its cookie database (any profile)
    "firefox": ["~/.mozilla/firefox/*/cookies.sqlite", "~/snap/firefox/common/.mozilla/firefox/*/cookies.sqlite"],
    "chrome": ["~/.config/google-chrome/*/Cookies", "~/.config/google-chrome/*/Network/Cookies"],
    "chromium": ["~/.config/chromium/*/Cookies", "~/.config/chromium/*/Network/Cookies"],
    "brave": ["~/.config/BraveSoftware/Brave-Browser/*/Cookies",
              "~/.config/BraveSoftware/Brave-Browser/*/Network/Cookies"],
    "edge": ["~/.config/microsoft-edge/*/Cookies", "~/.config/microsoft-edge/*/Network/Cookies"],
    "opera": ["~/.config/opera/Cookies", "~/.config/opera/Network/Cookies"],
    "vivaldi": ["~/.config/vivaldi/*/Cookies", "~/.config/vivaldi/*/Network/Cookies"],
}


def _firefox_snapshot() -> str | None:
    """Firefox keeps a fresh login in cookies.sqlite-wal until it checkpoints; yt-dlp copies only
    cookies.sqlite, so right after signing in it sends the old cookies and YouTube refuses.
    Copy db + -wal + -shm of the newest profile, merge them, and point yt-dlp at that copy."""
    import sqlite3, tempfile
    dbs = []
    for pat in BROWSER_STORES["firefox"]:
        base = Path(pat.replace("~", str(HOME), 1))
        dbs += list(Path(base.anchor).glob(str(base.relative_to(base.anchor))))
    if not dbs:
        return None
    src = max(dbs, key=lambda f: max(f.stat().st_mtime,
                                     *(Path(str(f) + x).stat().st_mtime for x in ("-wal",)
                                       if Path(str(f) + x).exists())))
    root = CACHE_DIR / "ff-cookies"
    root.mkdir(parents=True, exist_ok=True)
    for old in root.iterdir():   # copies are only read while a download starts
        if time.time() - old.stat().st_mtime > 3600:
            shutil.rmtree(old, ignore_errors=True)
    dst = Path(tempfile.mkdtemp(dir=root))
    try:
        for x in ("", "-wal", "-shm"):
            if Path(str(src) + x).exists():
                shutil.copy2(str(src) + x, dst / ("cookies.sqlite" + x))
        con = sqlite3.connect(dst / "cookies.sqlite")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        return str(dst)
    except Exception:
        shutil.rmtree(dst, ignore_errors=True)
        return None   # fall back to yt-dlp's own lookup


def detect_browsers() -> list[tuple[str, float]]:
    """Browsers that have a cookie database here, most recently used first."""
    found = {}
    for name, pats in BROWSER_STORES.items():
        for pat in pats:
            base = Path(pat.replace("~", str(HOME), 1))
            for f in Path(base.anchor).glob(str(base.relative_to(base.anchor))):
                try:
                    found[name] = max(found.get(name, 0), f.stat().st_mtime)
                except OSError:
                    pass
    return sorted(found.items(), key=lambda kv: kv[1], reverse=True)


def resolve_browser(s: Settings) -> str | None:
    """'auto' = the browser whose login was used most recently (that's where the user is signed in)."""
    if s.cookies_browser == "auto":
        if s.cookies_file and Path(s.cookies_file).is_file():
            return None
        found = detect_browsers()
        return found[0][0] if found else None
    return s.cookies_browser or None


TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"   # "Me at the zoo", 19 s, public since 2005


def test_login(s: Settings) -> tuple[bool, str]:
    """Ask YouTube for one video's details using the current login settings."""
    via = resolve_browser(s)
    how = (f"your {via.capitalize()} login" if via else
           "the cookies file" if s.cookies_file else "no login")
    try:
        probe(TEST_URL, s)
    except EngineError as e:
        return False, f"YouTube refused ({how}): {e}"
    return True, f"Working: YouTube accepts {how}."


def site_name(info: dict) -> str:
    key = info.get("extractor_key") or info.get("ie_key") or info.get("extractor") or ""
    key = key.split(":")[0]
    return {"Youtube": "YouTube", "YoutubeTab": "YouTube", "Soundcloud": "SoundCloud",
            "SoundcloudPlaylist": "SoundCloud", "SoundcloudSet": "SoundCloud", "Twitter": "X / Twitter",
            "TikTok": "TikTok", "Vimeo": "Vimeo", "Instagram": "Instagram", "Facebook": "Facebook",
            "Dailymotion": "Dailymotion", "Twitch": "Twitch", "TwitchClips": "Twitch",
            "Reddit": "Reddit", "Bandcamp": "Bandcamp", "Generic": "Web"}.get(key, key or "Web")


def fetch_thumbnail(url: str | None) -> str | None:
    """Download a thumbnail into the cache; returns the local path."""
    if not url:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / ("t_" + hashlib.sha1(url.encode()).hexdigest()[:20])
    if dest.exists() and dest.stat().st_size:
        return str(dest)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(5_000_000)
        dest.write_bytes(data)
        return str(dest)
    except Exception:
        return None


def _fmt_size(f: dict) -> int:
    return int(f.get("filesize") or f.get("filesize_approx") or 0)


# ---------------------------------------------------------------- analyse a link

def probe(url: str, s: Settings) -> dict:
    """Look at a link without downloading. Returns a summary for the options dialog."""
    log = _Log()
    opts = base_opts(s, log) | {"extract_flat": "in_playlist", "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise EngineError(friendly_error(log.errors[-1] if log.errors else e)) from None
    if not info:
        raise EngineError(friendly_error(log.errors[-1] if log.errors else "Nothing found at this link."))

    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        entries = []
        for e in info.get("entries") or []:
            if not e:
                continue
            eu = e.get("url") or e.get("webpage_url")
            if e.get("ie_key") == "Youtube" and e.get("id") and not str(eu).startswith("http"):
                eu = f"https://www.youtube.com/watch?v={e['id']}"
            thumbs = e.get("thumbnails") or []
            entries.append({
                "url": eu, "title": e.get("title") or e.get("id") or "Untitled",
                "duration": e.get("duration"),
                "thumbnail": e.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None),
            })
        return {"kind": "playlist", "title": info.get("title") or "Playlist",
                "uploader": info.get("uploader") or info.get("channel") or "",
                "site": site_name(info), "url": info.get("webpage_url") or url,
                "thumbnail": info.get("thumbnail") or (entries[0]["thumbnail"] if entries else None),
                "entries": entries}

    formats = info.get("formats") or [info]
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max(audio, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    sizes: dict[int, int] = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") == "none":
            continue
        size = _fmt_size(f)
        if f.get("acodec") in (None, "none") and best_audio:
            size += _fmt_size(best_audio)
        sizes[h] = max(sizes.get(h, 0), size)
    heights = sorted(sizes, reverse=True)
    subs = sorted(set(info.get("subtitles") or {}) | set(
        k for k in (info.get("automatic_captions") or {}) if len(k) <= 3))
    return {
        "kind": "video", "title": info.get("title") or "Untitled", "url": info.get("webpage_url") or url,
        "uploader": info.get("uploader") or info.get("channel") or "", "site": site_name(info),
        "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
        "heights": heights, "sizes": sizes,
        "audio_size": _fmt_size(best_audio) if best_audio else 0,
        "has_video": bool(heights) or (info.get("vcodec") not in (None, "none")),
        "subtitles": subs[:200],
    }


# ---------------------------------------------------------------- download

PP_LABELS = {"Merger": "Merging video + audio", "FFmpegMerger": "Merging video + audio",
             "ExtractAudio": "Converting audio", "FFmpegExtractAudio": "Converting audio",
             "EmbedThumbnail": "Adding cover art", "FFmpegMetadata": "Writing tags",
             "FFmpegVideoConvertor": "Converting video", "FFmpegVideoRemuxer": "Remuxing",
             "FFmpegSubtitlesConvertor": "Converting subtitles", "MoveFiles": "Finishing"}


def build_format_opts(job: dict) -> dict:
    """job keys: mode ('video'|'audio'), quality (height), container, audio_format, bitrate,
    subtitles, sub_lang, embed_cover."""
    o: dict = {"postprocessors": []}
    if job["mode"] == "audio":
        fmt = job.get("audio_format", "mp3")
        o["format"] = "ba/b"
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
        if fmt not in ("flac", "wav"):
            pp["preferredquality"] = str(job.get("bitrate", "320"))
        o["postprocessors"].append(pp)
        o["postprocessors"].append({"key": "FFmpegMetadata", "add_metadata": True})
        if job.get("embed_cover", True) and fmt in ("mp3", "m4a", "flac", "ogg", "opus"):
            o["writethumbnail"] = True
            o["postprocessors"].append({"key": "FFmpegThumbnailsConvertor", "format": "jpg",
                                        "when": "before_dl"})
            o["postprocessors"].append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    else:
        h = int(job.get("quality") or 0) or 100000   # 0 = "Original quality": no height cap
        cont = job.get("container", "mp4")
        if cont == "mp4":
            # Prefer MP4-native streams (H.264/AV1 + AAC) at the chosen height; fall back to anything.
            o["format"] = f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
            o["format_sort"] = [f"res:{h}", "fps", "vcodec:avc1" if h <= 1080 else "res", "acodec:m4a"]
        else:
            o["format"] = f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
            o["format_sort"] = [f"res:{h}", "fps"]
        o["merge_output_format"] = cont
        if job.get("subtitles"):
            o["writesubtitles"] = True
            o["writeautomaticsub"] = True
            o["subtitleslangs"] = [job.get("sub_lang") or "en"]
            o["postprocessors"].append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})
    return o


def _abs(files, folder: Path) -> list[str]:
    return [str(f if Path(f).is_absolute() else folder / f) for f in files]


def download(job: dict, s: Settings, on_progress, should_stop) -> str:
    """Download one video/track. Returns the final file path."""
    log = _Log()
    folder = Path(job.get("folder") or s.download_dir)
    folder.mkdir(parents=True, exist_ok=True)
    state = {"done_parts": 0, "done_bytes": 0, "expected": 0, "seen": set(), "final": None,
             "last_emit": 0.0, "files": set()}

    def hook(d):
        if should_stop():
            raise Cancelled()
        st = d.get("status")
        fn = d.get("filename")
        for k in ("filename", "tmpfilename"):
            if d.get(k):
                state["files"].add(d[k])
        if st == "downloading":
            got = d.get("downloaded_bytes") or 0
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            now = time.monotonic()
            if now - state["last_emit"] < 0.25:
                return
            state["last_emit"] = now
            whole = max(state["expected"], state["done_bytes"] + tot)
            frac = (state["done_bytes"] + got) / whole if whole else None
            on_progress({"stage": "Downloading", "fraction": frac,
                         "downloaded": state["done_bytes"] + got, "total": whole or None,
                         "speed": d.get("speed"), "eta": d.get("eta")})
        elif st == "finished" and fn not in state["seen"]:
            state["seen"].add(fn)
            state["done_parts"] += 1
            state["done_bytes"] += d.get("total_bytes") or d.get("downloaded_bytes") or 0

    def pp_hook(d):
        if d.get("status") == "started":
            on_progress({"stage": PP_LABELS.get(d.get("postprocessor"), "Processing"), "fraction": None})
        elif d.get("status") == "finished":
            fp = (d.get("info_dict") or {}).get("filepath")
            if fp:
                state["final"] = fp

    tmpl = "%(title).150B.%(ext)s"
    opts = base_opts(s, log) | build_format_opts(job) | {
        "paths": {"home": str(folder), "temp": str(folder / ".mediagrab-parts")},
        "outtmpl": {"default": tmpl, "thumbnail": tmpl, "subtitle": tmpl},
        "progress_hooks": [hook], "postprocessor_hooks": [pp_hook],
        "overwrites": False,
    }
    on_progress({"stage": "Preparing", "fraction": None})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job["url"], download=False)
            if should_stop():
                raise Cancelled()
            on_progress({"stage": "Preparing", "fraction": None, "title": info.get("title"),
                         "site": site_name(info), "thumbnail": info.get("thumbnail")})
            parts = info.get("requested_formats") or [info]
            state["expected"] = sum(_fmt_size(f) for f in parts)
            res = ydl.process_ie_result(info, download=True)
            if should_stop():
                raise Cancelled()
            if log.errors and not state["final"]:
                raise EngineError(friendly_error(log.errors[-1]))
            dl = (res or {}).get("requested_downloads") or []
            final = state["final"] or (dl[-1].get("filepath") if dl else None) or res.get("filepath")
    except Cancelled:
        raise Cancelled(_abs(state["files"], folder)) from None
    except EngineError:
        raise
    except Exception as e:
        if should_stop():
            raise Cancelled(_abs(state["files"], folder)) from None
        raise EngineError(friendly_error(log.errors[-1] if log.errors else e)) from None
    if final and not Path(final).is_absolute():
        final = str(folder / final)
    if final and Path(final).parent.name == ".mediagrab-parts":
        final = str(folder / Path(final).name)  # MoveFiles moved it home after the hook fired
    try:
        (folder / ".mediagrab-parts").rmdir()  # only succeeds when empty (no other paused downloads)
    except OSError:
        pass
    on_progress({"stage": "Done", "fraction": 1.0})
    return final or str(folder)


# ---------------------------------------------------------------- local files (Video to MP3)

CODECS = {"mp3": ["-c:a", "libmp3lame"], "m4a": ["-c:a", "aac"], "ogg": ["-c:a", "libvorbis"],
          "opus": ["-c:a", "libopus"], "flac": ["-c:a", "flac"], "wav": ["-c:a", "pcm_s16le"]}


def media_info(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                          "-show_streams", path], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise EngineError("This file can't be read as audio or video.")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    if not any(st.get("codec_type") == "audio" for st in streams):
        raise EngineError("This file has no audio track.")
    return {"duration": float((data.get("format") or {}).get("duration") or 0),
            "has_video": any(st.get("codec_type") == "video" and
                             not (st.get("disposition") or {}).get("attached_pic") for st in streams),
            "tags": (data.get("format") or {}).get("tags") or {}}


def local_thumbnail(path: str) -> str | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / ("l_" + hashlib.sha1(path.encode()).hexdigest()[:20] + ".jpg")
    if dest.exists():
        return str(dest)
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "3", "-i", path, "-frames:v", "1",
                        "-vf", "scale=320:-2", str(dest)], capture_output=True, timeout=60)
    if r.returncode != 0 or not dest.exists():
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-frames:v", "1",
                            "-vf", "scale=320:-2", str(dest)], capture_output=True, timeout=60)
    return str(dest) if dest.exists() else None


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while (q := p.with_name(f"{p.stem} ({i}){p.suffix}")).exists():
        i += 1
    return q


def convert_file(job: dict, s: Settings, on_progress, should_stop) -> str:
    """Extract the audio of a local video/audio file with ffmpeg."""
    src = job["path"]
    fmt = job.get("audio_format", "mp3")
    info = media_info(src)
    folder = Path(job.get("folder") or s.download_dir)
    folder.mkdir(parents=True, exist_ok=True)
    out = unique_path(folder / f"{Path(src).stem}.{fmt}")
    tmp = out.with_name(out.name + ".part." + fmt)
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-y", "-i", src, "-vn", "-sn", "-dn",
           "-map", "0:a:0", "-map_metadata", "0", *CODECS[fmt]]
    if fmt not in ("flac", "wav"):
        cmd += ["-b:a", f"{job.get('bitrate', '320')}k"]
    if fmt == "mp3":
        cmd += ["-id3v2_version", "3"]
    cmd += ["-progress", "pipe:1", str(tmp)]
    dur = info["duration"] or 0
    on_progress({"stage": "Converting", "fraction": 0.0 if dur else None})
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    t0 = time.monotonic()
    try:
        for line in proc.stdout:
            if should_stop():
                proc.kill()
                proc.wait()
                tmp.unlink(missing_ok=True)
                raise Cancelled()
            if line.startswith("out_time_us=") and dur:
                try:
                    pos = int(line.split("=")[1]) / 1e6
                except ValueError:
                    continue
                frac = min(pos / dur, 1.0)
                el = time.monotonic() - t0
                eta = el / frac - el if frac > 0.01 else None
                on_progress({"stage": "Converting", "fraction": frac, "eta": eta,
                             "speed_x": pos / el if el else None})
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        err = (proc.stderr.read() or "").strip().splitlines()
        raise EngineError("Conversion failed: " + (err[-1] if err else f"ffmpeg exit {proc.returncode}"))
    os.replace(tmp, out)
    on_progress({"stage": "Done", "fraction": 1.0})
    return str(out)
