#!/usr/bin/env python3
"""
Tests for fgu_ui.py, the basic map-tools window.

It checks the things that matter for a local web page that can start programs and
open files:

  - who may talk to it: only the browser that was given the one-time address, only
    when addressed as this machine, and POSTs only as same-origin JSON
  - that it can never put a whole library on screen: browsing is 48 maps a page,
    search results and the folder list are capped, the log keeps only its tail --
    all proved against an index of 5,000 maps
  - that it can only open a map the index lists, and only serves thumbnails
  - that every button starts exactly the command it should (sorting and undo need
    the confirmation box; a search description can't become part of a shell command)
  - that it writes nothing itself, and that the real Index, Search, Sort-preview,
    Sort and Undo buttons work end to end and leave files exactly as they should be

The end-to-end parts use the real CLIP model from the local cache (they never go
online), so run it the same way as the other tests, inside the project's venv:

    source venv/bin/activate
    python3 fgu_support_test_ui.py
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse

import numpy as np
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)
sys.argv = ["fgu_support_test_ui.py"]

import fgu_find_maps                                   # noqa: E402  (index writer, for the fake big index)
import fgu_ui as ui                                    # noqa: E402
from fgu_support_test_safety import WriteRecorder      # noqa: E402

failures = []


def check(condition, message):
    if condition:
        print(f"  ok   - {message}")
    else:
        print(f"  FAIL - {message}")
        failures.append(message)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class Client:
    """A browser stand-in: talks to the server over a real socket."""

    def __init__(self, server):
        self.port = server.ctx.port
        self.token = server.ctx.token
        self.cookie = None

    def request(self, method, path, body=None, headers=None, host=None, use_cookie=True):
        headers = dict(headers or {})
        headers["Host"] = host or f"127.0.0.1:{self.port}"
        if use_cookie and self.cookie:
            headers["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def json(self, method, path, body=None, **kwargs):
        status, _, raw = self.request(method, path, body, **kwargs)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {}

    def sign_in(self):
        status, headers, _ = self.request("GET", f"/?token={self.token}", use_cookie=False)
        self.cookie = headers.get("Set-Cookie", "").split(";")[0]
        return status

    def wait_for_job(self, timeout=420):
        end = time.time() + timeout
        while time.time() < end:
            _, job = self.json("GET", "/api/job?since=0")
            if job.get("status") != "running":
                return job
            time.sleep(0.4)
        raise AssertionError("the job did not finish in time")


def make_library(root, names_colours):
    """A small real library of flat-colour images. Returns {relative path: bytes}."""
    os.makedirs(root, exist_ok=True)
    originals = {}
    for rel, rgb in names_colours.items():
        full = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        Image.fromarray(np.full((120, 160, 3), rgb, dtype=np.uint8)).save(full)
        originals[rel] = read_bytes(full)
    return originals


def all_files(root):
    found = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            found.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
    return sorted(found)


# ---------------------------------------------------------------------------
def test_static():
    print("\n[static] the page can't be made to run file names as HTML; constants agree with the finder")
    check("innerHTML" not in ui.PAGE and "eval(" not in ui.PAGE and "document.write" not in ui.PAGE,
          "the page never inserts text as HTML (file names are untrusted) and never evals")
    check(ui.INDEX_DIRNAME == fgu_find_maps.INDEX_DIRNAME and ui.INDEX_FILENAME == fgu_find_maps.INDEX_FILENAME
          and ui.INDEX_FORMAT == fgu_find_maps.INDEX_FORMAT and ui.MODEL_ID == fgu_find_maps.MODEL_ID
          and ui.VALID_IMAGE_EXTS == fgu_find_maps.VALID_IMAGE_EXTS,
          "the index name, format, model and image types match fgu_find_maps.py")
    source = open(os.path.join(REPO_ROOT, "fgu_ui.py"), encoding="utf-8").read()
    check("shell=True" not in source and "os.system(" not in source, "no command is ever run through a shell")
    check('"127.0.0.1"' in source and '"0.0.0.0"' not in source, "it listens on this machine only")


def test_access_rules(server):
    print("\n[access] only the browser given the address, addressed as this machine, same-origin JSON")
    client = Client(server)
    status, _, _ = client.request("GET", "/", use_cookie=False)
    check(status == 403, f"the page is refused without the token (got {status})")
    status, _, _ = client.request("GET", "/?token=not-the-token", use_cookie=False)
    check(status == 403, f"a wrong token is refused (got {status})")
    status, _, _ = client.request("GET", "/api/status", use_cookie=False)
    check(status == 403, f"the API is refused without the cookie (got {status})")
    status, _, _ = client.request("GET", "/thumb/" + "a" * 20 + ".jpg?lib=/", use_cookie=False)
    check(status == 403, f"thumbnails are refused without the cookie (got {status})")

    check(client.sign_in() == 302 and client.cookie.startswith("fgu_token="), "the real address signs the browser in (cookie set)")
    status, headers, _ = client.request("GET", f"/?token={client.token}", use_cookie=False)
    check("HttpOnly" in headers.get("Set-Cookie", "") and "SameSite=Strict" in headers.get("Set-Cookie", ""),
          "the cookie is HttpOnly and SameSite=Strict")
    status, headers, body = client.request("GET", "/")
    check(status == 200 and b"Map tools" in body, "with the cookie the page loads")

    status, _, _ = client.request("GET", "/api/status", host="evil.example.com")
    check(status == 403, f"a request addressed to another name is refused (DNS rebinding) (got {status})")
    status, _, _ = client.request("GET", "/api/status", host=f"localhost:{client.port}")
    check(status == 200, f"localhost:<port> is accepted too (got {status})")

    status, _, _ = client.request("POST", "/api/stop", body={}, use_cookie=False)
    check(status == 403, f"a POST without the cookie is refused (got {status})")
    status, _, _ = client.request("POST", "/api/stop", body={}, headers={"Origin": "http://evil.example.com"})
    check(status == 403, f"a POST from another origin is refused (got {status})")
    status, _, _ = client.request("POST", "/api/stop", body={}, headers={"Content-Type": "text/plain"})
    check(status == 415, f"a POST that isn't JSON is refused (got {status})")
    status, _, _ = client.request("POST", "/api/stop", body={}, headers={"Origin": f"http://127.0.0.1:{client.port}"})
    check(status == 200, f"a same-origin JSON POST is accepted (got {status})")


def test_big_library_is_never_listed(server, tmp):
    print("\n[limits] a 5,000-map library is only ever shown a page at a time")
    lib = os.path.join(tmp, "big")
    os.makedirs(lib)
    zero = np.zeros(8, dtype=np.float32)
    entries = []
    for i in range(4200):
        entries.append((f"dungeons/map_{i:05d}.png", 1000 + i, 1_000_000 + i, f"{i:020x}.jpg", zero))
    for i in range(200):
        entries.append((f"top_{i:03d}.png", 1, 1, f"{i + 90000:020x}.jpg", zero))
    for i in range(600):                                   # more folders than the list will show
        entries.append((f"folder_{i:04d}/only.png", 1, 1, f"{i + 95000:020x}.jpg", zero))
    entries.sort(key=lambda e: e[0])
    fgu_find_maps.save_index(lib, entries)
    total = len(entries)

    client = Client(server)
    client.sign_in()
    q = urllib.parse.urlencode
    status, s = client.json("GET", "/api/status?" + q({"lib": lib}))
    check(status == 200 and s["library"]["indexed"] == total, f"the status reports the count, not a list ({s['library']['indexed']} maps)")
    check(len(json.dumps(s)) < 2000, "...and the status reply is tiny")

    status, f = client.json("GET", "/api/folders?" + q({"lib": lib}))
    check(len(f["folders"]) == ui.MAX_FOLDERS and f["more"] == 602 - ui.MAX_FOLDERS,
          f"the folder list is capped at {ui.MAX_FOLDERS} of 602 folders, with a 'more' count (showed {len(f['folders'])}, {f['more']} more)")
    check(f["folders"][0]["name"] == "dungeons" and f["folders"][0]["count"] == 4200, "folders come with their counts, biggest first")

    status, page = client.json("GET", "/api/browse?" + q({"lib": lib, "folder": "dungeons", "page": 0}))
    check(status == 200 and len(page["items"]) == ui.PAGE_SIZE == 48, f"a page of 4,200 maps holds exactly {ui.PAGE_SIZE}")
    check(page["pages"] == 88 and page["total"] == 4200, "it says how many pages and maps there are")
    status, page = client.json("GET", "/api/browse?" + q({"lib": lib, "folder": "dungeons", "page": 99999, "size": 5000}))
    check(len(page["items"]) <= ui.PAGE_SIZE and page["page"] == 87, "an out-of-range page is clamped, and a requested page size is ignored")
    check(page["items"][0]["rel"].startswith("dungeons/") and "thumb" in page["items"][0], "items carry the map's relative path and thumbnail name only")
    status, _ = client.json("GET", "/api/browse?" + q({"lib": lib, "folder": "nope", "page": 0}))
    check(status == 400, f"an unknown folder is an error, not an empty listing (got {status})")
    status, page = client.json("GET", "/api/browse?" + q({"lib": lib, "folder": "", "page": 3}))
    check(status == 200 and page["total"] == 200 and len(page["items"]) == 48 and page["pages"] == 5,
          "the top level of the library pages the same way (200 maps = 5 pages of 48)")

    title, argv = ui.build_command("search", lib, {"query": "tavern", "top": 100000})
    check(argv[argv.index("--top") + 1] == str(ui.MAX_SEARCH_RESULTS), f"a search can ask for at most {ui.MAX_SEARCH_RESULTS} results")
    check("--no-page" in argv, "...and it never writes a page that would list them all")



def test_commands():
    print("\n[commands] each button starts exactly the right command, and risky ones need the tick box")
    lib = os.path.join(tempfile.gettempdir(), "some maps")
    os.makedirs(lib, exist_ok=True)

    def refused(action, options):
        try:
            ui.build_command(action, lib, options)
            return False
        except ValueError:
            return True

    _, preview = ui.build_command("sort_preview", lib, {})
    check("--no-move" in preview and "-y" not in preview and "--move-to" not in preview, "Sort: preview moves nothing (--no-move, no -y)")
    check("--report" in preview, "...and writes a report")
    check(refused("sort", {}) and refused("sort", {"confirm": "yes"}) and refused("sort", {"confirm": 1}),
          "Sort refuses unless confirm is exactly true")
    _, sort = ui.build_command("sort", lib, {"confirm": True})
    check("-y" in sort and sort[sort.index("--move-to") + 1] == lib and "--no-update-check" in sort,
          "Sort moves inside the chosen folder only, after confirmation, offline")
    _, photos = ui.build_command("sort_preview", lib, {"detect_photos": True})
    check("--detect-photos" in photos, "the photo option is passed through")

    check(refused("undo", {"manifest": "fgu_moves_2026-01-01_000000.jsonl"}), "Undo refuses without the tick box")
    check(refused("undo", {"manifest": "fgu_moves_2026-01-01_000000.jsonl", "confirm": True}), "Undo refuses a file that isn't in the folder")
    check(refused("undo", {"manifest": "../fgu_moves_2026-01-01_000000.jsonl", "confirm": True})
          and refused("undo", {"manifest": "other.jsonl", "confirm": True}), "Undo refuses paths and names that aren't undo files")
    with open(os.path.join(lib, "fgu_moves_2026-01-01_000000.jsonl"), "w") as f:
        f.write("")
    try:
        _, undo = ui.build_command("undo", lib, {"manifest": "fgu_moves_2026-01-01_000000.jsonl", "confirm": True})
        check("--undo" in undo and "-y" in undo, "Undo with a real undo file and the tick box runs --undo")
    finally:
        os.remove(os.path.join(lib, "fgu_moves_2026-01-01_000000.jsonl"))

    _, grid = ui.build_command("grid", lib, {})
    check("--force" not in grid, "Grids don't overwrite by default")
    check(refused("grid", {"force": True}), "Replacing grid files needs the tick box")
    _, forced = ui.build_command("grid", lib, {"force": True, "confirm": True})
    check("--force" in forced, "...and with it, --force is passed")

    nasty = 'x"; rm -rf ~; echo "'
    _, argv = ui.build_command("search", lib, {"query": nasty})
    check(argv.count(nasty) == 1 and all(isinstance(a, str) for a in argv), "a search description stays one argument, never part of a command line")
    check(refused("search", {"query": "   "}) and refused("search", {"query": "x" * 1000}), "an empty or huge description is refused")
    check(refused("rm -rf", {}), "an unknown action is refused")


def test_thumbs_and_open(server, tmp):
    print("\n[files] only the index's own thumbnails are served; only indexed maps can be opened")
    lib = os.path.join(tmp, "files")
    originals = make_library(lib, {"a.png": (200, 30, 30), "sub/b.png": (30, 30, 200)})
    thumbs = os.path.join(lib, ui.INDEX_DIRNAME, "thumbs")
    os.makedirs(thumbs)
    good = "0123456789abcdef0123.jpg"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(os.path.join(thumbs, good))
    secret = os.path.join(tmp, "secret.txt")
    with open(secret, "w") as f:
        f.write("not for the web page")
    entries = [("a.png", 1, 1, good, np.zeros(8, np.float32)), ("sub/b.png", 1, 1, "f" * 20 + ".jpg", np.zeros(8, np.float32)),
               ("ghost.png", 1, 1, "e" * 20 + ".jpg", np.zeros(8, np.float32))]
    fgu_find_maps.save_index(lib, entries)

    client = Client(server)
    client.sign_in()
    q = urllib.parse.urlencode
    status, headers, body = client.request("GET", f"/thumb/{good}?" + q({"lib": lib}))
    check(status == 200 and headers.get("Content-Type") == "image/jpeg" and body[:2] == b"\xff\xd8", "a real thumbnail is served as a JPEG")
    for name in ("../../../secret.txt", "..%2F..%2Fsecret.txt", "secret.txt", "a.png", good.replace(".jpg", ".png"), "x" * 20 + ".jpg"):
        status, _, body = client.request("GET", f"/thumb/{name}?" + q({"lib": lib}))
        check(status == 404 and b"not for the web page" not in body, f"/thumb/{name} is not served (got {status})")

    opened = []
    real_open = ui.open_with_default_app
    ui.open_with_default_app = opened.append
    try:
        status, _ = client.json("POST", "/api/open", {"lib": lib, "rel": "a.png"})
        check(status == 200 and opened == [os.path.join(lib, "a.png")], "an indexed map is opened with the computer's default app")
        for bad in ("../secret.txt", "..\\secret.txt", "../files/a.png", "/etc/hosts", "not_in_index.png", "ghost.png"):
            before = len(opened)
            status, _ = client.json("POST", "/api/open", {"lib": lib, "rel": bad})
            check(status == 400 and len(opened) == before, f"opening {bad!r} is refused (got {status})")
    finally:
        ui.open_with_default_app = real_open
    check(read_bytes(os.path.join(lib, "a.png")) == originals["a.png"], "the map itself was not touched")


def test_jobs(server):
    print("\n[jobs] one at a time, stoppable, and the log can't grow without bound")
    client = Client(server)
    client.sign_in()
    py = sys.executable
    check(server.ctx.start_job("long", [py, "-c", "import time; time.sleep(60)"]), "a job starts")
    status, reply = client.json("POST", "/api/run", {"action": "download_model", "options": {}})
    check(status == 409, f"a second job is refused while one runs (got {status})")
    status, job = client.json("GET", "/api/status")
    check(job["job"]["status"] == "running", "the status says a job is running")
    client.json("POST", "/api/stop", {})
    job = client.wait_for_job(30)
    check(job["status"] == "stopped", f"Stop ends it (status: {job['status']})")

    check(server.ctx.start_job("noisy", [py, "-c", "for i in range(5000): print(i)"]), "a noisy job starts")
    job = client.wait_for_job(60)
    status, tail = client.json("GET", "/api/job?since=0")
    check(job["status"] == "done" and tail["next"] == 5000, f"all 5,000 lines were produced (next={tail['next']})")
    check(len(tail["lines"]) <= ui.MAX_LOG_LINES and tail["lines"][-1] == "4999", f"only the last {ui.MAX_LOG_LINES} are kept (got {len(tail['lines'])}, last {tail['lines'][-1]})")
    status, later = client.json("GET", "/api/job?since=4990")
    check(later["lines"] == [str(n) for n in range(4990, 5000)], "asking for new lines since a point gives exactly those")

    server.ctx.start_job("fails", [py, "-c", "import sys; print('boom'); sys.exit(3)"])
    job = client.wait_for_job(30)
    check(job["status"] == "failed" and job["returncode"] == 3, "a failing command is reported as failed")


def test_real_buttons(server, lib, originals):
    print("\n[end to end] Index, Search, Sort preview, Sort and Undo, through the page's own API")
    client = Client(server)
    client.sign_in()
    q = urllib.parse.urlencode

    status, _ = client.json("POST", "/api/run", {"action": "index", "lib": lib, "options": {}})
    job = client.wait_for_job()
    status, s = client.json("GET", "/api/status?" + q({"lib": lib}))
    check(job["status"] == "done" and s["library"]["indexed"] == 3, f"Index builds an index of the 3 maps ({job['status']}, {s['library']['indexed']})")

    status, _ = client.json("POST", "/api/run", {"action": "search", "lib": lib, "options": {"query": "a solid red image", "top": 2}})
    job = client.wait_for_job()
    results = job.get("results") or []
    check(job["status"] == "done" and len(results) == 2, f"Search returns the 2 results asked for ({len(results)})")
    check(results and results[0]["rel"] == "red.png" and {"rel", "thumb", "score"} <= set(results[0]), "...best first, with the fields the page shows")
    check(not [f for f in all_files(lib) if f.endswith(".html")], "...and wrote no picture page")

    # Sort preview: moves nothing, writes a report.
    status, _ = client.json("POST", "/api/run", {"action": "sort_preview", "lib": lib, "options": {}})
    job = client.wait_for_job()
    reports = [f for f in all_files(lib) if f.startswith("fgu_report_") and f.endswith(".csv")]
    check(job["status"] == "done" and len(reports) == 1, f"Sort preview finished and wrote one report ({job['status']}, {reports})")
    check(all(read_bytes(os.path.join(lib, rel)) == b for rel, b in originals.items()), "...and every map is still where it was, unchanged")

    # Sort for real: needs the tick box.
    status, reply = client.json("POST", "/api/run", {"action": "sort", "lib": lib, "options": {}})
    check(status == 400 and sorted(f for f in all_files(lib) if "/" not in f and f.endswith(".png")) == sorted(originals),
          f"Sort without the tick box is refused and nothing moves (got {status})")
    status, _ = client.json("POST", "/api/run", {"action": "sort", "lib": lib, "options": {"confirm": True}})
    job = client.wait_for_job()
    left_in_place = [rel for rel in originals if os.path.exists(os.path.join(lib, rel))]
    manifests = [f for f in all_files(lib) if f.startswith("fgu_moves_")]
    check(job["status"] == "done" and len(manifests) == 1, f"Sort finished and saved an undo file ({job['status']}, {manifests})")
    check(len(left_in_place) < len(originals), "...and the maps moved into category folders")
    moved_files = [f for f in all_files(lib) if f.endswith(".png") and "/" in f and ui.INDEX_DIRNAME not in f]
    check(len(moved_files) == len(originals), f"...all of them, none lost or duplicated ({len(moved_files)} of {len(originals)})")

    status, s = client.json("GET", "/api/status?" + q({"lib": lib}))
    check(s["library"]["undo_files"] == manifests, "the status offers that undo file")
    status, _ = client.json("POST", "/api/run", {"action": "undo", "lib": lib, "options": {"manifest": manifests[0]}})
    check(status == 400, f"Undo without the tick box is refused (got {status})")
    status, _ = client.json("POST", "/api/run", {"action": "undo", "lib": lib, "options": {"manifest": manifests[0], "confirm": True}})
    job = client.wait_for_job()
    check(job["status"] == "done", f"Undo finished ({job['status']})")
    check(all(read_bytes(os.path.join(lib, rel)) == b for rel, b in originals.items() if os.path.exists(os.path.join(lib, rel))),
          "every map is back, byte for byte")
    check(all(os.path.exists(os.path.join(lib, rel)) for rel in originals), "...at its original place")


def main():
    test_static()
    test_commands()
    server = ui.make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            test_access_rules(server)
            test_big_library_is_never_listed(server, tmp)
            test_thumbs_and_open(server, tmp)
            test_jobs(server)
            # The page's own process must write nothing: only the commands it starts may.
            lib = os.path.join(tmp, "real")
            originals = make_library(lib, {"red.png": (220, 30, 30), "green.png": (30, 170, 60), "blue.png": (30, 60, 220)})
            with WriteRecorder() as recorder:
                test_real_buttons(server, lib, originals)
            check(recorder.touched == [], f"fgu_ui.py itself wrote nothing (wrote: {recorder.touched})")
    finally:
        server.shutdown()
        server.server_close()

    print()
    if failures:
        print(f"❌ {len(failures)} check(s) failed:")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print("✅ All UI checks passed.")


if __name__ == "__main__":
    main()
