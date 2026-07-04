"""
notify.py
Discord Webhook への実験進捗通知。
"""

import requests

WEBHOOK_URL = "https://discord.com/api/webhooks/1522520851143069746/OGi49l4g4HK6X0g_Hv-t2B-_VlumkDtcwXT3x6OxTtZUQMts_8AfP3xcxgcu6Met98FT"


def notify(message: str) -> None:
    try:
        requests.post(WEBHOOK_URL, json={"content": message}, timeout=10)
    except requests.RequestException as e:
        print(f"[notify] Discord通知失敗: {e}", flush=True)
