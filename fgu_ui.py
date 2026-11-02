#!/usr/bin/env python3
"""
A basic window for the map tools, the same on Mac and Windows. It starts a small
web page on your own computer and opens it in your browser; nothing is reachable
from outside your machine, and nothing goes online (apart from the Update AI model
cache button, which you press yourself).

    python3 fgu_ui.py [--library "/path/to/maps"] [--no-browser]

From the page you can search the library by description, browse it a page at a
time, sort maps into category folders (preview first, with an undo), detect grids on
any folder, check a Fantasy Grounds campaign for maps that are heavy to display, and
build the index / update the AI model cache.

It is a thin runner: every action starts one of the existing scripts
(fgu_classify_maps.py, fgu_detect_grid.py, fgu_find_maps.py, fgu_support_maptest.py)
with the same flags you would type, and shows their output. So their safety rules apply unchanged:
nothing is overwritten, sorting moves files only after you tick the confirmation
box and always leaves an undo file, and this script itself writes no files at all.

It never tries to show the whole library. Browsing is 48 maps per page, a search
shows at most 100, folders are listed by name and count, and the log keeps the last
few thousand lines.

Who can talk to it: only this computer (it listens on 127.0.0.1), and only a
browser that opened the address printed in the terminal, which contains a random
one-time token that is then kept in a cookie. Other web pages you have open cannot
drive it.
"""
import argparse
import collections
import http.server
import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

import numpy as np

REPO = os.path.dirname(os.path.abspath(__file__))

# Must match fgu_find_maps.py (fgu_support_test_ui.py checks that they do).
INDEX_DIRNAME = ".fgu_map_index"
INDEX_FILENAME = "index.npz"
INDEX_FORMAT = 1
MODEL_ID = "openai/clip-vit-base-patch32"
VALID_IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif')

# The limits that keep a 4,000-map library from ever being put on screen at once.
PAGE_SIZE = 48
MAX_SEARCH_RESULTS = 100
MAX_FOLDERS = 500
MAX_LOG_LINES = 3000
MAX_QUERY_LENGTH = 300

THUMB_NAME = re.compile(r"^[0-9a-f]{20}\.jpg$")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
MANIFEST_NAME = re.compile(r"^fgu_moves_\d{4}-\d{2}-\d{2}_\d{6}\.jsonl$")


# ---------------------------------------------------------------------------
# Library, index and model state (read-only)
# ---------------------------------------------------------------------------
_index_cache = {}


def is_safe_rel(rel):
    """Is rel a plain relative path to an image, written with '/', that stays inside the
    folder it is joined to? (The same rule as fgu_find_maps.is_safe_rel, which the tests
    hold this copy to.) An index file is only data in the library folder, and one that
    came with a downloaded map pack could list '../something' or a program."""
    if not isinstance(rel, str) or not rel or not rel.lower().endswith(VALID_IMAGE_EXTS):
        return False
    if rel.startswith("/") or "\x00" in rel or _DRIVE_PREFIX.match(rel) or (os.sep == "\\" and "\\" in rel):
        return False
    return all(part and not part.startswith(".") for part in rel.split("/"))


def safe_image_path(library, rel):
    """The real path of a map in the library, or None unless it is a plain image path that,
    after following any links, is an image file still inside the library."""
    if not is_safe_rel(rel):
        return None
    real = os.path.realpath(os.path.join(library, *rel.split("/")))
    root = os.path.realpath(library)
    try:
        inside = os.path.commonpath([root, real]) == root
    except ValueError:                      # a different drive
        return None
    if not inside or not real.lower().endswith(VALID_IMAGE_EXTS) or not os.path.isfile(real):
        return None
    return real


def read_index(library):
    """{'paths', 'thumbs', 'folders': {name: [positions]}, 'mtime'} from the library's
    index, or None if there isn't a usable one. Only the names are read, not the
    embeddings, so it's quick even for thousands of maps."""
    path = os.path.join(library, INDEX_DIRNAME, INDEX_FILENAME)
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    cached = _index_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with np.load(path, allow_pickle=False) as data:
            if int(data["format"]) != INDEX_FORMAT:
                return None
            paths = [str(p) for p in data["paths"]]
            thumbs = [str(t) for t in data["thumbs"]]
    except Exception:
        return None
    # Drop anything unsafe: the index is only data in the library folder (see is_safe_rel).
    kept = [i for i in range(len(paths)) if is_safe_rel(paths[i]) and THUMB_NAME.match(thumbs[i])]
    paths, thumbs = [paths[i] for i in kept], [thumbs[i] for i in kept]
    folders = {}
    for position, rel in enumerate(paths):
        folders.setdefault(rel.split("/")[0] if "/" in rel else "", []).append(position)
    index = {"paths": paths, "thumbs": thumbs, "folders": folders, "mtime": st.st_mtime,
             "known": set(paths)}
    _index_cache[path] = (key, index)
    return index


