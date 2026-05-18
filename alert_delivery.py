from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import requests


def find_powershell() -> list[str]:
    for cmd in ("pwsh", "powershell"):
        exe = shutil.which(cmd)
        if exe:
            if cmd == "powershell":
                return [exe, "-ExecutionPolicy", "Bypass", "-File"]
            return [exe, "-File"]
    raise RuntimeError("PowerShell not found.")


def resolve_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    return None


def send_telegram_http(message: str) -> None:
    bot_token = resolve_env("TELEGRAM_BOT_TOKEN")
    chat_id = resolve_env("TELEGRAM_CHAT_ID")
    prefix = resolve_env("TELEGRAM_MESSAGE_PREFIX")
    if not bot_token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID for HTTP fallback.")
    text = message.strip()
    if prefix:
        text = f"{prefix} {text}".strip()
    if len(text) > 3500:
        text = text[:3497] + "..."
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram HTTP fallback returned non-ok response.")


def send_telegram(script_path: Path, message: str) -> None:
    try:
        if not script_path.exists():
            raise FileNotFoundError(f"Telegram script not found: {script_path}")
        cmd = find_powershell() + [str(script_path), "-Message", message]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except Exception:
        send_telegram_http(message)


def telegram_api_get_updates(offset: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    bot_token = resolve_env("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return []
    params: dict[str, Any] = {"timeout": 0, "limit": limit}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getUpdates",
        params=params,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram getUpdates returned non-ok response.")
    return list(data.get("result", []))
