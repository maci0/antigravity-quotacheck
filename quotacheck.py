#!/usr/bin/env python3
"""Antigravity model quota checker TUI."""

import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

# Suppress warnings for local self-signed HTTPS connections to IDE
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

OAUTH_CLIENT_ID = (
    "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
)
OAUTH_CLIENT_SECRET = "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"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://cloudcode-pa.googleapis.com"

# Internal models to hide (tab completion, chat internals, etc.)
HIDDEN_PREFIXES = ("tab_", "chat_")

console = Console()
session = requests.Session()


def _search_paths() -> list[Path]:
    paths = [
        Path.home() / ".config" / "opencode" / "antigravity-accounts.json",
    ]
    xdg_raw = os.environ.get("XDG_DATA_HOME", "")
    xdg_data = Path(xdg_raw) if xdg_raw else None
    default_data = Path.home() / ".local" / "share"
    if xdg_data and xdg_data != default_data:
        paths.append(xdg_data / "opencode" / "antigravity-accounts.json")
    paths.append(default_data / "opencode" / "antigravity-accounts.json")
    return paths


def find_accounts_file() -> Path | None:
    for p in _search_paths():
        if p.exists():
            return p
    return None


def load_accounts(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        console.print(f"[red]Failed to read {path}: {e}[/]")
        sys.exit(1)
    return data.get("accounts", [])


_cached_token: str | None = None
_cached_token_expiry: float = 0


def get_access_token(refresh_token: str) -> str:
    global _cached_token, _cached_token_expiry
    if _cached_token and time.monotonic() < _cached_token_expiry:
        return _cached_token
    resp = session.post(
        TOKEN_URL,
        data={
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _cached_token = data["access_token"]
    # Refresh 5 minutes before actual expiry
    _cached_token_expiry = time.monotonic() + data.get("expires_in", 3600) - 300
    return _cached_token


def invalidate_token_cache():
    global _cached_token, _cached_token_expiry
    _cached_token = None
    _cached_token_expiry = 0


SERVER_SIGNALS = (
    "language-server",
    "lsp",
    "--csrf_token",
    "--extension_server_port",
    "exa.language_server_pb",
)


def find_antigravity_process() -> dict | None:
    """Find a running Antigravity language server process."""
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    for line in result.stdout.splitlines():
        if "antigravity" not in line.lower():
            continue
        if not any(sig in line for sig in SERVER_SIGNALS):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        csrf_token = None
        extension_server_port = None
        m = re.search(r"--csrf_token[= ](\S+)", line)
        if m:
            csrf_token = m.group(1)
        m = re.search(r"--extension_server_port[= ](\d+)", line)
        if m:
            extension_server_port = int(m.group(1))
        return {
            "pid": pid,
            "csrf_token": csrf_token,
            "extension_server_port": extension_server_port,
        }
    return None


def discover_ports(pid: int) -> list[int]:
    """Discover TCP ports a process is listening on."""
    ports: list[int] = []
    system = platform.system()
    if system == "Linux":
        for cmd in [["ss", "-tlnp"], ["netstat", "-tlnp"]]:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            except (subprocess.SubprocessError, FileNotFoundError):
                continue
            for line in result.stdout.splitlines():
                if f"pid={pid}," not in line:
                    continue
                m = re.search(r":(\d+)\s", line)
                if m:
                    port = int(m.group(1))
                    if port not in ports:
                        ports.append(port)
            if ports:
                break
    elif system == "Darwin":
        try:
            result = subprocess.run(
                ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                m = re.search(r":(\d+)\s", line)
                if m:
                    port = int(m.group(1))
                    if port not in ports:
                        ports.append(port)
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    return ports


def probe_connect_port(ports: list[int], csrf_token: str | None) -> str | None:
    """Find the Connect RPC port by probing each candidate."""
    headers = {"Connect-Protocol-Version": "1", "Content-Type": "application/json"}
    if csrf_token:
        headers["X-Codeium-Csrf-Token"] = csrf_token
    path = "/exa.language_server_pb.LanguageServerService/GetUnleashData"
    for port in ports:
        for scheme in ("https", "http"):
            url = f"{scheme}://127.0.0.1:{port}"
            try:
                resp = session.post(
                    url + path,
                    headers=headers,
                    json={},
                    timeout=0.5,
                    verify=False,
                )
                if resp.status_code in (200, 401):
                    return url
            except requests.RequestException:
                continue
    return None


def fetch_models_local(base_url: str, csrf_token: str | None) -> tuple[str, dict]:
    """Fetch model quota from local IDE and return (email, models_dict)."""
    headers = {"Connect-Protocol-Version": "1", "Content-Type": "application/json"}
    if csrf_token:
        headers["X-Codeium-Csrf-Token"] = csrf_token
    body = {
        "metadata": {
            "ideName": "antigravity",
            "extensionName": "antigravity",
            "locale": "en",
        }
    }
    resp = session.post(
        f"{base_url}/exa.language_server_pb.LanguageServerService/GetUserStatus",
        headers=headers,
        json=body,
        timeout=15,
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()

    user_status = data.get("userStatus", data)
    email = user_status.get("email") or user_status.get("name") or "local-user"

    cascade = user_status.get("cascadeModelConfigData", user_status)
    config_list = cascade.get("clientModelConfigs", [])

    models: dict[str, dict] = {}
    for cfg in config_list:
        model_key = cfg.get("modelOrAlias", {}).get("model")
        if not model_key:
            continue
        info: dict = {"displayName": cfg.get("label", model_key)}
        if cfg.get("isRecommended") or cfg.get("recommended"):
            info["recommended"] = True
        if cfg.get("tagTitle"):
            info["tagTitle"] = cfg["tagTitle"]
        quota = cfg.get("quotaInfo")
        if quota:
            info["quotaInfo"] = {
                "remainingFraction": quota.get("remainingFraction", 0.0),
            }
            if quota.get("resetTime"):
                info["quotaInfo"]["resetTime"] = quota["resetTime"]
        models[model_key] = info
    return email, {"models": models}


def fetch_models(access_token: str) -> dict:
    resp = session.post(
        f"{API_BASE}/v1internal:fetchAvailableModels",
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "antigravity",
        },
        json={},
        timeout=15,
    )
    if resp.status_code == 401:
        invalidate_token_cache()
    resp.raise_for_status()
    return resp.json()


def format_reset_time(reset_time_str: str | None) -> str:
    if not reset_time_str:
        return "-"
    try:
        reset = datetime.fromisoformat(reset_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = reset - now
        total_seconds = int(delta.total_seconds())
        if total_seconds <= 0:
            return "now"
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except (ValueError, TypeError):
        return "?"


def quota_color(fraction: float) -> str:
    if fraction >= 0.5:
        return "green"
    if fraction >= 0.2:
        return "yellow"
    return "red"


def build_bar(fraction: float, width: int = 15) -> Text:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(fraction * width)
    empty = width - filled
    color = quota_color(fraction)
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * empty, style="dim")
    bar.append(f" {fraction * 100:.0f}%", style=f"bold {color}")
    return bar


def model_family(key: str, info: dict) -> str | None:
    """Return the display family, or None if the model should be hidden."""
    if key.startswith(HIDDEN_PREFIXES):
        return None
    display = info.get("displayName")
    if not display:
        return None
    provider = info.get("modelProvider", "")
    key_lower = key.lower()
    display_lower = display.lower()
    if "GOOGLE" in provider or "gemini" in key_lower or "gemini" in display_lower:
        return "Gemini"
    if "ANTHROPIC" in provider or "claude" in key_lower or "claude" in display_lower:
        return "Claude"
    if "OPENAI" in provider or "gpt" in key_lower or "gpt" in display_lower:
        return "GPT"
    return "Other"


def _model_name_text(
    display_name: str, tag_title: str | None, recommended: bool
) -> Text:
    """Build the model name as a Text object, avoiding markup injection."""
    text = Text()
    if recommended:
        text.append("* ", style="yellow")
    else:
        text.append("  ")
    text.append(display_name, style="bold white")
    if tag_title:
        text.append(f" ({tag_title})", style="dim")
    return text


def build_dashboard(email: str, models: dict) -> Panel:
    # Group and filter
    groups: dict[str, list[tuple[str, dict]]] = {}
    for key, info in models.items():
        fam = model_family(key, info)
        if fam is None:
            continue
        groups.setdefault(fam, []).append((key, info))

    for fam in groups:
        groups[fam].sort(key=lambda x: x[1].get("displayName", x[0]))

    table = Table(
        show_header=True,
        header_style="bold magenta",
        expand=True,
        pad_edge=False,
        show_lines=False,
        padding=(0, 1),
    )
    table.add_column("Model", no_wrap=True, ratio=1)
    table.add_column("Quota", no_wrap=True)
    table.add_column("Resets", justify="right", no_wrap=True)

    first_group = True
    for family in ["Gemini", "Claude", "GPT", "Other"]:
        if family not in groups:
            continue
        if not first_group:
            table.add_section()
        first_group = False

        for key, info in groups[family]:
            display_name = info.get("displayName", key)
            quota_info = info.get("quotaInfo")

            name = _model_name_text(
                display_name, info.get("tagTitle"), info.get("recommended", False)
            )

            if quota_info:
                fraction = quota_info.get("remainingFraction", 0.0)
                reset_str = quota_info.get("resetTime")
            else:
                fraction = 0.0
                reset_str = None

            table.add_row(
                name,
                build_bar(fraction),
                Text(format_reset_time(reset_str), style="cyan"),
            )

    now_str = datetime.now().strftime("%H:%M:%S")
    title = Text.assemble(
        ("Antigravity Quota", "bold cyan"),
        (" — ", "dim"),
        (email, "white"),
    )
    return Panel(
        table,
        title=title,
        subtitle=Text(now_str, style="dim"),
        border_style="cyan",
        padding=(1, 1),
    )


def _try_local_ide() -> tuple[str, dict] | None:
    """Attempt to connect to a local Antigravity IDE. Returns (email, data) or None."""
    proc = find_antigravity_process()
    if not proc:
        return None
    csrf_token = proc.get("csrf_token")
    ports: list[int] = []
    if proc.get("extension_server_port"):
        ports.append(proc["extension_server_port"])
    ports.extend(p for p in discover_ports(proc["pid"]) if p not in ports)
    if not ports:
        return None
    base_url = probe_connect_port(ports, csrf_token)
    if not base_url:
        return None
    email, data = fetch_models_local(base_url, csrf_token)
    return email, data


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Check Antigravity model quota")
    parser.add_argument(
        "--watch", "-w", action="store_true", help="Auto-refresh periodically"
    )
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=60,
        metavar="SECS",
        help="Refresh interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--account",
        "-a",
        type=int,
        default=0,
        metavar="INDEX",
        help="Account index (default: 0)",
    )
    parser.add_argument(
        "--json", "-j", action="store_true", help="Output raw JSON instead of TUI"
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--local",
        action="store_true",
        help="Fetch quota from local IDE (no credentials file needed)",
    )
    source.add_argument(
        "--cloud",
        action="store_true",
        help="Fetch quota from cloud API (requires accounts file)",
    )
    args = parser.parse_args()

    email = None
    do_fetch = None

    if args.local or not args.cloud:
        try:
            result = _try_local_ide()
        except Exception as e:
            result = None
            if args.local:
                console.print(f"[red]Local IDE connection failed: {e}[/]")
                sys.exit(1)
        if result:
            email, local_data = result
            local_models = local_data.get("models", {})
            if local_models:

                def do_fetch() -> dict:
                    fresh = _try_local_ide()
                    if fresh:
                        return fresh[1]
                    return local_data

                console.print("[dim]Connected to local IDE[/]")
            elif args.local:
                console.print(
                    "[yellow]Connected to local IDE but no models returned.[/]"
                )

                def do_fetch() -> dict:
                    fresh = _try_local_ide()
                    if fresh:
                        return fresh[1]
                    return local_data
            else:
                console.print(
                    "[dim]Local IDE returned no models, falling back to cloud...[/]"
                )
        elif args.local:
            console.print("[red]Could not find a running Antigravity IDE.[/]")
            console.print(
                "Make sure Antigravity is running in your IDE "
                "(VS Code, JetBrains, etc.) and try again."
            )
            sys.exit(1)
        elif not args.cloud:
            console.print(
                "[dim]No local Antigravity IDE found, falling back to cloud...[/]"
            )

    if do_fetch is None:
        accounts_path = find_accounts_file()
        if not accounts_path:
            console.print("[red]Could not find antigravity-accounts.json[/]")
            console.print("Looked in:")
            for p in _search_paths():
                console.print(f"  {p}")
            console.print(
                "\nMake sure you have opencode-antigravity-auth set up, "
                "or Antigravity credentials stored in one of the above paths."
            )
            console.print(
                "\n[dim]Tip: If Antigravity is running in your IDE, "
                "try [bold]--local[/bold] to fetch quota directly.[/]"
            )
            sys.exit(1)

        accounts = load_accounts(accounts_path)
        if not accounts:
            console.print("[red]No accounts found in credentials file.[/]")
            sys.exit(1)

        if args.account >= len(accounts):
            console.print(
                f"[red]Account index {args.account} out of range "
                f"(have {len(accounts)} account(s)).[/]"
            )
            sys.exit(1)

        account = accounts[args.account]
        email = account.get("email", "unknown")
        refresh_token = account.get("refreshToken")
        if not refresh_token:
            console.print(
                f"[red]Account {args.account} ({email}) has no refreshToken.[/]"
            )
            sys.exit(1)

        def do_fetch() -> dict:
            access_token = get_access_token(refresh_token)
            return fetch_models(access_token)

    if args.json:
        with console.status("[cyan]Fetching quota...[/]"):
            data = do_fetch()
        console.print_json(data=data)
        return

    if not args.watch:
        with console.status("[cyan]Fetching quota...[/]"):
            data = do_fetch()
        models = data.get("models", {})
        if not models:
            console.print("[yellow]No models returned.[/]")
            console.print_json(data=data)
            sys.exit(1)
        console.print(build_dashboard(email, models))
    else:
        console.print(
            f"[dim]Refreshing every {args.interval}s. Press Ctrl+C to stop.[/]\n"
        )
        try:
            while True:
                try:
                    with console.status("[cyan]Fetching quota...[/]"):
                        data = do_fetch()
                        models = data.get("models", {})
                        output = build_dashboard(email, models) if models else None
                    console.clear()
                    if output:
                        console.print(output)
                    else:
                        console.print("[yellow]No models returned.[/]")
                    console.print(
                        f"\n[dim]Next refresh in {args.interval}s. Ctrl+C to quit.[/]"
                    )
                except (requests.RequestException, KeyError, ValueError) as e:
                    console.print(f"[red]Request failed: {e}[/]")
                    console.print(f"[dim]Retrying in {args.interval}s...[/]")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped.[/]")


if __name__ == "__main__":
    main()
