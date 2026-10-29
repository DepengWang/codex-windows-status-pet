"""Pure formatting helpers for quota display."""

from __future__ import annotations

from datetime import datetime, timezone


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            try:
                normalized = value.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(normalized)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError:
                return None
    return None


def local_time_date(value):
    if value in (None, ""):
        return "--"
    timestamp = _epoch(value)
    if timestamp is None:
        return "--"
    try:
        return datetime.fromtimestamp(timestamp).astimezone().strftime("%H:%M %-m/%-d")
    except (TypeError, ValueError, OverflowError, OSError):
        # Windows strftime does not support %-m/%-d.
        try:
            current = datetime.fromtimestamp(timestamp).astimezone()
            return f"{current.hour:02d}:{current.minute:02d} {current.month}/{current.day}"
        except (TypeError, ValueError, OverflowError, OSError):
            return "--"


def reset_display_time(value, now=None):
    """Format quota resets as HH:mm today, otherwise HH:mm M/d."""
    timestamp = _epoch(value)
    if timestamp is None:
        return "—"
    try:
        moment = datetime.fromtimestamp(timestamp).astimezone()
        current = (now or datetime.now().astimezone()).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"
    if moment.date() == current.date():
        return moment.strftime("%H:%M")
    return f"{moment.hour:02d}:{moment.minute:02d} {moment.month}/{moment.day}"


def earliest_future_expiry(expirations, now=None):
    """Return the earliest future epoch from a mixed provider response."""
    current = datetime.now().timestamp() if now is None else float(now)
    values = []
    def candidates(value):
        if isinstance(value, dict):
            for key in ("expiresAt", "resetsAt", "resetAt"):
                if key in value:
                    yield value[key]
            for child in value.values():
                yield from candidates(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from candidates(child)
        else:
            yield value

    for value in candidates(expirations):
        timestamp = _epoch(value)
        if timestamp is None:
            continue
        if timestamp > current:
            values.append(timestamp)
    return min(values) if values else None


def quota_line(label, value, reset_at):
    if not reset_at:
        return f"{label} {value}"
    return f"{label} {value} / {local_time_date(reset_at)}"


def reset_credit_line(count, expiration, text=None):
    text = text or f"重置 {count} 次"
    if expiration is None:
        return text
    return f"{text} / {local_time_date(expiration)}"
