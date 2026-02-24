#!/usr/bin/env python3
"""Antigravity model quota checker TUI."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

OAUTH_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
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
    if not info.get("displayName"):
        return None
    provider = info.get("modelProvider", "")
    key_lower = key.lower()
    if "GOOGLE" in provider or "gemini" in key_lower:
        return "Gemini"
    if "ANTHROPIC" in provider or "claude" in key_lower:
        return "Claude"
    if "OPENAI" in provider or "gpt" in key_lower:
        return "GPT"
    return "Other"


def _model_name_text(display_name: str, tag_title: str | None, recommended: bool) -> Text:
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
        "--account", "-a", type=int, default=0, metavar="INDEX",
        help="Account index (default: 0)",
    )
    parser.add_argument(
        "--json", "-j", action="store_true", help="Output raw JSON instead of TUI"
    )
    args = parser.parse_args()

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
