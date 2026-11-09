#!/usr/bin/env python3
"""Local read-only NTFS browser using ntfsls/ntfscat; no OS mount required."""

import argparse
from collections import OrderedDict
import ctypes
import hmac
import json
import locale
import os
from pathlib import Path
import posixpath
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit


LINE = re.compile(r"^\s*(\d+)\s+(\w{3}\s+\d+\s+\d{2}:\d{2}\s+\d{4}) (.*)$")
IMAGE_EXTS = set("jpg jpeg png gif webp bmp tif tiff heic heif avif dng cr2 cr3 nef nrw arw raf rw2 orf srw pef".split())
VIDEO_EXTS = set("mp4 mov m4v webm mkv avi mts m2ts mpg mpeg 3gp wmv".split())
MEDIA_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
               "webp": "image/webp", "avif": "image/avif", "bmp": "image/bmp", "mp4": "video/mp4",
               "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}


def media_kind(name):
    ext = name.rsplit(".", 1)[-1].lower()
    return "image" if ext in IMAGE_EXTS else "video" if ext in VIDEO_EXTS else "file"


class NativeReader:
    """Read offsets through libntfs; NTFS_MNT_RDONLY | NTFS_MNT_FORENSIC.

    This opens a userspace filesystem context, never an OS mount. Every C call
    on the shared context is serialized because libntfs caches mutable metadata.
    """
    def __init__(self, device):
        library = next((p for p in ("/opt/homebrew/lib/libntfs-3g.dylib",
                                   "/usr/local/lib/libntfs-3g.dylib") if os.path.exists(p)), None)
        if not library:
            raise RuntimeError("libntfs-3g is unavailable")
        self.lib = ctypes.CDLL(library, use_errno=True)
        ptr, i64 = ctypes.c_void_p, ctypes.c_int64
        signatures = {
            "ntfs_mount": ([ctypes.c_char_p, ctypes.c_ulong], ptr),
            "ntfs_umount": ([ptr, ctypes.c_int], ctypes.c_int),
            "ntfs_pathname_to_inode": ([ptr, ptr, ctypes.c_char_p], ptr),
            "ntfs_inode_close": ([ptr], ctypes.c_int),
            "ntfs_attr_open": ([ptr, ctypes.c_uint32, ptr, ctypes.c_uint32], ptr),
            "ntfs_attr_close": ([ptr], None),
            "ntfs_attr_pread": ([ptr, i64, i64, ptr], i64),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = arguments, result
        locale.setlocale(locale.LC_CTYPE, "en_US.UTF-8")
        self.lock = threading.RLock()
        self.active = 0
        self.closing = False
        self.volume = self.lib.ntfs_mount(os.fsencode(device), 0x04000001)
        if not self.volume:
            raise OSError(ctypes.get_errno(), "Cannot open read-only NTFS context")

    def chunks(self, path, offset, count):
        inode = attribute = None
        registered = False
        try:
            with self.lock:
                if self.closing:
                    raise OSError("SSD reader is closing")
                self.active += 1
                registered = True
                inode = self.lib.ntfs_pathname_to_inode(self.volume, None, path.encode("utf-8"))
                if not inode:
                    raise OSError(ctypes.get_errno(), "Cannot open SSD file")
                attribute = self.lib.ntfs_attr_open(inode, 0x80, None, 0)
                if not attribute:
                    raise OSError(ctypes.get_errno(), "Cannot open file data")
            while count:
                buffer = ctypes.create_string_buffer(min(count, 1024 * 1024))
                with self.lock:
                    length = self.lib.ntfs_attr_pread(attribute, offset, len(buffer), buffer)
                if length < 0:
                    raise OSError(ctypes.get_errno(), "SSD read failed")
                if not length:
                    raise OSError("SSD returned fewer bytes than expected")
                yield buffer.raw[:length]
                offset += length
                count -= length
        finally:
            with self.lock:
                if attribute:
                    self.lib.ntfs_attr_close(attribute)
                if inode:
                    self.lib.ntfs_inode_close(inode)
                if registered:
                    self.active -= 1
                if self.closing and not self.active and self.volume:
                    self.lib.ntfs_umount(self.volume, 0)
                    self.volume = None

    def close(self):
        with self.lock:
            self.closing = True
            if self.volume and not self.active:
                self.lib.ntfs_umount(self.volume, 0)
                self.volume = None


def parse_listing(output):
    entries = []
    for line in output.splitlines():
        if not line.strip():
            continue
        match = LINE.fullmatch(line)
        if not match:
            raise RuntimeError("Unexpected ntfsls output: " + line[:200])
        size, modified, name = match.groups()
        directory = name.endswith("/")
        name = name[:-1] if directory else name
        if name in (".", ".."):
            continue
        if not name or "/" in name:
            raise RuntimeError("Unsupported filename in directory listing")
        entries.append(dict(name=name, directory=directory, size=int(size), modified=modified))
    return sorted(entries, key=lambda entry: (not entry["directory"], entry["name"].casefold()))


def normalized_path(value):
    if not value.startswith("/") or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid SSD path")
    if ".." in value.split("/"):
        raise ValueError("Parent path components are not allowed")
    return "/" + posixpath.normpath(value).lstrip("/")


class NTFS:
    def __init__(self, device):
        self.device = device
        self.ls = shutil.which("ntfsls") or "/opt/homebrew/bin/ntfsls"
        self.cat = shutil.which("ntfscat") or "/opt/homebrew/bin/ntfscat"
        for tool in (self.ls, self.cat):
            if not os.access(tool, os.X_OK):
                raise RuntimeError(f"Utility unavailable: {tool}")
        # Keep Unicode filenames while ensuring a predictable date format.
        self.env = dict(os.environ, LC_ALL="en_US.UTF-8")
        self.slots = threading.BoundedSemaphore(4)
        self.cache_lock = threading.Lock()
        self.list_cache = OrderedDict()
        self.thumbs = OrderedDict()
        self.thumb_bytes = 0
        self.thumb_slots = threading.BoundedSemaphore(2)
        self.ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        self.reader = None
        self.base_url = None

    def enable_previews(self):
        self.reader = NativeReader(self.device)

    def file_entry(self, path):
        parent, name = posixpath.split(path)
        return next((entry for entry in self.listing(parent or "/")
                     if entry["name"] == name and not entry["directory"]), None)

    def listing(self, path, refresh=False):
        with self.cache_lock:
            cached = self.list_cache.get(path)
            if not refresh and cached and time.monotonic() - cached[0] < 30:
                return cached[1]
        with self.slots:
            result = subprocess.run([self.ls, "-l", "-F", "-p", path, self.device],
                                    capture_output=True, env=self.env, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()
                               or result.stdout.decode("utf-8", "replace").strip()
                               or "ntfsls failed")
        entries = parse_listing(result.stdout.decode("utf-8", "strict"))
        with self.cache_lock:
            self.list_cache[path] = (time.monotonic(), entries)
            self.list_cache.move_to_end(path)
            while len(self.list_cache) > 128:
                self.list_cache.popitem(last=False)
        return entries

    def thumbnail(self, path, entry, large=False):
        width = 1800 if large else 480
        key = (path, entry["size"], entry["modified"], width)
        with self.thumb_slots:
            with self.cache_lock:
                if key in self.thumbs:
                    self.thumbs.move_to_end(key)
                    return self.thumbs[key]
            url = self.base_url + "&" + urlencode(dict(path=path))
            kind = media_kind(path)
            result = None
            if os.access(self.ffmpeg, os.X_OK):
                for seek in (["1", "0"] if kind == "video" else ["0"]):
                    command = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                               "-threads", "1", "-rw_timeout", "15000000", "-protocol_whitelist", "http,tcp"]
                    if kind == "video":
                        command += ["-ss", seek]
                    command += ["-i", url, "-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn",
                                "-vf", f"scale={width}:{width}:force_original_aspect_ratio=decrease",
                                "-threads", "1", "-q:v", "4", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]
                    try:
                        converted = subprocess.run(command, capture_output=True, timeout=30)
                        if converted.returncode == 0 and converted.stdout.startswith(b"\xff\xd8"):
                            result = converted.stdout
                            break
                    except subprocess.TimeoutExpired:
                        pass
            # ImageIO also supports many HEIC and camera RAW formats on macOS.
            if result is None and kind == "image" and entry["size"] <= 128 * 1024 * 1024:
                with tempfile.TemporaryDirectory(prefix="ssd-preview-") as temp:
                    source = Path(temp) / ("source." + path.rsplit(".", 1)[-1].lower())
                    output = Path(temp) / "preview.jpg"
                    with source.open("wb") as stream:
                        for chunk in self.reader.chunks(path, 0, entry["size"]):
                            stream.write(chunk)
                    converted = subprocess.run(["/usr/bin/sips", "-s", "format", "jpeg",
                                                "--resampleHeightWidthMax", str(width), str(source),
                                                "--out", str(output)], capture_output=True, timeout=30)
                    if converted.returncode == 0 and output.exists():
                        result = output.read_bytes()
            if result is None:
                raise RuntimeError("No preview is available for this format")
            with self.cache_lock:
                self.thumbs[key] = result
                self.thumb_bytes += len(result)
                while self.thumb_bytes > 64 * 1024 * 1024 and self.thumbs:
                    _, removed = self.thumbs.popitem(last=False)
                    self.thumb_bytes -= len(removed)
            return result


