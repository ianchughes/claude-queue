# Requires Windows Python. Double-click from Desktop. Connects to remote server via SSH.
# Project Command Centre — see docs/superpowers/specs/2026-03-24-project-command-centre-design.md

import json
import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError
import base64

# --- Constants ---

HOME = Path(os.path.expanduser("~"))
SCAN_ROOT = HOME
REMOTE_HOST = "ian@204.168.159.151"
REMOTE_ALIAS = "ian@IansCloudServer"  # preferred if SSH config has the alias
REMOTE_BASE = "/home/ian"
REMOTE_PROJECTS = f"{REMOTE_BASE}/projects"
CONFIG_PATH = HOME / ".project-dashboard.json"

# Manifest + launch instructions live in OneDrive so both machines share them.
# Respects the %OneDrive% env var; falls back to the default ~/OneDrive layout.
ONEDRIVE = Path(os.environ.get("OneDrive") or (HOME / "OneDrive"))
PORTAL_CONFIG_DIR = ONEDRIVE / "project-dashboard"
MANIFEST_PATH = PORTAL_CONFIG_DIR / "projects.manifest.json"
LAUNCH_INSTRUCTIONS_PATH = PORTAL_CONFIG_DIR / "portal-launch-instructions.md"

# Statuses hidden from the portal entirely (Rule 2).
HIDDEN_STATUSES = {"archive", "infra"}
# cwd values the portal refuses to launch into (Rule 1 — the "never /home/ian" guard).
UNSAFE_CWDS = {"", "/", "/home", "/home/ian", "/root"}

NAVY = "#1A2B4A"
GOLD = "#B8933A"
GOLD_HOVER = "#D4AD5A"
GOLD_DIM = "#5C4A1D"
WHITE = "#FFFFFF"
DARK_BG = "#0F1A2E"
ROW_BG = "#142236"
ROW_SELECTED = "#1E3454"
GREEN = "#4CAF50"
GREY = "#8899AA"
DIVIDER = "#2A3F5F"
RED_MUTED = "#C0392B"

FONT_BODY = ("Calibri", 11)
FONT_HEADING = ("Calibri", 13, "bold")
FONT_SUBHEADING = ("Calibri", 11, "bold")
FONT_SMALL = ("Calibri", 9)
FONT_TODO = ("Calibri", 11)
FONT_CHECK = ("Segoe UI Symbol", 13)

EXCLUDED_DIRS = {
    "appdata", "documents", "downloads", "desktop", "music", "pictures",
    "videos", "favorites", "links", "contacts", "searches", "sendto",
    "templates", "printhood", "nethood", "recent", "start menu",
    "node_modules", "processed_data", "rich_sample", "discovery_output",
    "everything-claude-code", "ecc-install-tmp", ".claude", "3d objects",
    "saved games", "application data", "local settings", "my documents",
    "ntuser.dat", "intelgraphicsprofiles", "conversion_analysis",
    "functions", "dataconnect", "docs", "tasks", "airweave",
}

PROJECT_MARKERS = ["CLAUDE.md", "TODOS.md"]
TODO_MARKERS = [os.path.join("tasks", "todo.md"), "TODOS.md"]


# --- Config ---

def load_config():
    """Load persistent dashboard config."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config):
    """Save persistent dashboard config."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass


# --- Manifest (Rules 1–4) ---

def load_manifest():
    """Load the projects manifest keyed by project name. {} if missing/invalid."""
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for entry in data.get("projects", []):
        name = entry.get("name")
        if name:
            out[name] = entry
    return out


def validate_cwd(cwd):
    """Enforce Rule 1. Raises ValueError for unsafe cwds like /home/ian."""
    if not cwd or cwd.rstrip("/") in {c.rstrip("/") for c in UNSAFE_CWDS}:
        raise ValueError(f"Refusing to launch with unsafe cwd: {cwd!r}")
    return cwd