def library_summary(library):
    """Counts only -- never a listing of the maps."""
    top_images, manifests = 0, []
    with os.scandir(library) as entries:
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            name = entry.name
            if name.lower().endswith(VALID_IMAGE_EXTS) and not name.startswith("."):
                top_images += 1
            elif MANIFEST_NAME.match(name):
                manifests.append(name)
    index = read_index(library)
    return {
        "top_level_images": top_images,
        "undo_files": sorted(manifests, reverse=True)[:20],
        "indexed": len(index["paths"]) if index else None,
        "index_updated": index["mtime"] if index else None,
    }


_model_check = {"at": 0.0, "cached": False}


def model_cached():
    """Is the CLIP model in the local cache? (Looked up at most every 15 seconds.)"""
    if time.time() - _model_check["at"] > 15:
        cached = False
        try:
            from huggingface_hub import scan_cache_dir
            cached = any(repo.repo_id == MODEL_ID for repo in scan_cache_dir().repos)
        except Exception:
            pass
        _model_check.update(at=time.time(), cached=cached)
    return _model_check["cached"]


def open_with_default_app(path):
    """Open a map in whatever the computer uses for that file type."""
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ---------------------------------------------------------------------------
# Jobs: one existing script at a time, its output streamed into the page
# ---------------------------------------------------------------------------
UPDATE_MODEL_CODE = (
    "from transformers import CLIPModel, CLIPProcessor\n"
    f"n = '{MODEL_ID}'\n"
    "print('Updating the AI model cache (the first time this downloads about 600 MB; needs internet)...', flush=True)\n"
    "CLIPModel.from_pretrained(n)\n"
    "CLIPProcessor.from_pretrained(n)\n"
    "print('The AI model cache is up to date.')\n"
)


def build_command(action, library, options):
    """(title, argv) for a UI action, or raises ValueError with a message for the user.
    Every action is one of the existing scripts with the flags you'd type yourself. The
    command is a list, never a shell string, so nothing typed into the page is interpreted."""
    py = sys.executable
    script = lambda name: os.path.join(REPO, name)
    if action == "index":
        return "Update the index", [py, script("fgu_find_maps.py"), "index", "--dir", library]
    if action == "search":
        query = str(options.get("query", "")).strip()
        if not query:
            raise ValueError("Type what you are looking for first.")
        if len(query) > MAX_QUERY_LENGTH:
            raise ValueError("That description is too long.")
        try:
            top = int(options.get("top", 24))
        except (TypeError, ValueError):
            top = 24
        top = max(1, min(top, MAX_SEARCH_RESULTS))
        # "--" ends the options, so a description that starts with "-" is still just text.
        return f"Search: {query}", [py, script("fgu_find_maps.py"), "search", "--dir", library,
                                    "--top", str(top), "--no-page", "--json", "--", query]
    if action == "sort_preview":
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        cmd = [py, script("fgu_classify_maps.py"), "--dir", library, "--no-move", "--no-update-check",
               "--report", os.path.join(library, f"fgu_report_{stamp}.csv")]
        if options.get("detect_photos"):
            cmd.append("--detect-photos")
        return "Sort: preview (nothing is moved)", cmd
    if action == "sort":
        if options.get("confirm") is not True:
            raise ValueError("Tick the box to confirm that the maps should be moved.")
        cmd = [py, script("fgu_classify_maps.py"), "--dir", library, "--move-to", library, "-y", "--no-update-check"]
        if options.get("detect_photos"):
            cmd.append("--detect-photos")
        return "Sort maps into category folders", cmd
    if action == "undo":
        name = str(options.get("manifest", ""))
        if options.get("confirm") is not True:
            raise ValueError("Tick the box to confirm that the moves should be undone.")
        if not MANIFEST_NAME.match(name) or not os.path.isfile(os.path.join(library, name)):
            raise ValueError("That undo file isn't in this folder.")
        return "Undo a sort", [py, script("fgu_classify_maps.py"), "--undo", os.path.join(library, name), "-y"]
    if action == "grid":
        cmd = [py, script("fgu_detect_grid.py"), "--dir", library]
        if options.get("force"):
            if options.get("confirm") is not True:
                raise ValueError("Tick the box to confirm that existing grid files may be overwritten.")
            cmd.append("--force")
        return "Detect grids", cmd
    if action == "check":
        try:
            top = int(options.get("top", 25))
        except (TypeError, ValueError):
            top = 25
        cmd = [py, script("fgu_support_maptest.py"), "--no-color", "--top", str(max(1, min(top, MAX_SEARCH_RESULTS)))]
        campaign = str(options.get("campaign", "")).strip().strip('"')
        if campaign:
            is_campaign = os.path.isdir(campaign) or (os.path.basename(campaign) == "db.xml" and os.path.isfile(campaign))
            if not os.path.isabs(campaign) or not is_campaign:
                raise ValueError("Type the full path of a Fantasy Grounds campaign folder, or leave it blank to look in Fantasy Grounds' own data folder.")
            cmd += ["--campaign", campaign]
        return "Check campaign maps for heavy load", cmd
    if action == "update_model":
        return "Update AI model cache", [py, "-c", UPDATE_MODEL_CODE]
    raise ValueError("Unknown action.")


