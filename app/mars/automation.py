from __future__ import annotations

import json


RESTART_WARNING_SECONDS = (1800, 600, 300, 180, 120, 60, 30, 10)
TITLE_WARNING_SECONDS = frozenset({600, 60})
RESTART_WARNING_LEAD_MINUTES = RESTART_WARNING_SECONDS[0] // 60


def restart_warning_command(seconds: int) -> str:
    """Return the Minecraft console command for one scheduled warning."""
    amount = f"{seconds // 60}分" if seconds >= 60 else f"{seconds}秒"
    message = f"サーバーは{amount}後に再起動します。"
    if seconds in TITLE_WARNING_SECONDS:
        payload = json.dumps({"text": message, "color": "yellow"}, ensure_ascii=False, separators=(",", ":"))
        return f"title @a title {payload}"
    return f"say {message}"


def restart_warning_summary() -> str:
    """Return the fixed schedule shown in the Automation page."""
    parts = []
    for seconds in RESTART_WARNING_SECONDS:
        label = f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"
        parts.append(f"{label}(title)" if seconds in TITLE_WARNING_SECONDS else label)
    return " / ".join(parts)
