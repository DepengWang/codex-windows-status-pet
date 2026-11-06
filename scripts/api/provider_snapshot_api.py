"""Normalise provider quota payloads into display snapshots and short display strings."""

from __future__ import annotations

from datetime import datetime, timezone

from api.quota_format_api import earliest_future_expiry, reset_display_time


def epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def safe_window(window):
    if not isinstance(window, dict):
        return None
    used = window.get("usedPercent")
    remaining = None
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        remaining = max(0, min(100, round(100 - float(used))))
    rawreset = epoch(window.get("resetsAt"))
    if remaining is None and rawreset is None:
        return None
    return {"remainingPercent": remaining, "resetsAt": rawreset}


def codex_snapshot(parsed):
    limits = parsed.get("rateLimits", {})
    primary = safe_window(limits.get("primary"))
    weekly = safe_window(limits.get("secondary"))
    if primary is None and weekly is None:
        raise ValueError("No Codex quota windows")
    credits = parsed.get("rateLimitResetCredits", {})
    count = credits.get("availableCount")
    if not isinstance(count, int) or isinstance(count, bool):
        count = None
    expiry = earliest_future_expiry(credits.get("resetsAt"))
    return {
        "primary": primary,
        "weekly": weekly,
        "resetCount": count,
        "resetAt": expiry,
    }


def claude_snapshot(parsed):
    def clean(value):
        if not isinstance(value, dict):
            return None
        remaining = value.get("remainingPercent")
        if not isinstance(remaining, int) or isinstance(remaining, bool):
            return None
        reset = epoch(value.get("resetsAt"))
        return {
            "remainingPercent": max(0, min(100, remaining)),
            "resetsAt": reset,
        }

    models = {}
    for name, value in parsed.get("models", {}).items():
        if name not in {"Sonnet", "Opus"}:
            continue
        model = clean(value)
        if model:
            models[name] = model
    primary = clean(parsed.get("primary"))
    weekly = clean(parsed.get("weekly"))
    if primary is None and weekly is None and not models:
        raise ValueError("No Claude quota windows")
    return {"primary": primary, "weekly": weekly, "models": models}


def percent(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return f"{max(0, min(100, round(value)))}%"


def reset(value):
    return reset_display_time(value) if value is not None else "—"


def safe_failure(provider, error):
    if isinstance(error, TimeoutError):
        return "query timed out"
    if isinstance(error, FileNotFoundError):
        return "CLI not found"
    if isinstance(error, PermissionError):
        return "process could not start"
    if isinstance(error, ValueError):
        return "no quota fields returned"
    if isinstance(error, RuntimeError):
        message = str(error)
        if "status " in message:
            suffix = message.rsplit("status ", 1)[-1]
            if suffix.isdigit():
                return f"CLI exited ({suffix})"
        return "local quota service unavailable"
    return f"request failed ({type(error).__name__})"


def count_text(value):
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "—"