class Job:
    def __init__(self, title, argv):
        self.title, self.argv = title, argv
        self.lines = collections.deque(maxlen=MAX_LOG_LINES)
        self.total = 0                  # lines ever produced; the deque only keeps the last few thousand
        self.status = "running"         # running | done | failed | stopped
        self.returncode = None
        self.results = None
        self.stopping = False
        self.lock = threading.Lock()
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1", HF_HUB_DISABLE_SYMLINKS_WARNING="1")
        self.proc = subprocess.Popen(argv, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=self._read, daemon=True).start()

    def _add(self, text):
        with self.lock:
            self.lines.append(text)
            self.total += 1

    def _read(self):
        for raw in iter(self.proc.stdout.readline, b""):
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            text = text.split("\r")[-1]               # progress bars redraw one line with \r
            if text.startswith("FGU_RESULTS "):
                try:
                    self.results = json.loads(text[len("FGU_RESULTS "):])
                except ValueError:
                    pass
                continue
            if text.strip():
                self._add(text)
        code = self.proc.wait()
        with self.lock:
            self.returncode = code
            self.status = "stopped" if self.stopping else ("done" if code == 0 else "failed")

    def stop(self):
        self.stopping = True
        if self.proc.poll() is None:
            self.proc.terminate()

    def snapshot(self, since):
        with self.lock:
            kept = list(self.lines)
            first = self.total - len(kept)
            start = max(since, first)
            return {"title": self.title, "status": self.status, "returncode": self.returncode,
                    "lines": kept[start - first:], "next": self.total,
                    "results": self.results if self.status == "done" else None}


class Context:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.job = None
        self.lock = threading.Lock()
        self.port = None

    def start_job(self, title, argv):
        with self.lock:
            if self.job is not None and self.job.status == "running":
                return False
            self.job = Job(title, argv)
            return True


