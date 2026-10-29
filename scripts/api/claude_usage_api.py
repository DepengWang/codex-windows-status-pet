"""Read and normalize only Claude Code plan quota fields from `/usage`."""

from __future__ import annotations

import calendar
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_USED_PERCENT = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*%\s*used\b", re.IGNORECASE)
_REMAINING_PERCENT = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*%\s*(?:remaining|left)\b", re.IGNORECASE
)
_RESET_DATE = re.compile(
    r"\bresets?\s+(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})"
    r"(?:,\s*(?P<year>\d{4}))?\s*,?\s*"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<ampm>a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)
_RESET_RELATIVE = re.compile(
    r"\bresets?\s+in\s+(?:(?P<days>\d+)\s*d(?:ays?)?\s*)?"
    r"(?:(?P<hours>\d+)\s*h(?:ours?)?\s*)?"
    r"(?:(?P<minutes>\d+)\s*m(?:in(?:utes?)?)?\s*)?",
    re.IGNORECASE,
)


def _local_now(now=None):
    current = now or datetime.now().astimezone()
    return current.astimezone() if current.tzinfo else current.astimezone()


def _reset_timestamp(line, now):
    match = _RESET_DATE.search(line)
    if match:
        month_name = match.group("month").lower()
        months = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
        months.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})
        month = months.get(month_name) or months.get(month_name[:3])
        if not month:
            return None
        hour = int(match.group("hour")) % 12
        if match.group("ampm").lower().startswith("p"):
            hour += 12
        minute = int(match.group("minute") or 0)
        explicit_year = match.group("year")
        years = [int(explicit_year)] if explicit_year else [now.year, now.year + 1]
        for year in years:
            try:
                candidate = datetime(
                    year, month, int(match.group("day")), hour, minute,
                    tzinfo=now.tzinfo,
                )
            except ValueError:
                return None
            if explicit_year or candidate >= now:
                return candidate.timestamp()
        return None

    relative = _RESET_RELATIVE.search(line)
    if relative:
        days = int(relative.group("days") or 0)
        hours = int(relative.group("hours") or 0)
        minutes = int(relative.group("minutes") or 0)
        if days or hours or minutes:
            from datetime import timedelta

            return (now + timedelta(days=days, hours=hours, minutes=minutes)).timestamp()
    return None


def _remaining_percent(line):
    used = _USED_PERCENT.search(line)
    if used:
        value = 100.0 - float(used.group("value"))
    else:
        remaining = _REMAINING_PERCENT.search(line)
        if not remaining:
            return None
        value = float(remaining.group("value"))
    return max(0, min(100, round(value)))


def parse_claude_usage(output, now=None):
    """Return only session, weekly, and model-weekly remaining percentages/reset epochs."""
    current = _local_now(now)
    primary = None
    weekly = None
    models = {}

    for raw_line in _ANSI_ESCAPE.sub("", str(output)).splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if not line or any(word in lowered for word in ("cost", "token", "prompt", "conversation")):
            continue
        remaining = _remaining_percent(line)
        reset_at = _reset_timestamp(line, current)
        if remaining is None or reset_at is None:
            continue
        entry = {"remainingPercent": remaining, "resetsAt": reset_at}

        if "week" in lowered:
            model = next((name for name in ("Sonnet", "Opus") if name.lower() in lowered), None)
            if model:
                models[model] = entry
            elif "all model" in lowered or "week" in lowered:
                weekly = entry
        elif "session" in lowered or "5-hour" in lowered or "5h" in lowered:
            primary = entry

    if primary is None and weekly is None:
        raise ValueError("Claude /usage output contained no quota windows")
    return {"primary": primary, "weekly": weekly, "models": models}


def find_claude():
    """Find an installed Claude Code executable without inspecting its credentials."""
    candidates = [shutil.which("claude.exe"), shutil.which("claude")]
    candidates.extend((
        Path.home() / ".local" / "bin" / "claude.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Claude" / "claude.exe",
    ))
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file() and (os.name != "nt" or path.suffix.lower() == ".exe"):
            return str(path)
    raise FileNotFoundError("Claude Code executable not found")


class ClaudeUsageClient:
    """Run the existing signed-in CLI in hidden, non-persistent query mode."""

    def __init__(self, timeout_seconds=25):
        self.timeout_seconds = timeout_seconds

    def read_usage(self):
        command = [
            find_claude(), "-p", "--no-session-persistence", "--max-turns", "1",
            "--max-budget-usd", "0.01", "/usage", "--output-format", "text",
        ]
        options = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": self.timeout_seconds,
            "check": False,
        }
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            options["startupinfo"] = startup
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(command, **options)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("Claude Code /usage timed out") from error
        except OSError as error:
            raise RuntimeError("Unable to start Claude Code /usage") from error
        if result.returncode != 0:
            raise RuntimeError(f"Claude Code /usage exited with status {result.returncode}")
        return parse_claude_usage(result.stdout)
