#!/usr/bin/env python3
"""
Cheap whole-repo consistency checks. No dependencies, takes about a second, and
is the first thing CI runs. Every check here exists because the thing it catches
has actually gone wrong in this repo (or came very close):

  - a launcher pointing at a script that was renamed away (the Mac classifier
    launcher was broken for a while after a rename)
  - the README naming files that don't exist
  - a .command / .sh file without its executable bit, so double-clicking does nothing
  - run_windows.ps1 picking up a non-ASCII character (Windows PowerShell 5.1 reads
    files without a BOM as ANSI, so a smart quote or emoji can break parsing)
  - a .bat file losing its CRLF line endings (cmd.exe can misparse LF-only files)
  - a .bat file asking run_windows.ps1 for a tool it doesn't know
  - a script that no longer parses

Run it from anywhere:

    python3 fgu_support_check_repo.py

It only reads files; it changes nothing. Exits 1 if any check fails.
"""
import ast
import os
import re
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(condition, message):
    if condition:
        print(f"  ok   - {message}")
    else:
        print(f"  FAIL - {message}")
        failures.append(message)


def read_bytes(path):
    with open(os.path.join(REPO, path), "rb") as f:
        return f.read()


def read_text(path):
    return read_bytes(path).decode("utf-8")


def tracked_files():
    """{path: git file mode} for every file git tracks, e.g. {'run.command': '100755'}."""
    out = subprocess.run(["git", "ls-files", "-s"], cwd=REPO, capture_output=True, text=True, check=True).stdout
    files = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        files[path] = meta.split()[0]
    return files


files = tracked_files()
launchers = sorted(p for p in files if p.endswith(".command"))
shell_scripts = sorted(p for p in files if p.endswith((".command", ".sh")))
bat_files = sorted(p for p in files if p.endswith(".bat"))
ps1_files = sorted(p for p in files if p.endswith(".ps1"))
py_files = sorted(p for p in files if p.endswith(".py"))

print("\n[README] every script it mentions exists")
for name in sorted(set(re.findall(r"\b([\w\-]+\.(?:py|sh|command|bat|ps1))\b", read_text("README.md")))):
    check(name in files, name)

print("\n[launchers] each one runs a script that exists")
for path in launchers:
    scripts = re.findall(r"^\s*python3?\s+([\w\-]+\.py)\b", read_text(path), re.M)
    check(bool(scripts), f"{path} runs a python script (found {scripts})")
    for script in scripts:
        check(script in files, f"{path} -> {script}")
for path in ps1_files:
    scripts = re.findall(r"Script\s*=\s*'([\w\-]+\.py)'", read_text(path))
    for script in scripts:
        check(script in files, f"{path} -> {script}")

print("\n[requirements] every requirements file that is named exists, and every package is a bounded range")
requirement_files = sorted(p for p in files if re.fullmatch(r"requirements[\w\-]*\.txt", p))
for path in launchers + ps1_files + [p for p in files if p.endswith(".yml")]:
    text = read_text(path)
    for name in sorted(set(re.findall(r"\b(requirements[\w\-]*\.txt)\b", text))):
        check(name in files, f"{path} -> {name}")
for path in requirement_files:
    for line in read_text(path).splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            check(">=" in line and "<" in line, f"{path}: '{line}' has both a lower and an upper bound")

print("\n[Mac/shell] executable, with a shebang and LF line endings")
for path in shell_scripts:
    check(files[path] == "100755", f"{path} is executable in git (mode {files[path]})")
    check(read_bytes(path).startswith(b"#!"), f"{path} starts with a shebang")
    check(b"\r" not in read_bytes(path), f"{path} has no CR characters")

print("\n[Windows] .ps1 is pure ASCII; .bat files are CRLF and call a tool the .ps1 knows")
for path in ps1_files:
    try:
        read_bytes(path).decode("ascii")
        ascii_ok = True
    except UnicodeDecodeError as e:
        ascii_ok = False
        print(f"         first non-ASCII byte at offset {e.start}")
    check(ascii_ok, f"{path} is pure ASCII")
known_tools = set()
for path in ps1_files:
    m = re.search(r"ValidateSet\(([^)]*)\)", read_text(path))
    if m:
        known_tools |= set(re.findall(r"'([^']+)'", m.group(1)))
for path in bat_files:
    data = read_bytes(path)
    check(b"\n" not in data.replace(b"\r\n", b""), f"{path} uses CRLF line endings throughout")
    m = re.search(r'-File "%~dp0([\w\-]+\.ps1)" -Tool (\w+)', data.decode("utf-8"))
    check(bool(m), f"{path} starts a .ps1 with -Tool")
    if m:
        check(m.group(1) in files, f"{path} -> {m.group(1)} exists")
        check(m.group(2) in known_tools, f"{path} asks for tool '{m.group(2)}' (known: {sorted(known_tools)})")

print("\n[syntax] every script parses")
for path in py_files:
    try:
        ast.parse(read_text(path), path)
        parsed = True
    except SyntaxError as e:
        parsed = False
        print(f"         {e}")
    check(parsed, path)
if shutil.which("bash"):
    for path in shell_scripts:
        # -n only parses; it never runs the script (fgu_fix_macos.sh deletes data).
        result = subprocess.run(["bash", "-n", os.path.join(REPO, path)], capture_output=True, text=True)
        check(result.returncode == 0, f"{path} (bash -n){': ' + result.stderr.strip() if result.returncode else ''}")
else:
    print("  skip - bash not found, shell scripts not syntax-checked")

print()
if failures:
    print(f"{len(failures)} repo check(s) failed:")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("All repo checks passed.")