def shell_quote(text):
    """Bash single-quote a string, including embedded single quotes."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def build_system_prompt(entry):
    """Rule 4 — build the --append-system-prompt payload from a manifest entry."""
    name = entry.get("name", "unknown")
    cwd = entry.get("cwd", "unknown")
    kind = entry.get("kind") or "project"
    owns = entry.get("owns") or "this project"
    port = entry.get("port")
    pm2 = entry.get("pm2")

    parens = []
    if port:
        parens.append(f"port {port}")
    if pm2:
        parens.append(f"PM2 {pm2}")
    suffix = f" ({', '.join(parens)})" if parens else ""
    desc = f"This is the {kind}{suffix}."

    reference_line = ""
    if entry.get("status") == "reference":
        reference_line = (
            " This project is READ-ONLY reference material — do not modify files. "
        )

    prompt = (
        f"You have been launched by the Project Dashboard for work in the "
        f"{name} project. Current working directory: {cwd}. {desc} "
        f"Owns: {owns}.{reference_line} "
        f"Do NOT cd out. If the task requires touching another project, stop "
        f"and tell the user — a separate session should be launched."
    )
    # The prompt travels through cmd.exe -> ssh -> remote bash. cmd.exe's
    # double-quote wrapping breaks if the payload contains literal ". Normalise.
    return prompt.replace('"', "'")


# --- Data Layer ---

def is_excluded(dirname):
    low = dirname.lower()
    if low.startswith(".") or low.startswith("ntuser"):
        return True
    if low.startswith("onedrive"):
        return True
    return low in EXCLUDED_DIRS


def parse_markdown_todos(filepath):
    """Parse - [ ] and - [x] lines from a markdown file."""
    todos = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                m_open = re.match(r"^- \[ \]\s+(.+)$", line)
                m_done = re.match(r"^- \[x\]\s+(.+)$", line, re.IGNORECASE)
                if m_open:
                    todos.append({"text": m_open.group(1), "done": False, "source": "md"})
                elif m_done:
                    todos.append({"text": m_done.group(1), "done": True, "source": "md"})
    except (OSError, UnicodeDecodeError):
        pass
    return todos


def _ssh_read(remote_path):
    """Read a file from the remote server via SSH."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             REMOTE_HOST, f"cat '{remote_path}'"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        )
        if result.returncode == 0:
            return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _ssh_run(command, timeout=15):
    """Run a command on the remote server via SSH."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             REMOTE_HOST, command],
            capture_output=True, text=True, timeout=timeout,
            creationflags=0x08000000,
        )
        return result
    except (OSError, subprocess.TimeoutExpired):
        return None


def load_code_queue():
    """Load Code Queue from remote server."""
    raw = _ssh_read(f"{REMOTE_BASE}/.claude/cache/cq-local.json")
    if raw:
        try:
            data = json.loads(raw)
            return data.get("data", {}).get("projects", {})
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def normalise_text(text):
    return re.sub(r"^-\s*\[.\]\s*", "", text.strip()).lower().strip()


def merge_todos(md_todos, cq_todos):
    """Merge markdown and Code Queue todos, deduplicating by normalised text."""
    seen = set()
    merged = []

    # Code Queue first (preferred on collision)
    for t in cq_todos:
        key = normalise_text(t["text"])
        if key not in seen:
            seen.add(key)
            merged.append(t)

    for t in md_todos:
        key = normalise_text(t["text"])
        if key not in seen:
            seen.add(key)
            merged.append(t)

    return merged


def _cq_todos_for(name, cq_data):
    """Return normalised Code Queue todos for a given project name."""
    return [
        {
            "text": t.get("text", ""),
            "done": t.get("status") == "done",
            "source": "cq",
        }
        for t in cq_data.get(name, [])
        if t.get("text")
    ]


def _has_local_markers(local_dir):
    return any(
        (local_dir / m).exists() for m in (PROJECT_MARKERS + TODO_MARKERS)
    )


def scan_projects():
    """Merge three sources into one list: manifest (authoritative),
    Windows FS scan, and Code Queue. Manifest decides visibility —
    archive/infra are hidden entirely."""
    projects = []
    cq_data = load_code_queue()
    manifest = load_manifest()
    seen = set()

    try:
        fs_entries = {e.name: e for e in SCAN_ROOT.iterdir() if e.is_dir()}
    except OSError:
        fs_entries = {}

    # 1. Manifest entries — authoritative; hidden statuses drop out.
    for name, entry in manifest.items():
        if entry.get("status") in HIDDEN_STATUSES:
            seen.add(name)
            continue
        local_dir = fs_entries.get(name)
        if local_dir is not None and not _has_local_markers(local_dir):
            local_dir = None

        md_todos = []
        if local_dir is not None:
            for p in TODO_MARKERS:
                full = local_dir / p
                if full.exists():
                    md_todos.extend(parse_markdown_todos(full))
        all_todos = merge_todos(md_todos, _cq_todos_for(name, cq_data))
        open_count = sum(1 for t in all_todos if not t["done"])

        projects.append({
            "name": name,
            "path": str(local_dir) if local_dir is not None else entry.get("cwd", ""),
            "todos": all_todos,
            "open_count": open_count,
            "remote_only": local_dir is None,
            "manifest": entry,
            "status": entry.get("status", "active"),
        })
        seen.add(name)

    # 2. Windows FS scan — unclassified projects not in the manifest.
    for name, local_dir in fs_entries.items():
        if name in seen or is_excluded(name):
            continue
        if not _has_local_markers(local_dir):
            continue
        md_todos = []
        for p in TODO_MARKERS:
            full = local_dir / p
            if full.exists():
                md_todos.extend(parse_markdown_todos(full))
        all_todos = merge_todos(md_todos, _cq_todos_for(name, cq_data))
        open_count = sum(1 for t in all_todos if not t["done"])
        projects.append({
            "name": name,
            "path": str(local_dir),
            "todos": all_todos,
            "open_count": open_count,
            "remote_only": False,
            "manifest": {},
            "status": "unclassified",
        })
        seen.add(name)

    # 3. Code Queue extras — remote-only projects not in manifest and not local.
    for name in cq_data.keys():
        if name in seen or is_excluded(name):
            continue
        cq_todos = _cq_todos_for(name, cq_data)
        open_count = sum(1 for t in cq_todos if not t["done"])
        projects.append({
            "name": name,
            "path": str(SCAN_ROOT / name),
            "todos": cq_todos,
            "open_count": open_count,
            "remote_only": True,
            "manifest": {},
            "status": "unclassified",
        })

    status_rank = {"active": 0, "unclassified": 1, "reference": 2}
    projects.sort(key=lambda p: (
        status_rank.get(p["status"], 3),
        -p["open_count"],
        p["name"],
    ))
    return projects


# --- Launch ---

def find_wt():
    """Find Windows Terminal executable."""
    wt = shutil.which("wt.exe")
    if wt:
        return wt
    local_apps = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe")
    if os.path.isfile(local_apps):
        return local_apps
    return None


def load_session_summary(project_name):
    """Load the latest session summary for a project from remote server."""
    raw = _ssh_read(f"{REMOTE_BASE}/.claude/sessions/{project_name}/latest.json")
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def format_session_age(timestamp_str):
    """Format a timestamp as a human-readable age string."""
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - ts
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return "less than an hour ago"
        if hours < 24:
            return f"{int(hours)} hour{'s' if int(hours) != 1 else ''} ago"
        days = int(hours / 24)
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days} days ago"
        return ts.strftime("%d %b %Y")
    except (ValueError, TypeError):
        return "unknown time"


def resolve_remote_path(project_name):
    """Resolve the remote path for a project, preferring projects/ over home."""
    projects_path = f"{REMOTE_PROJECTS}/{project_name}"
    home_path = f"{REMOTE_BASE}/{project_name}"
    result = _ssh_run(f"test -d '{projects_path}' && echo projects || (test -d '{home_path}' && echo home || echo none)")
    if result and result.stdout.strip() == "projects":
        return projects_path
    if result and result.stdout.strip() == "home":
        return home_path
    return home_path


def _windows_to_wsl_path(win_path):
    """Convert a Windows path to its WSL equivalent."""
    path = str(win_path).replace("\\", "/")
    if len(path) >= 2 and path[1] == ":":
        drive = path[0].lower()
        path = f"/mnt/{drive}{path[2:]}"
    return path


def _launch_in_terminal(title, cmd_args):
    """Launch a command in Windows Terminal (or fallback)."""
    wt = find_wt()
    if wt:
        cmd = [wt, "--title", title] + cmd_args
    else:
        cmd = ["cmd.exe", "/c", "start"] + cmd_args

    try:
        subprocess.Popen(cmd, creationflags=0x00000008)  # DETACHED_PROCESS
    except OSError as e:
        messagebox.showerror("Launch Error", f"Could not launch:\n{e}")


def check_remote_project_exists(project_name):
    """Check if a project directory exists on the remote server."""
    result = _ssh_run(f"test -d '{REMOTE_BASE}/{project_name}' -o -d '{REMOTE_PROJECTS}/{project_name}' && echo yes || echo no")
    return result and result.stdout.strip() == "yes"


def confirm_reference_launch(project_name):
    """Rule 3 — warn before launching a read-only reference project."""
    return messagebox.askokcancel(
        "Reference project",
        (
            f"'{project_name}' is a read-only reference project.\n\n"
            "Claude will be told not to modify files here. Continue?"
        ),
        icon=messagebox.WARNING,
    )


def _claude_command(entry, mode):
    """Build the 'claude ...' portion of the launch command for a manifest entry.

    Applies Rule 4 — appends the per-project system prompt. For fresh
    sessions we also seed a first-message prompt; for --continue we do not
    (sending a new prompt against a resumed session would change the flow).
    """
    parts = ["claude"]
    if mode == "resume":
        parts.append("--continue")
    if entry:
        parts.append("--append-system-prompt")
        parts.append(shell_quote(build_system_prompt(entry)))
    if mode != "resume":
        parts.append(shell_quote(
            "Review the todos for this project and help me work through them"
        ))
    return " ".join(parts)


def launch_claude_remote(project_name, mode="ask"):
    """Launch Claude Code on the remote server via SSH.

    mode: "ask" (check for session, prompt user), "fresh", or "resume"
    """
    entry = load_manifest().get(project_name, {})
    cwd = entry.get("cwd") or resolve_remote_path(project_name)
    try:
        validate_cwd(cwd)
    except ValueError as e:
        messagebox.showerror("Unsafe launch", str(e))
        return

    if entry.get("status") == "reference" and not confirm_reference_launch(project_name):
        return

    if mode == "ask":
        session = load_session_summary(project_name)
        if session and session.get("summary"):
            mode = show_resume_dialog(project_name, session)
            if mode is None:
                return
        else:
            mode = "fresh"

    claude_cmd = _claude_command(entry, mode)
    ssh_cmd = f"cd {shell_quote(cwd)} && {claude_cmd}"
    ssh_full = f'ssh -t {REMOTE_HOST} "{ssh_cmd}"'
    _launch_in_terminal(project_name, ["cmd.exe", "/c", ssh_full])


def launch_claude_local(project_path, project_name, mode="fresh"):
    """Launch Claude Code locally via WSL in the project directory.

    mode: "fresh" or "resume"
    """
    entry = load_manifest().get(project_name, {})
    if project_path and os.path.isdir(project_path):
        cwd = _windows_to_wsl_path(project_path)
    elif entry.get("cwd"):
        cwd = entry["cwd"]
    else:
        cwd = _windows_to_wsl_path(project_path)
    try:
        validate_cwd(cwd)
    except ValueError as e:
        messagebox.showerror("Unsafe launch", str(e))
        return

    if entry.get("status") == "reference" and not confirm_reference_launch(project_name):
        return

    claude_cmd = _claude_command(entry, mode)
    wsl_cmd = f"cd {shell_quote(cwd)} && {claude_cmd}"
    _launch_in_terminal(
        project_name,
        ["wsl.exe", "-d", "Ubuntu", "--", "bash", "-lc", wsl_cmd],
    )


def launch_blank_claude(location="local"):
    """Launch a blank Claude session with no project context."""
    if location == "remote":
        ssh_full = f"ssh -t {REMOTE_HOST} \"claude\""
        _launch_in_terminal("Claude (Remote)", ["cmd.exe", "/c", ssh_full])
    else:
        _launch_in_terminal("Claude (Local)", ["wsl.exe", "-e", "bash", "-ic", "claude"])


def show_resume_dialog(project_name, session):
    """Show a dialog asking whether to resume previous session or start fresh.

    Returns "resume", "fresh", or None (cancelled).
    """
    summary = session.get("summary", "No description available.")
    age = format_session_age(session.get("timestamp", ""))
    todos_done = session.get("todos_completed", 0)
    todos_added = session.get("todos_added", 0)

    detail_parts = []
    if todos_done:
        detail_parts.append(f"{todos_done} todo{'s' if todos_done != 1 else ''} completed")
    if todos_added:
        detail_parts.append(f"{todos_added} new todo{'s' if todos_added != 1 else ''} added")
    detail_line = f"  ({', '.join(detail_parts)})" if detail_parts else ""

    dialog = tk.Toplevel()
    dialog.title(f"Launch {project_name}")
    dialog.configure(bg=NAVY)
    dialog.geometry("480x280")
    dialog.resizable(False, False)
    dialog.transient()
    dialog.grab_set()

    # Centre on screen
    dialog.update_idletasks()
    x = (dialog.winfo_screenwidth() - 480) // 2
    y = (dialog.winfo_screenheight() - 280) // 2
    dialog.geometry(f"+{x}+{y}")

    result = {"choice": None}

    # Header
    tk.Label(dialog, text=f"Previous session found", font=FONT_HEADING,
             bg=NAVY, fg=GOLD).pack(pady=(20, 4))

    # Session info
    info_frame = tk.Frame(dialog, bg=DARK_BG, padx=16, pady=12)
    info_frame.pack(fill=tk.X, padx=20, pady=(4, 12))

    tk.Label(info_frame, text=summary, font=FONT_BODY, bg=DARK_BG, fg=WHITE,
             wraplength=420, justify=tk.LEFT).pack(anchor=tk.W)
    tk.Label(info_frame, text=f"{age}{detail_line}", font=FONT_SMALL,
             bg=DARK_BG, fg=GREY).pack(anchor=tk.W, pady=(4, 0))

    # Buttons
    btn_frame = tk.Frame(dialog, bg=NAVY)
    btn_frame.pack(pady=(0, 20))

    def pick(choice):
        result["choice"] = choice
        dialog.destroy()

    resume_btn = tk.Button(btn_frame, text="Resume Session", font=FONT_BODY,
                           bg=GOLD, fg=NAVY, activebackground=GOLD_HOVER,
                           activeforeground=NAVY, relief=tk.FLAT, padx=18, pady=6,
                           cursor="hand2", command=lambda: pick("resume"))
    resume_btn.pack(side=tk.LEFT, padx=8)
    resume_btn.bind("<Enter>", lambda e: resume_btn.config(bg=GOLD_HOVER))
    resume_btn.bind("<Leave>", lambda e: resume_btn.config(bg=GOLD))

    fresh_btn = tk.Button(btn_frame, text="Start Fresh", font=FONT_BODY,
                          bg=DIVIDER, fg=WHITE, activebackground=NAVY,
                          activeforeground=WHITE, relief=tk.FLAT, padx=18, pady=6,
                          cursor="hand2", command=lambda: pick("fresh"))
    fresh_btn.pack(side=tk.LEFT, padx=8)
    fresh_btn.bind("<Enter>", lambda e: fresh_btn.config(bg=NAVY))
    fresh_btn.bind("<Leave>", lambda e: fresh_btn.config(bg=DIVIDER))

    cancel_btn = tk.Button(btn_frame, text="Cancel", font=FONT_BODY,
                           bg=NAVY, fg=GREY, activebackground=NAVY,
                           activeforeground=WHITE, relief=tk.FLAT, padx=18, pady=6,
                           cursor="hand2", command=lambda: pick(None))
    cancel_btn.pack(side=tk.LEFT, padx=8)

    dialog.wait_window()
    return result["choice"]


# --- New Project ---

def read_cq_credentials():
    """Read Code Queue PAT and repo from remote server .env file."""
    raw = _ssh_read(f"{REMOTE_BASE}/.claude/.env")
    creds = {}
    if raw:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                creds[key.strip()] = val.strip()
    return creds.get("CODEQUEUE_PAT"), creds.get("CODEQUEUE_REPO", "ianchughes/claude-todos-data")


def infer_github_url(project_name):
    """Guess a GitHub clone URL from the Code Queue repo owner."""
    _, repo = read_cq_credentials()
    owner = "ianchughes"
    if repo and "/" in repo:
        owner = repo.split("/", 1)[0]
    return f"https://github.com/{owner}/{project_name}.git"


def clone_project(project_name, github_url):
    """Clone a GitHub repo into SCAN_ROOT/<project_name> via WSL git.

    If a Code Queue PAT is available and the URL is a github.com HTTPS URL,
    inject it for the clone so private repos work, then rewrite origin
    afterwards so the PAT doesn't persist in the repo config.

    Returns (success, message).
    """
    target = SCAN_ROOT / project_name
    if target.exists():
        return False, f"Directory already exists: {target}"

    pat, _ = read_cq_credentials()
    clone_url = github_url
    auth_used = False
    if pat and github_url.startswith("https://github.com/"):
        clone_url = github_url.replace("https://", f"https://{pat}@", 1)
        auth_used = True

    wsl_home = _windows_to_wsl_path(str(SCAN_ROOT))
    clone_cmd = f"cd '{wsl_home}' && git clone '{clone_url}' '{project_name}' 2>&1"

    try:
        result = subprocess.run(
            ["wsl.exe", "-e", "bash", "-ic", clone_cmd],
            capture_output=True, text=True, timeout=300,
            creationflags=0x08000000,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"Clone failed to run: {e}"

    if result.returncode != 0:
        err = (result.stdout or "") + (result.stderr or "")
        if pat:
            err = err.replace(pat, "<PAT>")
        return False, err.strip() or f"git exited with code {result.returncode}"

    if auth_used:
        wsl_target = _windows_to_wsl_path(str(target))
        reset_cmd = f"cd '{wsl_target}' && git remote set-url origin '{github_url}'"
        try:
            subprocess.run(
                ["wsl.exe", "-e", "bash", "-ic", reset_cmd],
                capture_output=True, text=True, timeout=15,
                creationflags=0x08000000,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    return True, "Cloned successfully"


def register_code_queue(project_name):
    """Add project to Code Queue via GitHub API."""
    pat, repo = read_cq_credentials()
    if not pat:
        return

    api_url = f"https://api.github.com/repos/{repo}/contents/todos.json"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "project-dashboard",
    }

    try:
        req = Request(api_url, headers=headers)
        with urlopen(req, timeout=10) as resp:
            meta = json.loads(resp.read().decode())

        file_sha = meta["sha"]
        content = json.loads(base64.b64decode(meta["content"]).decode())

        if project_name not in content.get("data", {}).get("projects", {}):
            content.setdefault("data", {}).setdefault("projects", {})[project_name] = []

            new_content = base64.b64encode(
                json.dumps(content, indent=2).encode()
            ).decode()

            put_data = json.dumps({
                "message": f"Add {project_name} from Project Dashboard",
                "content": new_content,
                "sha": file_sha,
            }).encode()

            put_req = Request(api_url, data=put_data, headers=headers, method="PUT")
            urlopen(put_req, timeout=10)
    except (OSError, URLError, json.JSONDecodeError, KeyError):
        pass  # Skip silently


PROJECT_TYPES = {
    "python": {
        "commands": (
            "## Commands\n\n"
            "```bash\n"
            "pip install -r requirements.txt        # Install dependencies\n"
            "pytest --cov                            # Run tests with coverage\n"
            "pytest tests/ -v                        # Verbose test output\n"
            "```\n\n"
            "## Pre-push Coverage Gate\n\n"
            "```bash\n"
            "pytest --cov=. --cov-fail-under=80\n"
            "```\n"
        ),
        "extra_files": {
            "requirements.txt": "pytest\npytest-cov\n",
            ".gitignore": (
                "__pycache__/\n*.pyc\n.pytest_cache/\n"
                "htmlcov/\n.coverage\n*.egg-info/\n"
                "dist/\nvenv/\n.env\n"
            ),
        },
        "extra_dirs": ["tests"],
        "extra_remote_cmd": "cd '{remote_project}' && python -m venv venv 2>/dev/null; true",
    },
    "nextjs": {
        "commands": (
            "## Commands\n\n"
            "```bash\n"
            "npm install          # Install dependencies\n"
            "npm run dev          # Development server\n"
            "npm run build        # Production build\n"
            "npx vitest run       # Run tests\n"
            "```\n\n"
            "## Pre-push Coverage Gate\n\n"
            "```bash\n"
            "npx vitest run --coverage\n"
            "```\n"
        ),
        "extra_files": {
            ".gitignore": (
                "node_modules/\n.next/\nout/\n"
                "coverage/\n.env\n.env.local\n"
            ),
        },
        "extra_dirs": ["tests"],
        "extra_remote_cmd": (
            "cd '{remote_project}' && "
            "npm init -y > /dev/null 2>&1 && "
            "npm install -D vitest @testing-library/react > /dev/null 2>&1; true"
        ),
    },
    "plain": {
        "commands": (
            "## Commands\n\n"
            "_Add build/run/test commands here._\n"
        ),
        "extra_files": {},
        "extra_dirs": [],
        "extra_remote_cmd": None,
    },
}


def create_new_project(name, project_type="plain", location="remote"):
    """Create project directory with skeleton files.

    location: "remote" (create on server, local stub for scanner) or "local" (local only).
    """
    project_dir = SCAN_ROOT / name
    if project_dir.exists():
        messagebox.showerror("Error", f"Directory '{name}' already exists.")
        return False

    type_config = PROJECT_TYPES.get(project_type, PROJECT_TYPES["plain"])
    display_name = name.replace("-", " ").title()

    claude_md = (
        f"# {display_name}\n\n"
        "## Overview\n\n"
        "_Describe this project._\n\n"
        + type_config["commands"]
    )
    todo_md = (
        f"# {display_name} — Todos\n\n"
        "## Outstanding\n\n"
        "- [ ] Define project scope\n\n"
        "## Completed\n"
    )

    if location == "local":
        # Full project on Windows FS
        tasks_dir = project_dir / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "todo.md").write_text(todo_md, encoding="utf-8")
        (project_dir / "CLAUDE.md").write_text(claude_md, encoding="utf-8")

        for extra_dir in type_config["extra_dirs"]:
            (project_dir / extra_dir).mkdir(parents=True, exist_ok=True)

        for filename, content in type_config["extra_files"].items():
            (project_dir / filename).write_text(content, encoding="utf-8")
    else:
        # Minimal local stub (so the scanner finds it)
        tasks_dir = project_dir / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "todo.md").write_text(todo_md, encoding="utf-8")
        (project_dir / "CLAUDE.md").write_text(
            f"# {display_name}\n\n"
            f"_This project lives on the remote server ({REMOTE_HOST})._\n",
            encoding="utf-8",
        )

        # Full project on remote server
        remote_project = f"{REMOTE_BASE}/{name}"
        extra_dirs_cmd = ""
        if type_config["extra_dirs"]:
            dirs = " ".join(f"'{remote_project}/{d}'" for d in type_config["extra_dirs"])
            extra_dirs_cmd = f" && mkdir -p {dirs}"

        extra_files_cmd = ""
        for filename, content in type_config["extra_files"].items():
            extra_files_cmd += (
                f" && cat > '{remote_project}/{filename}' << 'SKELETON_EOF'\n{content}SKELETON_EOF\n"
            )

        extra_setup = type_config.get("extra_remote_cmd")
        extra_setup_cmd = ""
        if extra_setup:
            extra_setup_cmd = " && " + extra_setup.format(remote_project=remote_project)

        _ssh_run(
            f"mkdir -p '{remote_project}/tasks'{extra_dirs_cmd} && "
            f"cat > '{remote_project}/CLAUDE.md' << 'SKELETON_EOF'\n{claude_md}SKELETON_EOF\n"
            f"cat > '{remote_project}/tasks/todo.md' << 'SKELETON_EOF'\n{todo_md}SKELETON_EOF"
            f"{extra_files_cmd}{extra_setup_cmd}",
            timeout=30,
        )

    register_code_queue(name)
    return True


# --- GUI ---

class ProjectDashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Project Command Centre")
        self.configure(bg=NAVY)
        self.minsize(820, 600)
        self.geometry("860x680")

        self.projects = []
        self.filtered_projects = []
        self.selected_project = None

        self._build_ui()
        self.refresh()
        self.bind("<F5>", lambda e: self.refresh())

    def _build_ui(self):
        # Status bar (pack first so it stays at bottom)
        self.status_var = tk.StringVar()
        status_bar = tk.Label(self, textvariable=self.status_var, font=FONT_SMALL,
                               bg=DARK_BG, fg=GREY, anchor=tk.W, padx=16, pady=4)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        # Top bar
        top = tk.Frame(self, bg=NAVY, pady=10, padx=16)
        top.pack(fill=tk.X)

        tk.Label(top, text="Project Command Centre", font=("Calibri", 16, "bold"),
                 bg=NAVY, fg=GOLD).pack(side=tk.LEFT)

        btn_frame = tk.Frame(top, bg=NAVY)
        btn_frame.pack(side=tk.RIGHT)

        self.btn_new = self._make_button(btn_frame, "+ New Project", self._on_new_project)
        self.btn_new.pack(side=tk.RIGHT, padx=(6, 0))

        self.btn_claude = self._make_button(btn_frame, "Claude", self._on_blank_claude)
        self.btn_claude.pack(side=tk.RIGHT, padx=(6, 0))

        self.btn_refresh = self._make_button(btn_frame, "Refresh", self.refresh)
        self.btn_refresh.pack(side=tk.RIGHT, padx=(6, 0))

        # Divider
        tk.Frame(self, bg=DIVIDER, height=1).pack(fill=tk.X)

        # Search bar
        search_frame = tk.Frame(self, bg=NAVY, padx=16, pady=8)
        search_frame.pack(fill=tk.X)

        self.search_var = tk.StringVar()
        search_entry = tk.Entry(search_frame, textvariable=self.search_var,
                                font=FONT_BODY, bg=DARK_BG, fg=WHITE,
                                insertbackground=WHITE, relief=tk.FLAT,
                                highlightthickness=1, highlightcolor=GOLD,
                                highlightbackground=DIVIDER)
        search_entry.pack(fill=tk.X, expand=True, ipady=5)
        self._add_placeholder(search_entry, "Filter projects...")
        self.search_var.trace_add("write", lambda *_: self._filter_projects())

        # Project list header
        list_header = tk.Frame(self, bg=NAVY, padx=16, pady=6)
        list_header.pack(fill=tk.X)
        tk.Label(list_header, text="PROJECTS", font=FONT_SUBHEADING,
                 bg=NAVY, fg=GREY).pack(side=tk.LEFT)
        self.project_count_label = tk.Label(list_header, text="", font=FONT_SMALL,
                                             bg=NAVY, fg=GREY)
        self.project_count_label.pack(side=tk.RIGHT)

        # Project list
        list_container = tk.Frame(self, bg=NAVY, padx=16)
        list_container.pack(fill=tk.BOTH, expand=True)

        self.project_canvas = tk.Canvas(list_container, bg=DARK_BG,
                                         highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(list_container, orient=tk.VERTICAL,
                                  command=self.project_canvas.yview,
                                  bg=DARK_BG, troughcolor=DARK_BG)
        self.project_inner = tk.Frame(self.project_canvas, bg=DARK_BG)

        self.project_inner.bind("<Configure>",
            lambda e: self.project_canvas.configure(scrollregion=self.project_canvas.bbox("all")))
        self.project_canvas.create_window((0, 0), window=self.project_inner, anchor=tk.NW)
        self.project_canvas.configure(yscrollcommand=scrollbar.set)

        self.project_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.project_canvas.bind_all("<MouseWheel>",
            lambda e: self.project_canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        # Divider
        tk.Frame(self, bg=GOLD_DIM, height=1).pack(fill=tk.X, padx=16)

        # Todo header + add bar in one row
        todo_header = tk.Frame(self, bg=NAVY, padx=16, pady=8)
        todo_header.pack(fill=tk.X)

        self.detail_label = tk.Label(todo_header, text="TODOS",
                                      font=FONT_SUBHEADING, bg=NAVY, fg=GREY)
        self.detail_label.pack(side=tk.LEFT)

        # Add button (right of header)
        self.btn_add_todo = self._make_button(todo_header, "+ Add", self._add_todo)
        self.btn_add_todo.pack(side=tk.RIGHT)

        # Add entry (between label and button)
        self.add_var = tk.StringVar()
        self.add_entry = tk.Entry(todo_header, textvariable=self.add_var,
                             font=FONT_BODY, bg=DARK_BG, fg=WHITE,
                             insertbackground=WHITE, relief=tk.FLAT,
                             highlightthickness=1, highlightcolor=GOLD,
                             highlightbackground=DIVIDER)
        self.add_entry.pack(side=tk.RIGHT, fill=tk.X, expand=True, ipady=4, padx=(12, 8))
        self._add_placeholder(self.add_entry, "Add a todo...")
        self.add_entry.bind("<Return>", lambda e: self._add_todo())

        # Todo list
        detail_container = tk.Frame(self, bg=NAVY, padx=16, pady=4)
        detail_container.pack(fill=tk.BOTH, expand=True)

        self.todo_canvas = tk.Canvas(detail_container, bg=DARK_BG,
                                      highlightthickness=0, bd=0)
        todo_scrollbar = tk.Scrollbar(detail_container, orient=tk.VERTICAL,
                                       command=self.todo_canvas.yview,
                                       bg=DARK_BG, troughcolor=DARK_BG)
        self.todo_inner = tk.Frame(self.todo_canvas, bg=DARK_BG)

        self.todo_inner.bind("<Configure>",
            lambda e: self.todo_canvas.configure(scrollregion=self.todo_canvas.bbox("all")))
        self.todo_canvas.create_window((0, 0), window=self.todo_inner, anchor=tk.NW)
        self.todo_canvas.configure(yscrollcommand=todo_scrollbar.set)

        self.todo_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        todo_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Show empty state
        self._show_empty_todos()

    def _make_button(self, parent, text, command):
        btn = tk.Button(parent, text=text, font=FONT_BODY, bg=GOLD, fg=NAVY,
                        activebackground=GOLD_HOVER, activeforeground=NAVY,
                        relief=tk.FLAT, padx=14, pady=4, cursor="hand2",
                        command=command)
        btn.bind("<Enter>", lambda e: btn.config(bg=GOLD_HOVER))
        btn.bind("<Leave>", lambda e: btn.config(bg=GOLD))
        return btn

    def refresh(self, *_):
        self.projects = scan_projects()
        self.search_var.set("")
        self._filter_projects()

    def _filter_projects(self):
        if not hasattr(self, "project_inner"):
            return
        query = self.search_var.get().lower()
        # Ignore placeholder text
        if query == "filter projects...":
            query = ""
        if query:
            self.filtered_projects = [p for p in self.projects if query in p["name"].lower()]
        else:
            self.filtered_projects = list(self.projects)
        self._render_project_list()
        self._update_status()

    def _render_project_list(self):
        for w in self.project_inner.winfo_children():
            w.destroy()

        for proj in self.filtered_projects:
            is_selected = (self.selected_project and
                           self.selected_project["name"] == proj["name"])
            bg = ROW_SELECTED if is_selected else ROW_BG

            # Outer frame for left accent
            outer = tk.Frame(self.project_inner, bg=DARK_BG)
            outer.pack(fill=tk.X, pady=1)

            # Gold left accent for selected
            accent_color = GOLD if is_selected else ROW_BG
            accent = tk.Frame(outer, bg=accent_color, width=3)
            accent.pack(side=tk.LEFT, fill=tk.Y)

            row = tk.Frame(outer, bg=bg, pady=3, padx=10)
            row.pack(side=tk.LEFT, fill=tk.X, expand=True)

            # Project name
            name_lbl = tk.Label(row, text=proj["name"], font=FONT_BODY,
                                bg=bg, fg=WHITE, anchor=tk.W, cursor="hand2")
            name_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

            # Todo badge
            total = len(proj["todos"])
            done = total - proj["open_count"]
            if proj["open_count"] > 0:
                badge_text = f" {proj['open_count']} "
                badge_bg = GOLD_DIM
                badge_fg = GOLD
            else:
                badge_text = " 0 "
                badge_bg = ROW_BG
                badge_fg = GREY

            badge = tk.Label(row, text=badge_text, font=FONT_SMALL,
                             bg=badge_bg, fg=badge_fg)
            badge.pack(side=tk.LEFT, padx=(8, 4))

            # Mini progress (text-based)
            if total > 0:
                pct = int(done / total * 100)
                bar_width = 6
                filled = int(done / total * bar_width)
                bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
                bar_fg = GREEN if pct == 100 else GREY
                tk.Label(row, text=bar, font=("Consolas", 8), bg=bg,
                         fg=bar_fg).pack(side=tk.LEFT, padx=(0, 8))

            launch_btn = self._make_button(row, "Launch", lambda p=proj: self._launch(p))
            launch_btn.pack(side=tk.RIGHT)

            if proj.get("remote_only"):
                remote_lbl = tk.Label(row, text="\u2601 remote only",
                                       font=FONT_SMALL, bg=bg, fg=GOLD_DIM)
                remote_lbl.pack(side=tk.RIGHT, padx=(0, 8))

            if proj.get("status") == "reference":
                tk.Label(row, text=" REF ", font=FONT_SMALL,
                         bg=GOLD_DIM, fg=GOLD).pack(side=tk.RIGHT, padx=(0, 8))

            # Clickable areas
            clickable = [row, name_lbl]
            for widget in clickable:
                widget.bind("<Button-1>", lambda e, p=proj: self._select_project(p))
                widget.bind("<Double-Button-1>", lambda e, p=proj: self._launch(p))

            # Hover (skip if selected)
            def on_enter(e, r=row, n=name_lbl, sel=is_selected):
                if not sel:
                    r.config(bg=NAVY)
                    n.config(bg=NAVY)
            def on_leave(e, r=row, n=name_lbl, b=bg, sel=is_selected):
                if not sel:
                    r.config(bg=b)
                    n.config(bg=b)
            for widget in clickable:
                widget.bind("<Enter>", on_enter)
                widget.bind("<Leave>", on_leave)

    def _show_empty_todos(self, message="Click a project to see its todos"):
        for w in self.todo_inner.winfo_children():
            w.destroy()
        tk.Label(self.todo_inner, text=message, font=FONT_BODY,
                 bg=DARK_BG, fg=GREY, pady=20).pack()

    def _select_project(self, proj):
        self.selected_project = proj
        self.detail_label.config(text=f"TODOS  \u2014  {proj['name']}", fg=WHITE)
        self._render_todos()
        self._render_project_list()

    def _render_todos(self):
        proj = self.selected_project
        if not proj:
            self._show_empty_todos()
            return

        for w in self.todo_inner.winfo_children():
            w.destroy()

        open_todos = [t for t in proj["todos"] if not t["done"]]
        done_todos = [t for t in proj["todos"] if t["done"]]

        if not open_todos and not done_todos:
            self._show_empty_todos("No todos yet. Add one above.")
            return

        for t in open_todos:
            self._make_todo_row(t, done=False)

        if done_todos and open_todos:
            # Separator between open and done
            sep = tk.Frame(self.todo_inner, bg=DIVIDER, height=1)
            sep.pack(fill=tk.X, padx=8, pady=6)
            tk.Label(self.todo_inner, text=f"Completed ({len(done_todos)})",
                     font=FONT_SMALL, bg=DARK_BG, fg=GREY).pack(anchor=tk.W, padx=8)

        for t in done_todos:
            self._make_todo_row(t, done=True)

    def _make_todo_row(self, todo, done):
        row = tk.Frame(self.todo_inner, bg=DARK_BG, pady=3, padx=8)
        row.pack(fill=tk.X)

        check_text = "\u2611" if done else "\u2610"
        check_fg = GREY if done else GOLD
        check_btn = tk.Label(row, text=check_text, font=FONT_CHECK,
                             bg=DARK_BG, fg=check_fg, cursor="hand2")
        check_btn.pack(side=tk.LEFT, padx=(0, 8))

        text_fg = GREY if done else WHITE
        text_lbl = tk.Label(row, text=todo["text"], font=FONT_TODO,
                            bg=DARK_BG, fg=text_fg, anchor=tk.W, wraplength=600)
        text_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        if todo["source"] == "cq":
            tk.Label(row, text="cq", font=FONT_SMALL, bg=DARK_BG,
                     fg=GOLD_DIM).pack(side=tk.RIGHT, padx=(4, 0))

        # Click anywhere on row to toggle
        for widget in [check_btn, row, text_lbl]:
            widget.bind("<Button-1>", lambda e, t=todo: self._toggle_todo(t))

        # Hover
        def on_enter(e, r=row, c=check_btn, t=text_lbl):
            for w in [r, c, t]:
                w.config(bg=ROW_BG)
        def on_leave(e, r=row, c=check_btn, t=text_lbl):
            for w in [r, c, t]:
                w.config(bg=DARK_BG)
        for widget in [row, check_btn, text_lbl]:
            widget.bind("<Enter>", on_enter)
            widget.bind("<Leave>", on_leave)

    def _toggle_todo(self, todo):
        proj = self.selected_project
        if not proj:
            return
        new_done = not todo["done"]
        todo["done"] = new_done

        # Update Code Queue via GitHub API
        if todo["source"] == "cq":
            self._update_cq_todo(proj["name"], todo["text"], new_done)
        else:
            self._update_md_todo(proj["path"], todo["text"], new_done)

        # Update open count
        proj["open_count"] = sum(1 for t in proj["todos"] if not t["done"])
        self._render_todos()
        self._render_project_list()
        self._update_status()

    def _update_cq_todo(self, project_name, text, done):
        """Toggle a Code Queue todo via GitHub API."""
        pat, repo = read_cq_credentials()
        if not pat:
            return
        api_url = f"https://api.github.com/repos/{repo}/contents/todos.json"
        headers = {
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "project-dashboard",
        }
        try:
            req = Request(api_url, headers=headers)
            with urlopen(req, timeout=10) as resp:
                meta = json.loads(resp.read().decode())

            file_sha = meta["sha"]
            content = json.loads(base64.b64decode(meta["content"]).decode())
            projects = content.get("data", {}).get("projects", content.get("projects", {}))
            items = projects.get(project_name, [])

            for item in items:
                if item.get("text", "").strip() == text.strip():
                    item["status"] = "done" if done else "backlog"
                    break

            new_content = base64.b64encode(
                json.dumps(content, indent=2).encode()
            ).decode()
            put_data = json.dumps({
                "message": f"Toggle '{text[:40]}' in {project_name}",
                "content": new_content,
                "sha": file_sha,
            }).encode()
            put_req = Request(api_url, data=put_data, headers=headers, method="PUT")
            urlopen(put_req, timeout=10)
        except (OSError, URLError, json.JSONDecodeError, KeyError) as e:
            print(f"CQ update error: {e}")

    def _update_md_todo(self, project_path, text, done):
        """Toggle a markdown todo in the tasks/todo.md file."""
        for todo_path in TODO_MARKERS:
            full_path = os.path.join(project_path, todo_path)
            if not os.path.exists(full_path):
                continue
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                updated = []
                for line in lines:
                    stripped = line.strip()
                    if done and re.match(r"^- \[ \]\s+", stripped):
                        line_text = re.sub(r"^- \[ \]\s+", "", stripped)
                        if line_text == text:
                            line = line.replace("- [ ]", "- [x]", 1)
                    elif not done and re.match(r"^- \[x\]\s+", stripped, re.IGNORECASE):
                        line_text = re.sub(r"^- \[x\]\s+", "", stripped, flags=re.IGNORECASE)
                        if line_text == text:
                            line = line.replace("- [x]", "- [ ]", 1).replace("- [X]", "- [ ]", 1)
                    updated.append(line)

                with open(full_path, "w", encoding="utf-8") as f:
                    f.writelines(updated)
                return
            except (OSError, UnicodeDecodeError):
                pass

    def _add_todo(self):
        text = self.add_var.get().strip()
        proj = self.selected_project
        if not text or not proj:
            return

        new_todo = {"text": text, "done": False, "source": "cq"}
        proj["todos"].insert(0, new_todo)
        proj["open_count"] = sum(1 for t in proj["todos"] if not t["done"])

        # Push to Code Queue
        self._push_new_cq_todo(proj["name"], text)

        self.add_var.set("")
        self._render_todos()
        self._render_project_list()
        self._update_status()

    def _push_new_cq_todo(self, project_name, text):
        """Add a new todo to Code Queue via GitHub API."""
        pat, repo = read_cq_credentials()
        if not pat:
            return
        api_url = f"https://api.github.com/repos/{repo}/contents/todos.json"
        headers = {
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "project-dashboard",
        }
        try:
            req = Request(api_url, headers=headers)
            with urlopen(req, timeout=10) as resp:
                meta = json.loads(resp.read().decode())

            file_sha = meta["sha"]
            content = json.loads(base64.b64decode(meta["content"]).decode())
            projects = content.get("data", {}).get("projects", content.get("projects", {}))
            items = projects.setdefault(project_name, [])

            new_item = {
                "id": hex(int(time.time() * 1000))[2:] + hex(int(time.time() * 7919))[2:4],
                "text": text,
                "status": "backlog",
                "added": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            items.insert(0, new_item)

            new_content = base64.b64encode(
                json.dumps(content, indent=2).encode()
            ).decode()
            put_data = json.dumps({
                "message": f"Add '{text[:40]}' to {project_name}",
                "content": new_content,
                "sha": file_sha,
            }).encode()
            put_req = Request(api_url, data=put_data, headers=headers, method="PUT")
            urlopen(put_req, timeout=10)
        except (OSError, URLError, json.JSONDecodeError, KeyError) as e:
            print(f"CQ add error: {e}")

    def _add_placeholder(self, entry, placeholder):
        """Add placeholder text to an entry widget."""
        def on_focus_in(e):
            if entry.get() == placeholder:
                entry.delete(0, tk.END)
                entry.config(fg=WHITE)
        def on_focus_out(e):
            if not entry.get():
                entry.insert(0, placeholder)
                entry.config(fg=GREY)
        entry.insert(0, placeholder)
        entry.config(fg=GREY)
        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)

    def _launch(self, proj):
        self._show_launch_dialog(proj)

    def _show_launch_dialog(self, proj):
        """Show dialog with Launch Local / Launch Remote options."""
        dialog = tk.Toplevel(self)
        dialog.title(f"Launch {proj['name']}")
        dialog.configure(bg=NAVY)
        dialog.geometry("420x260")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() - 420) // 2
        y = (dialog.winfo_screenheight() - 260) // 2
        dialog.geometry(f"+{x}+{y}")

        tk.Label(dialog, text=f"Launch {proj['name']}", font=FONT_HEADING,
                 bg=NAVY, fg=GOLD).pack(pady=(20, 4))

        if proj.get("status") == "reference":
            subtitle_text = "Reference project — read-only. Claude will be told not to modify files."
            subtitle_fg = GOLD
        else:
            subtitle_text = "Where should Claude run?"
            subtitle_fg = GREY
        tk.Label(dialog, text=subtitle_text, font=FONT_SMALL, bg=NAVY,
                 fg=subtitle_fg, wraplength=360,
                 justify=tk.CENTER).pack(pady=(0, 16))

        btn_frame = tk.Frame(dialog, bg=NAVY)
        btn_frame.pack(fill=tk.X, padx=40)

        # Check if project exists locally
        local_exists = os.path.isdir(proj["path"])
        is_remote_only = bool(proj.get("remote_only"))

        if is_remote_only:
            clone_btn = tk.Button(
                btn_frame, text="Clone & Launch Locally", font=FONT_BODY,
                bg=GOLD, fg=NAVY,
                activebackground=GOLD_HOVER, activeforeground=NAVY,
                relief=tk.FLAT, cursor="hand2", pady=6,
                command=lambda: (dialog.destroy(), self._clone_and_launch(proj)),
            )
            clone_btn.pack(fill=tk.X, pady=3)
            clone_btn.bind("<Enter>", lambda e: clone_btn.config(bg=GOLD_HOVER))
            clone_btn.bind("<Leave>", lambda e: clone_btn.config(bg=GOLD))
        else:
            local_label = f"Launch Local  ({proj['path'][:40]}...)" if len(proj["path"]) > 40 else f"Launch Local  ({proj['path']})"
            local_btn = tk.Button(
                btn_frame, text=local_label, font=FONT_BODY,
                bg=GOLD if local_exists else DIVIDER,
                fg=NAVY if local_exists else GREY,
                activebackground=GOLD_HOVER, activeforeground=NAVY,
                relief=tk.FLAT, cursor="hand2" if local_exists else "arrow",
                pady=6,
                state=tk.NORMAL if local_exists else tk.DISABLED,
                command=lambda: (dialog.destroy(), launch_claude_local(proj["path"], proj["name"])),
            )
            local_btn.pack(fill=tk.X, pady=3)
            if local_exists:
                local_btn.bind("<Enter>", lambda e: local_btn.config(bg=GOLD_HOVER))
                local_btn.bind("<Leave>", lambda e: local_btn.config(bg=GOLD))

        remote_label = f"Launch Remote  ({REMOTE_HOST.split('@')[1]})"
        remote_btn = tk.Button(
            btn_frame, text=remote_label, font=FONT_BODY,
            bg=DARK_BG, fg=WHITE,
            activebackground=GOLD, activeforeground=NAVY,
            relief=tk.FLAT, cursor="hand2", pady=6,
            command=lambda: (dialog.destroy(), launch_claude_remote(proj["name"])),
        )
        remote_btn.pack(fill=tk.X, pady=3)

        cancel_btn = tk.Button(
            btn_frame, text="Cancel", font=FONT_BODY,
            bg=NAVY, fg=GREY, activebackground=NAVY, activeforeground=WHITE,
            relief=tk.FLAT, cursor="hand2", pady=6,
            command=dialog.destroy,
        )
        cancel_btn.pack(fill=tk.X, pady=(12, 3))

        dialog.wait_window()

    def _clone_and_launch(self, proj):
        """Clone a remote-only project locally and then launch Claude."""
        default_url = infer_github_url(proj["name"])
        target_path = SCAN_ROOT / proj["name"]

        dialog = tk.Toplevel(self)
        dialog.title(f"Clone {proj['name']}")
        dialog.configure(bg=NAVY)
        dialog.geometry("520x320")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() - 520) // 2
        y = (dialog.winfo_screenheight() - 320) // 2
        dialog.geometry(f"+{x}+{y}")

        tk.Label(dialog, text=f"Clone {proj['name']}", font=FONT_HEADING,
                 bg=NAVY, fg=GOLD).pack(pady=(20, 4))
        tk.Label(dialog, text=f"Target: {target_path}",
                 font=FONT_SMALL, bg=NAVY, fg=GREY).pack(pady=(0, 12))

        tk.Label(dialog, text="GitHub URL", font=FONT_SMALL,
                 bg=NAVY, fg=GREY).pack(anchor=tk.W, padx=40)

        url_var = tk.StringVar(value=default_url)
        url_entry = tk.Entry(dialog, textvariable=url_var, font=FONT_BODY,
                              bg=DARK_BG, fg=WHITE, insertbackground=WHITE,
                              relief=tk.FLAT, highlightthickness=1,
                              highlightcolor=GOLD, highlightbackground=DIVIDER)
        url_entry.pack(fill=tk.X, padx=40, ipady=5)

        status_var = tk.StringVar(value="")
        tk.Label(dialog, textvariable=status_var, font=FONT_SMALL,
                 bg=NAVY, fg=GOLD, pady=6).pack()

        btn_frame = tk.Frame(dialog, bg=NAVY)
        btn_frame.pack(pady=(8, 0))

        state = {"busy": False}

        def on_done(success, msg):
            state["busy"] = False
            if success:
                status_var.set("Cloned. Launching...")
                self.refresh()
                dialog.after(300, lambda: (
                    dialog.destroy(),
                    launch_claude_local(str(target_path), proj["name"]),
                ))
            else:
                status_var.set("Clone failed.")
                clone_btn.config(state=tk.NORMAL, bg=GOLD, cursor="hand2")
                cancel_btn.config(state=tk.NORMAL)
                messagebox.showerror("Clone Failed", msg[:2000] or "Unknown error")

        def do_clone():
            if state["busy"]:
                return
            url = url_var.get().strip()
            if not url:
                status_var.set("Enter a URL")
                return
            state["busy"] = True
            clone_btn.config(state=tk.DISABLED, bg=DIVIDER, cursor="arrow")
            cancel_btn.config(state=tk.DISABLED)
            status_var.set("Cloning... (may take a moment)")
            dialog.update_idletasks()

            def worker():
                success, msg = clone_project(proj["name"], url)
                dialog.after(0, lambda: on_done(success, msg))

            threading.Thread(target=worker, daemon=True).start()

        clone_btn = tk.Button(btn_frame, text="Clone & Launch", font=FONT_BODY,
                               bg=GOLD, fg=NAVY, activebackground=GOLD_HOVER,
                               activeforeground=NAVY, relief=tk.FLAT,
                               padx=18, pady=6, cursor="hand2", command=do_clone)
        clone_btn.pack(side=tk.LEFT, padx=8)
        clone_btn.bind("<Enter>", lambda e: clone_btn.config(bg=GOLD_HOVER)
                       if clone_btn["state"] == tk.NORMAL else None)
        clone_btn.bind("<Leave>", lambda e: clone_btn.config(bg=GOLD)
                       if clone_btn["state"] == tk.NORMAL else None)

        cancel_btn = tk.Button(btn_frame, text="Cancel", font=FONT_BODY,
                                bg=NAVY, fg=GREY, activebackground=NAVY,
                                activeforeground=WHITE, relief=tk.FLAT,
                                padx=18, pady=6, cursor="hand2",
                                command=dialog.destroy)
        cancel_btn.pack(side=tk.LEFT, padx=8)

        dialog.wait_window()

    def _on_blank_claude(self):
        """Show dialog to launch a blank Claude session."""
        dialog = tk.Toplevel(self)
        dialog.title("Launch Claude")
        dialog.configure(bg=NAVY)
        dialog.geometry("380x220")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() - 380) // 2
        y = (dialog.winfo_screenheight() - 220) // 2
        dialog.geometry(f"+{x}+{y}")

        tk.Label(dialog, text="Launch Claude", font=FONT_HEADING,
                 bg=NAVY, fg=GOLD).pack(pady=(20, 4))
        tk.Label(dialog, text="Start a blank session with no project context",
                 font=FONT_SMALL, bg=NAVY, fg=GREY).pack(pady=(0, 16))

        btn_frame = tk.Frame(dialog, bg=NAVY)
        btn_frame.pack(fill=tk.X, padx=40)

        local_btn = tk.Button(
            btn_frame, text="Local (this machine)", font=FONT_BODY,
            bg=GOLD, fg=NAVY, activebackground=GOLD_HOVER, activeforeground=NAVY,
            relief=tk.FLAT, cursor="hand2", pady=6,
            command=lambda: (dialog.destroy(), launch_blank_claude("local")),
        )
        local_btn.pack(fill=tk.X, pady=3)
        local_btn.bind("<Enter>", lambda e: local_btn.config(bg=GOLD_HOVER))
        local_btn.bind("<Leave>", lambda e: local_btn.config(bg=GOLD))

        remote_btn = tk.Button(
            btn_frame, text=f"Remote  ({REMOTE_HOST.split('@')[1]})", font=FONT_BODY,
            bg=DARK_BG, fg=WHITE,
            activebackground=GOLD, activeforeground=NAVY,
            relief=tk.FLAT, cursor="hand2", pady=6,
            command=lambda: (dialog.destroy(), launch_blank_claude("remote")),
        )
        remote_btn.pack(fill=tk.X, pady=3)

        cancel_btn = tk.Button(
            btn_frame, text="Cancel", font=FONT_BODY,
            bg=NAVY, fg=GREY, activebackground=NAVY, activeforeground=WHITE,
            relief=tk.FLAT, cursor="hand2", pady=6,
            command=dialog.destroy,
        )
        cancel_btn.pack(fill=tk.X, pady=(12, 3))

        dialog.wait_window()

    def _on_new_project(self):
        name = simpledialog.askstring(
            "New Project",
            "Project name (lowercase, alphanumeric, hyphens):",
            parent=self,
        )
        if not name:
            return

        name = name.strip().lower()
        if not re.match(r"^[a-z0-9][a-z0-9\-]*$", name):
            messagebox.showerror("Invalid Name",
                "Name must be lowercase, alphanumeric, and hyphens only.")
            return

        project_type = self._ask_project_type()
        if project_type is None:
            return

        location = self._ask_project_location()
        if location is None:
            return

        if create_new_project(name, project_type, location):
            self.refresh()
            if location == "remote":
                launch_claude_remote(name)
            else:
                launch_claude_local(str(SCAN_ROOT / name), name)

    def _ask_project_type(self):
        """Show a dialog to pick project type. Returns type string or None."""
        dialog = tk.Toplevel(self)
        dialog.title("Project Type")
        dialog.configure(bg=NAVY)
        dialog.geometry("320x200")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() - 320) // 2
        y = (dialog.winfo_screenheight() - 200) // 2
        dialog.geometry(f"+{x}+{y}")

        result = {"choice": None}

        tk.Label(dialog, text="Choose project type", font=FONT_HEADING,
                 bg=NAVY, fg=GOLD).pack(pady=(20, 16))

        btn_frame = tk.Frame(dialog, bg=NAVY)
        btn_frame.pack(fill=tk.X, padx=24)

        types = [
            ("Python", "python"),
            ("Next.js", "nextjs"),
            ("Plain", "plain"),
        ]
        for label, ptype in types:
            btn = tk.Button(
                btn_frame, text=label, font=FONT_BODY, bg=DARK_BG, fg=WHITE,
                activebackground=GOLD, activeforeground=NAVY,
                relief=tk.FLAT, cursor="hand2", pady=6,
                command=lambda t=ptype: (result.__setitem__("choice", t), dialog.destroy()),
            )
            btn.pack(fill=tk.X, pady=3)

        dialog.wait_window()
        return result["choice"]

    def _ask_project_location(self):
        """Ask where to create the project. Returns 'remote', 'local', or None.

        If a default is saved, uses it without prompting.
        """
        config = load_config()
        saved = config.get("default_location")
        if saved in ("remote", "local"):
            return saved

        dialog = tk.Toplevel(self)
        dialog.title("Project Location")
        dialog.configure(bg=NAVY)
        dialog.geometry("400x240")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() - 400) // 2
        y = (dialog.winfo_screenheight() - 240) // 2
        dialog.geometry(f"+{x}+{y}")

        result = {"choice": None}
        remember_var = tk.BooleanVar(value=False)

        tk.Label(dialog, text="Where should this project live?",
                 font=FONT_HEADING, bg=NAVY, fg=GOLD).pack(pady=(20, 4))
        tk.Label(dialog, text="Remote projects run on the cloud server via SSH.\n"
                 "Local projects run on this machine.",
                 font=FONT_SMALL, bg=NAVY, fg=GREY).pack(pady=(0, 12))

        btn_frame = tk.Frame(dialog, bg=NAVY)
        btn_frame.pack(fill=tk.X, padx=40)

        def pick(choice):
            result["choice"] = choice
            if remember_var.get():
                cfg = load_config()
                cfg["default_location"] = choice
                save_config(cfg)
            dialog.destroy()

        remote_btn = tk.Button(
            btn_frame, text=f"Remote Server  ({REMOTE_HOST.split('@')[1]})",
            font=FONT_BODY, bg=GOLD, fg=NAVY,
            activebackground=GOLD_HOVER, activeforeground=NAVY,
            relief=tk.FLAT, cursor="hand2", pady=6,
            command=lambda: pick("remote"),
        )
        remote_btn.pack(fill=tk.X, pady=3)
        remote_btn.bind("<Enter>", lambda e: remote_btn.config(bg=GOLD_HOVER))
        remote_btn.bind("<Leave>", lambda e: remote_btn.config(bg=GOLD))

        local_btn = tk.Button(
            btn_frame, text="Local (this machine)",
            font=FONT_BODY, bg=DARK_BG, fg=WHITE,
            activebackground=GOLD, activeforeground=NAVY,
            relief=tk.FLAT, cursor="hand2", pady=6,
            command=lambda: pick("local"),
        )
        local_btn.pack(fill=tk.X, pady=3)

        tk.Checkbutton(
            dialog, text="Remember this choice", variable=remember_var,
            font=FONT_SMALL, bg=NAVY, fg=GREY, selectcolor=DARK_BG,
            activebackground=NAVY, activeforeground=WHITE,
        ).pack(pady=(12, 0))

        dialog.wait_window()
        return result["choice"]

    def _update_status(self):
        total_projects = len(self.filtered_projects)
        total_open = sum(p["open_count"] for p in self.filtered_projects)
        total_todos = sum(len(p["todos"]) for p in self.filtered_projects)
        total_done = total_todos - total_open

        self.project_count_label.config(
            text=f"{total_done}/{total_todos} done" if total_todos > 0 else ""
        )
        self.status_var.set(
            f"{total_projects} project{'s' if total_projects != 1 else ''}  \u00b7  "
            f"{total_open} outstanding  \u00b7  "
            f"{total_done} completed"
        )


if __name__ == "__main__":
    app = ProjectDashboard()
    app.mainloop()