def make_handler(backend, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Do not log the access token from request URLs.

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'nonce-" + token + "'; style-src 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            super().end_headers()

        def do_GET(self):
            self.dispatch(False)

        def do_HEAD(self):
            self.dispatch(True)

        def dispatch(self, head):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            supplied_token = query.get("token", [""])[0]
            if not hmac.compare_digest(supplied_token.encode(), token.encode()):
                self.send_error(403, "Open the complete URL printed in Terminal")
                return
            try:
                path = normalized_path(query.get("path", ["/"])[0])
                if parsed.path == "/api/list":
                    entries = [dict(entry, kind="folder" if entry["directory"] else media_kind(entry["name"]),
                                    path=posixpath.join(path, entry["name"]))
                               for entry in backend.listing(path, query.get("refresh") == ["1"])]
                    self.respond(json.dumps(dict(path=path, entries=entries)).encode(), "application/json", head)
                elif parsed.path in ("/thumb", "/preview"):
                    entry = backend.file_entry(path)
                    if not entry or not backend.reader or media_kind(path) == "file":
                        self.send_error(404, "Preview unavailable")
                        return
                    data = backend.thumbnail(path, entry, parsed.path == "/preview")
                    self.respond(data, "image/jpeg", head)
                elif parsed.path == "/media" or (parsed.path == "/" and query.get("download") == ["1"]):
                    if backend.reader:
                        self.media(path, query.get("download") == ["1"], head)
                    elif not head and query.get("download") == ["1"]:
                        self.download(path)
                    else:
                        self.send_error(503, "Restart with libntfs-3g available for previews")
                elif parsed.path == "/":
                    config = json.dumps(dict(token=token, path=path, previews=bool(backend.reader),
                                             device=backend.device)).replace("<", "\\u003c")
                    ui = Path(__file__).with_name("ssd_browser_ui.html").read_text()
                    body = ui.replace("__BOOTSTRAP__", config).replace("__NONCE__", token).encode()
                    self.respond(body, "text/html; charset=utf-8", head)
                else:
                    self.send_error(404)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except ValueError as exc:
                self.send_error(400, str(exc))
            except (RuntimeError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
                self.send_error(503, "SSD read failed: " + str(exc)[:500])

        def respond(self, body, content_type, head=False):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def media(self, path, attachment, head):
            entry = backend.file_entry(path)
            if not entry:
                self.send_error(404, "File not found")
                return
            size, start, end = entry["size"], 0, entry["size"] - 1
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                if not match or not any(match.groups()) or not size:
                    match = None
                if match:
                    left, right = match.groups()
                    if left:
                        start = int(left)
                        end = min(int(right), size - 1) if right else size - 1
                    else:
                        start = max(0, size - int(right))
                if not match or start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            count = max(0, end - start + 1)
            chunks = backend.reader.chunks(path, start, count)
            try:
                # Open and validate the data stream before committing response headers.
                first = next(chunks, b"") if not head else b""
                self.send_response(206 if range_header else 200)
                ext = path.rsplit(".", 1)[-1].lower()
                self.send_header("Content-Type", "application/octet-stream" if attachment else MEDIA_TYPES.get(ext, "application/octet-stream"))
                self.send_header("Content-Length", str(count))
                self.send_header("Accept-Ranges", "bytes")
                if range_header:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                if attachment:
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(entry["name"], safe=""))
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(first)
                        for chunk in chunks:
                            self.wfile.write(chunk)
                    except (OSError, RuntimeError):
                        self.close_connection = True
            finally:
                chunks.close()

        def download(self, path):
            parent, name = posixpath.split(path)
            entry = next((entry for entry in backend.listing(parent or "/")
                          if entry["name"] == name and not entry["directory"]), None)
            if entry is None:
                self.send_error(404, "File not found")
                return
            # Stream large videos instead of loading them into memory or copying them first.
            with backend.slots, tempfile.TemporaryFile() as errors:
                process = subprocess.Popen([backend.cat, backend.device, path],
                                           stdout=subprocess.PIPE, stderr=errors, env=backend.env)
                try:
                    first = process.stdout.read(min(1024 * 1024, entry["size"]))
                    if not first and entry["size"]:
                        process.wait(timeout=10)
                        errors.seek(0)
                        raise RuntimeError(errors.read(2000).decode("utf-8", "replace") or "File could not be read")
                    if not entry["size"]:
                        process.wait(timeout=10)
                        if process.returncode:
                            errors.seek(0)
                            raise RuntimeError(errors.read(2000).decode("utf-8", "replace"))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name, safe=""))
                    self.send_header("Content-Length", str(entry["size"]))
                    self.end_headers()
                    try:
                        self.wfile.write(first)
                        remaining = entry["size"] - len(first)
                        while remaining:
                            chunk = process.stdout.read(min(1024 * 1024, remaining))
                            if not chunk:
                                print("Download interrupted: SSD returned fewer bytes than expected.", file=sys.stderr)
                                self.close_connection = True
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        self.close_connection = True
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    process.stdout.close()

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/disk8s1", help="NTFS partition device")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not re.fullmatch(r"/dev/r?disk\d+s\d+", args.device):
        parser.error("Expected a partition device such as /dev/disk8s1")
    try:
        backend = NTFS(args.device)
        backend.listing("/")
    except (RuntimeError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        parser.error(f"Cannot read SSD: {exc}\nRun this script with sudo for raw device access.")
    try:
        backend.enable_previews()
    except (RuntimeError, OSError, AttributeError) as exc:
        print(f"Previews unavailable; downloads still work: {exc}", file=sys.stderr)
    token = secrets.token_urlsafe(32)
    try:
        with ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(backend, token)) as server:
            backend.base_url = f"http://127.0.0.1:{server.server_port}/media?" + urlencode(dict(token=token))
            print(f"Read-only NTFS browser: {args.device}\nOpen http://127.0.0.1:{args.port}/?token={token}\nPress Ctrl+C to stop.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    except OSError as exc:
        parser.error(str(exc))
    finally:
        if backend.reader:
            backend.reader.close()


if __name__ == "__main__":
    main()