# ---------------------------------------------------------------------------
# The web server
# ---------------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "fgu-ui"

    def log_message(self, *args):
        pass

    @property
    def ctx(self):
        return self.server.ctx

    # -- helpers --
    def _send(self, code, body, content_type="application/json", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if not any(name == "Cache-Control" for name, _ in headers):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, {"error": message})

    def _host_ok(self):
        # Refuses a request addressed to anything but this machine's own name for the
        # server, which is what a DNS-rebinding attack on a local server relies on.
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.ctx.port}", f"localhost:{self.ctx.port}")

    def _has_cookie(self):
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "fgu_token" and secrets.compare_digest(value, self.ctx.token):
                return True
        return False

    def _library(self, query):
        library = (query.get("lib") or [""])[0]
        if not library or not os.path.isabs(library) or not os.path.isdir(library):
            raise ValueError("Type the full path of your maps folder, for example /Users/you/Maps or C:\\Maps.")
        return os.path.abspath(library)

    # -- GET --
    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "Wrong address.")
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/":
            supplied = (query.get("token") or [""])[0]
            if supplied and secrets.compare_digest(supplied, self.ctx.token):
                keep = "?lib=" + urllib.parse.quote(query["lib"][0]) if query.get("lib") else ""
                return self._send(302, b"", "text/plain", [
                    ("Location", "/" + keep),
                    ("Set-Cookie", f"fgu_token={self.ctx.token}; HttpOnly; SameSite=Strict; Path=/")])
            if not self._has_cookie():
                return self._error(403, "Open the address printed in the terminal window.")
            return self._send(200, PAGE, "text/html; charset=utf-8")

        if not self._has_cookie():
            return self._error(403, "Open the address printed in the terminal window.")
        try:
            if parsed.path == "/api/status":
                return self._status(query)
            if parsed.path == "/api/folders":
                return self._folders(query)
            if parsed.path == "/api/browse":
                return self._browse(query)
            if parsed.path == "/api/job":
                return self._job(query)
            if parsed.path.startswith("/thumb/"):
                return self._thumb(parsed.path[len("/thumb/"):], query)
        except ValueError as e:
            return self._error(400, str(e))
        except Exception as e:
            return self._error(500, f"Something went wrong: {e}")
        return self._error(404, "Not found.")

    def _status(self, query):
        reply = {"model_ready": model_cached(), "library": None, "error": None}
        try:
            library = self._library(query)
            reply["library"] = library_summary(library)
        except ValueError as e:
            reply["error"] = str(e) if (query.get("lib") or [""])[0] else None
        job = self.ctx.job
        reply["job"] = {"title": job.title, "status": job.status} if job else None
        self._send(200, reply)

    def _folders(self, query):
        library = self._library(query)
        index = read_index(library)
        if index is None:
            return self._send(200, {"indexed": False, "folders": [], "more": 0})
        rows = sorted(({"name": name, "count": len(positions), "path": os.path.join(library, name) if name else library}
                       for name, positions in index["folders"].items()),
                      key=lambda r: (-r["count"], r["name"].lower()))
        self._send(200, {"indexed": True, "folders": rows[:MAX_FOLDERS], "more": max(0, len(rows) - MAX_FOLDERS)})

    def _browse(self, query):
        library = self._library(query)
        index = read_index(library)
        if index is None:
            raise ValueError("This folder hasn't been indexed yet. Use Setup to build the index.")
        folder = (query.get("folder") or [""])[0]
        positions = index["folders"].get(folder)
        if positions is None:
            raise ValueError("No such folder in the index.")
        try:
            page = max(0, int((query.get("page") or ["0"])[0]))
        except ValueError:
            page = 0
        pages = max(1, -(-len(positions) // PAGE_SIZE))
        page = min(page, pages - 1)
        chosen = positions[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        self._send(200, {"page": page, "pages": pages, "total": len(positions), "items": [
            {"rel": index["paths"][p], "thumb": index["thumbs"][p]} for p in chosen]})

    def _job(self, query):
        job = self.ctx.job
        if job is None:
            return self._send(200, {"status": "idle", "lines": [], "next": 0, "title": "", "results": None})
        try:
            since = max(0, int((query.get("since") or ["0"])[0]))
        except ValueError:
            since = 0
        self._send(200, job.snapshot(since))

    def _thumb(self, name, query):
        library = self._library(query)
        if not THUMB_NAME.match(name):
            return self._error(404, "Not found.")
        path = os.path.join(library, INDEX_DIRNAME, "thumbs", name)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._error(404, "Not found.")
        self._send(200, data, "image/jpeg", [("Cache-Control", "private, max-age=3600")])

    # -- POST --
    def do_POST(self):
        if not self._host_ok() or not self._has_cookie():
            return self._error(403, "Not allowed.")
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{self.headers.get('Host')}":
            return self._error(403, "Not allowed.")
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._error(415, "Expected JSON.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                return self._error(413, "Too large.")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._error(400, "Bad request.")

        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/run":
                return self._run(body)
            if path == "/api/stop":
                job = self.ctx.job
                if job is not None and job.status == "running":
                    job.stop()
                return self._send(200, {"ok": True})
            if path == "/api/open":
                return self._open(body)
        except ValueError as e:
            return self._error(400, str(e))
        except Exception as e:
            return self._error(500, f"Something went wrong: {e}")
        return self._error(404, "Not found.")

    def _run(self, body):
        action = str(body.get("action", ""))
        options = body.get("options") if isinstance(body.get("options"), dict) else {}
        library = None
        if action in ("index", "search"):
            library = self._library({"lib": [str(body.get("lib", ""))]})
        elif action in ("sort_preview", "sort", "undo", "grid"):
            # These work on the folder named for them, which can be any folder, not only the library.
            library = self._library({"lib": [str(options.get("folder") or body.get("lib", ""))]})
        title, argv = build_command(action, library, options)
        if not self.ctx.start_job(title, argv):
            return self._error(409, "Another job is still running. Wait for it, or press Stop.")
        self._send(200, {"ok": True})

    def _open(self, body):
        library = self._library({"lib": [str(body.get("lib", ""))]})
        rel = str(body.get("rel", ""))
        index = read_index(library)
        # Only a map the index lists can be opened: the page can't be made to open an arbitrary file.
        if index is None or rel not in index["known"]:
            raise ValueError("That map isn't in the index.")
        full = safe_image_path(library, rel)
        if full is None:
            raise ValueError("That map is no longer there, or isn't a plain image inside the library. Update the index.")
        open_with_default_app(full)
        self._send(200, {"ok": True})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(port=0):
    """A server bound to this machine only. Returns it; .ctx has the token and the job."""
    server = Server(("127.0.0.1", port), Handler)
    server.ctx = Context()
    server.ctx.port = server.server_address[1]
    return server


# ---------------------------------------------------------------------------
# The page. Everything shown from file names is set with textContent, never as HTML.
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Map tools</title>
<style>
:root { --bg:#fafaf8; --fg:#1d1d1b; --muted:#6b6b66; --card:#ffffff; --line:#d9d7d0; --accent:#2f5f8f; --accent-fg:#fff; --warn:#8a4b08; --bad:#a32020; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --fg:#ecebe6; --muted:#9a9990; --card:#21211f; --line:#3a3935; --accent:#6aa2d6; --accent-fg:#101010; --warn:#e0a35c; --bad:#ee7b7b; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
.wrap { max-width:1100px; margin:0 auto; padding:16px; }
h1 { font-size:20px; margin:0 0 12px; } h2 { font-size:16px; margin:0 0 8px; }
.muted { color:var(--muted); } .warn { color:var(--warn); } .bad { color:var(--bad); }
input[type=text], select { font:inherit; padding:7px 9px; border:1px solid var(--line); border-radius:6px; background:var(--card); color:var(--fg); }
button { font:inherit; padding:7px 14px; border:1px solid var(--line); border-radius:6px; background:var(--card); color:var(--fg); cursor:pointer; }
button.primary { background:var(--accent); color:var(--accent-fg); border-color:var(--accent); }
button:disabled { opacity:.5; cursor:not-allowed; }
button.stacked { text-align:left; line-height:1.25; }
button.stacked small { display:block; font-size:12px; color:var(--muted); }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:8px 0; }
#lib { flex:1 1 320px; min-width:0; }
#status { margin:6px 0 0; font-size:14px; }
.tabs { display:flex; gap:4px; margin:14px 0 0; border-bottom:1px solid var(--line); flex-wrap:wrap; }
.tab { border:1px solid transparent; border-bottom:none; border-radius:6px 6px 0 0; background:none; }
.tab[aria-selected=true] { background:var(--card); border-color:var(--line); font-weight:600; position:relative; top:1px; }
.panel { background:var(--card); border:1px solid var(--line); border-top:none; padding:14px; border-radius:0 0 8px 8px; }
.panel[hidden] { display:none; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:10px; margin-top:12px; }
.item { border:1px solid var(--line); border-radius:8px; overflow:hidden; background:var(--bg); display:flex; flex-direction:column; }
.item img { width:100%; aspect-ratio:1/1; object-fit:cover; display:block; background:var(--line); }
.item .cap { padding:6px 8px; font-size:12.5px; overflow-wrap:anywhere; flex:1; }
.item .cap small { display:block; color:var(--muted); }
.item button { margin:0 8px 8px; padding:4px 8px; font-size:13px; }
.folders { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:6px; max-height:140px; overflow:auto; }
.folders button[aria-pressed=true] { background:var(--accent); color:var(--accent-fg); border-color:var(--accent); }
#logbox { margin-top:16px; border:1px solid var(--line); border-radius:8px; background:var(--card); }
#logbox .bar { display:flex; justify-content:space-between; align-items:center; gap:8px; padding:8px 12px; border-bottom:1px solid var(--line); }
#log { margin:0; padding:10px 12px; height:230px; overflow:auto; font:12.5px/1.4 ui-monospace,Menlo,Consolas,monospace; white-space:pre-wrap; overflow-wrap:anywhere; }
label.check { display:flex; gap:8px; align-items:flex-start; margin:6px 0; }
p { margin:6px 0; }
</style></head><body><div class="wrap">
<h1>Map tools</h1>

<div class="row">
  <input type="text" id="lib" placeholder="Full path of your maps folder, e.g. /Users/you/Maps or C:\Maps" autocomplete="off" spellcheck="false">
  <button id="check" class="primary">Use this folder</button>
</div>
<div id="status" class="muted">Type the folder your maps are in, then press the button.</div>

<div class="tabs" role="tablist">
  <button class="tab" role="tab" data-tab="find" aria-selected="true">Find</button>
  <button class="tab" role="tab" data-tab="browse" aria-selected="false">Browse</button>
  <button class="tab" role="tab" data-tab="sort" aria-selected="false">Sort</button>
  <button class="tab" role="tab" data-tab="grid" aria-selected="false">Grids</button>
  <button class="tab" role="tab" data-tab="check" aria-selected="false">Check</button>
  <button class="tab" role="tab" data-tab="setup" aria-selected="false">Setup</button>
</div>

<div class="panel" id="panel-find">
  <p class="muted">Describe the map you want, for example <em>tavern interior at night</em>. It looks at the pictures, so it also finds maps in folders like "unclassified".</p>
  <div class="row">
    <input type="text" id="query" placeholder="What are you looking for?" style="flex:1 1 300px" maxlength="300">
    <select id="top"><option>12</option><option selected>24</option><option>48</option><option>100</option></select>
    <span class="muted">results</span>
    <button id="search" class="primary">Search</button>
  </div>
  <div id="find-note" class="muted"></div>
  <div class="grid" id="find-results"></div>
</div>

<div class="panel" id="panel-browse" hidden>
  <p class="muted">Pick a folder to look through it a page at a time. (Only the maps in the index are shown.)</p>
  <div class="folders" id="folders"></div>
  <div class="row"><button id="prev">&larr; Previous</button><span id="pageinfo" class="muted"></span><button id="next">Next &rarr;</button></div>
  <div class="grid" id="browse-results"></div>
</div>

<div class="panel" id="panel-sort" hidden>
  <p>Sorts maps into category folders (dungeon, ruin, tavern, ...). It only looks at maps sitting <strong>directly inside</strong> the folder, not in its sub-folders, so a library that is already sorted has little left to do.</p>
  <div class="row"><label for="sort-folder">Folder to sort:</label>
    <input type="text" id="sort-folder" list="folder-choices" placeholder="(the folder at the top)" style="flex:1 1 320px" autocomplete="off" spellcheck="false"></div>
  <p id="sort-count" class="muted"></p>
  <label class="check"><input type="checkbox" id="photos"> <span>Also tag pictures that look like photographs "not a map" (experimental).</span></label>
  <div class="row"><button id="preview" class="primary">Preview (moves nothing)</button><span class="muted">Writes a CSV report of what it would do into the folder.</span></div>
  <hr style="border:0;border-top:1px solid var(--line);margin:14px 0">
  <label class="check"><input type="checkbox" id="confirm-sort"> <span>Yes, move the maps into category folders inside this folder. An undo file is saved.</span></label>
  <div class="row"><button id="sortnow">Sort the maps</button></div>
  <h2 style="margin-top:18px">Undo a sort</h2>
  <div class="row"><select id="undo-file"></select></div>
  <label class="check"><input type="checkbox" id="confirm-undo"> <span>Yes, put those maps back where they were. Nothing is overwritten and no folders are removed. (Only undo files made by this installation are used; one from anywhere else is refused.)</span></label>
  <div class="row"><button id="undo">Undo</button></div>
</div>

<div class="panel" id="panel-grid" hidden>
  <p>Detects square grids and writes a Fantasy Grounds <code>.xml</code> file next to each map. This looks in the folder <strong>and its sub-folders</strong>, so on a big library it can take a long time and a lot of memory for very large maps. Maps that already have an <code>.xml</code> are skipped.</p>
  <div class="row"><label for="grid-folder">Folder:</label>
    <input type="text" id="grid-folder" list="folder-choices" placeholder="(the folder at the top)" style="flex:1 1 320px" autocomplete="off" spellcheck="false"></div>
  <p class="muted">Leave it blank for the whole library, or pick or type one folder (any folder on this computer).</p>
  <div class="row"><button id="grid" class="primary">Detect grids</button></div>
  <label class="check"><input type="checkbox" id="force"> <span>Replace existing grid files too (other data in them is kept).</span></label>
  <label class="check"><input type="checkbox" id="confirm-grid"> <span>Yes, overwrite existing grid files. (Needed for the box above.)</span></label>
</div>

<div class="panel" id="panel-check" hidden>
  <p>Ranks the maps in a Fantasy Grounds campaign by how heavy they are to display (picture size, lights and line-of-sight walls), worst first, so you know which to fix. It only <strong>reads</strong> the campaign's files.</p>
  <div class="row"><input type="text" id="check-folder" placeholder="Campaign folder (optional), e.g. .../Fantasy Grounds/campaigns/My Campaign" style="flex:1 1 360px" autocomplete="off" spellcheck="false"></div>
  <p class="muted">Leave it blank to look through the campaigns in Fantasy Grounds' own data folder.</p>
  <div class="row"><select id="check-top"><option>10</option><option selected>25</option><option>50</option><option>100</option></select>
    <span class="muted">worst maps</span><button id="checkrun" class="primary">Check maps</button></div>
</div>

<div class="panel" id="panel-setup" hidden>
  <h2>AI model</h2>
  <p id="model-state" class="muted"></p>
  <div class="row"><button id="update-model" class="stacked">Update AI model cache<small>Requires internet access</small></button><span class="muted">The first time this downloads the model (about 600 MB); after that it checks for newer files. Everything else works offline.</span></div>
  <h2 style="margin-top:18px">Index</h2>
  <p class="muted">Finding and browsing use an index of your library, kept in a hidden <code>.fgu_map_index</code> folder inside it. The first build reads every map once and can take a long time on a big library; after that only new maps are read. Searching also updates it by itself.</p>
  <div class="row"><button id="index" class="primary">Build / update the index</button></div>
</div>

<datalist id="folder-choices"></datalist>

<div id="logbox">
  <div class="bar"><strong id="jobtitle" class="muted">Nothing running</strong><button id="stop" disabled>Stop</button></div>
  <pre id="log"></pre>
</div>
</div>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
let lib = "", busy = false, jobSince = 0, lastLine = "", pollTimer = null, wantResultsFor = null, browseFolder = null, browsePage = 0;

function remember(value) { try { localStorage.setItem("fgu-lib", value); } catch (e) {} }
function recall() { try { return localStorage.getItem("fgu-lib") || ""; } catch (e) { return ""; } }

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || ("Error " + response.status));
  return data;
}
const q = (params) => new URLSearchParams(params).toString();

function setBusy(value) {
  busy = value;
  for (const id of ["search", "preview", "sortnow", "undo", "grid", "update-model", "index", "checkrun"]) $(id).disabled = value;
  $("stop").disabled = !value;
}

function showTab(name) {
  for (const tab of document.querySelectorAll(".tab")) tab.setAttribute("aria-selected", String(tab.dataset.tab === name));
  for (const panel of document.querySelectorAll(".panel")) panel.hidden = panel.id !== "panel-" + name;
  if (name === "browse") loadFolders();
}
for (const tab of document.querySelectorAll(".tab")) tab.addEventListener("click", () => showTab(tab.dataset.tab));

function say(text, cls) { const s = $("status"); s.textContent = text; s.className = cls || "muted"; }

async function refreshStatus() {
  const s = await api("/api/status?" + q({ lib }));
  $("model-state").textContent = s.model_ready ? "The AI model is in the cache and ready." : "The AI model is not in the cache yet. Searching and sorting need it: press Update AI model cache below.";
  $("model-state").className = s.model_ready ? "muted" : "warn";
  if (!lib) return;
  if (s.error || !s.library) { say(s.error || "Folder not found.", "bad"); return; }
  const l = s.library, parts = [];
  parts.push(l.indexed === null ? "Not indexed yet (see Setup)" : l.indexed.toLocaleString() + " maps indexed");
  parts.push("model " + (s.model_ready ? "ready" : "not in the cache (see Setup)"));
  parts.push(l.top_level_images.toLocaleString() + " maps directly in this folder (what Sort looks at)");
  say(parts.join("  \u00b7  "), (l.indexed === null || !s.model_ready) ? "warn" : "muted");
  fillFolderChoices(l.indexed !== null).catch(() => {});
  refreshSortInfo().catch(() => {});
}

// The folder a tool should work on: what was typed in its own box, else the folder at the top.
const folderOf = (id) => $(id).value.trim().replace(/^"|"$/g, "") || lib;

async function refreshSortInfo() {
  const folder = folderOf("sort-folder"), sel = $("undo-file");
  sel.textContent = "";
  let names = [], text = "";
  if (folder) {
    const s = await api("/api/status?" + q({ lib: folder }));
    if (s.library) {
      names = s.library.undo_files;
      text = s.library.top_level_images.toLocaleString() + " map(s) are directly inside this folder.";
    } else { text = s.error || "That folder was not found."; }
  }
  $("sort-count").textContent = text;
  for (const name of names) { const o = document.createElement("option"); o.textContent = name; sel.appendChild(o); }
  if (!names.length) { const o = document.createElement("option"); o.textContent = "(no undo files in this folder)"; sel.appendChild(o); }
}

// Offers the library's top-level folders (names and counts only) in the folder boxes.
async function fillFolderChoices(indexed) {
  const list = $("folder-choices"); list.textContent = "";
  if (!indexed || !lib) return;
  const f = await api("/api/folders?" + q({ lib }));
  for (const row of f.folders) {
    const o = document.createElement("option"); o.value = row.path;
    o.label = (row.name || "(top level)") + " \u00b7 " + row.count.toLocaleString();
    list.appendChild(o);
  }
}

function card(item, withScore) {
  const div = document.createElement("div"); div.className = "item";
  const img = document.createElement("img"); img.loading = "lazy"; img.alt = "";
  img.src = "/thumb/" + encodeURIComponent(item.thumb) + "?" + q({ lib });
  div.appendChild(img);
  const cap = document.createElement("div"); cap.className = "cap";
  const slash = item.rel.lastIndexOf("/");
  cap.appendChild(document.createTextNode(slash < 0 ? item.rel : item.rel.slice(slash + 1)));
  const small = document.createElement("small");
  small.textContent = (slash < 0 ? "(top level)" : item.rel.slice(0, slash)) + (withScore ? "  \u00b7  match " + item.score.toFixed(3) : "");
  cap.appendChild(small); div.appendChild(cap);
  const open = document.createElement("button"); open.textContent = "Open map";
  open.addEventListener("click", async () => { try { await api("/api/open", { lib, rel: item.rel }); } catch (e) { alert(e.message); } });
  div.appendChild(open);
  return div;
}

async function loadFolders() {
  const box = $("folders"); box.textContent = "";
  if (!lib) { $("pageinfo").textContent = "Choose a folder first."; return; }
  try {
    const f = await api("/api/folders?" + q({ lib }));
    if (!f.indexed) { $("pageinfo").textContent = "Not indexed yet. Use Setup to build the index."; return; }
    for (const row of f.folders) {
      const b = document.createElement("button"); b.setAttribute("aria-pressed", String(row.name === browseFolder));
      b.textContent = (row.name || "(top level)") + " \u00b7 " + row.count.toLocaleString();
      b.addEventListener("click", () => { browseFolder = row.name; browsePage = 0; loadFolders(); loadPage(); });
      box.appendChild(b);
    }
    if (f.more) { const m = document.createElement("span"); m.className = "muted"; m.textContent = "+ " + f.more + " more folders not shown"; box.appendChild(m); }
  } catch (e) { $("pageinfo").textContent = e.message; }
}

async function loadPage() {
  const out = $("browse-results"); out.textContent = "";
  if (browseFolder === null) return;
  try {
    const p = await api("/api/browse?" + q({ lib, folder: browseFolder, page: browsePage }));
    browsePage = p.page;
    $("pageinfo").textContent = "Page " + (p.page + 1) + " of " + p.pages + "  (" + p.total.toLocaleString() + " maps)";
    $("prev").disabled = p.page <= 0; $("next").disabled = p.page >= p.pages - 1;
    for (const item of p.items) out.appendChild(card(item, false));
  } catch (e) { $("pageinfo").textContent = e.message; }
}
$("prev").addEventListener("click", () => { browsePage = Math.max(0, browsePage - 1); loadPage(); });
$("next").addEventListener("click", () => { browsePage += 1; loadPage(); });

async function run(action, options) {
  try {
    await api("/api/run", { action, lib, options: options || {} });
    jobSince = 0; lastLine = ""; $("log").textContent = ""; setBusy(true); poll();
    return true;
  } catch (e) { alert(e.message); return false; }
}

async function poll() {
  clearTimeout(pollTimer);
  let job;
  try { job = await api("/api/job?" + q({ since: jobSince })); } catch (e) { pollTimer = setTimeout(poll, 2000); return; }
  const log = $("log"), atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  if (job.lines.length) { log.textContent += job.lines.join("\n") + "\n"; }
  jobSince = job.next;
  if (job.lines.length) lastLine = job.lines[job.lines.length - 1].trim();
  if (atBottom) log.scrollTop = log.scrollHeight;
  // While it runs, the newest line (e.g. "[1200/4322] 27% . 7.1 maps/s . about 7 min left") sits next to the title.
  $("jobtitle").textContent = job.title ? job.title + " \u2014 " + job.status
      + (job.status === "running" && lastLine ? "  \u00b7  " + lastLine : "") : "Nothing running";
  if (job.status === "running") { setBusy(true); pollTimer = setTimeout(poll, 800); return; }
  setBusy(false);
  if (job.status === "failed") log.textContent += "\n(It finished with an error; see above.)\n";
  if (wantResultsFor && job.results) { showResults(job.results); }
  wantResultsFor = null;
  refreshStatus().catch(() => {});
  if (!$("panel-browse").hidden) loadFolders();
}

function showResults(results) {
  const out = $("find-results"); out.textContent = "";
  $("find-note").textContent = results.length ? results.length + " best matches, best first. The scores only rank them; judge by the pictures." : "No maps to show. Has the library been indexed?";
  for (const item of results) out.appendChild(card(item, true));
}

$("check").addEventListener("click", async () => {
  lib = $("lib").value.trim().replace(/^"|"$/g, ""); $("lib").value = lib; remember(lib);
  browseFolder = null; $("browse-results").textContent = ""; $("find-results").textContent = "";
  try { await refreshStatus(); } catch (e) { say(e.message, "bad"); }
});
$("lib").addEventListener("keydown", (e) => { if (e.key === "Enter") $("check").click(); });
$("search").addEventListener("click", () => { wantResultsFor = $("query").value; run("search", { query: $("query").value, top: parseInt($("top").value, 10) }).then((ok) => { if (!ok) wantResultsFor = null; }); });
$("query").addEventListener("keydown", (e) => { if (e.key === "Enter") $("search").click(); });
$("preview").addEventListener("click", () => run("sort_preview", { folder: folderOf("sort-folder"), detect_photos: $("photos").checked }));
$("sortnow").addEventListener("click", () => run("sort", { folder: folderOf("sort-folder"), detect_photos: $("photos").checked, confirm: $("confirm-sort").checked }));
$("undo").addEventListener("click", () => run("undo", { folder: folderOf("sort-folder"), manifest: $("undo-file").value, confirm: $("confirm-undo").checked }));
$("grid").addEventListener("click", () => run("grid", { folder: folderOf("grid-folder"), force: $("force").checked, confirm: $("confirm-grid").checked }));
$("checkrun").addEventListener("click", () => run("check", { campaign: $("check-folder").value.trim(), top: parseInt($("check-top").value, 10) }));
$("sort-folder").addEventListener("change", () => refreshSortInfo().catch(() => {}));
$("update-model").addEventListener("click", () => run("update_model"));
$("index").addEventListener("click", () => run("index"));
$("stop").addEventListener("click", () => api("/api/stop", {}).catch(() => {}));

(function start() {
  const fromAddress = new URLSearchParams(location.search).get("lib");
  lib = fromAddress || recall(); $("lib").value = lib;
  setBusy(false);
  refreshStatus().catch((e) => say(e.message, "bad"));
  poll();
})();
</script></body></html>
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description="A basic window for the map tools.")
    parser.add_argument("--library", help="the maps folder to start with")
    parser.add_argument("--port", type=int, default=0, help="port to listen on (default: any free one)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser by itself")
    args = parser.parse_args(argv)

    server = make_server(args.port)
    url = f"http://127.0.0.1:{server.ctx.port}/?token={server.ctx.token}"
    if args.library:
        url += "&lib=" + urllib.parse.quote(os.path.abspath(args.library))
    print("Map tools are running. If your browser doesn't open by itself, open this address:", flush=True)
    print(f"   {url}", flush=True)
    print("Close this window (or press Ctrl+C) to stop them.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        job = server.ctx.job
        if job is not None and job.status == "running":
            job.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
