from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request

from dotenv import load_dotenv

API_URL = "https://ws.audioscrobbler.com/2.0/"


def signature(params: dict[str, str], secret: str) -> str:
    payload = {key: str(value) for key, value in params.items() if key != "format"}
    raw = "".join(f"{key}{payload[key]}" for key in sorted(payload))
    return hashlib.md5((raw + secret).encode("utf-8")).hexdigest()


def api_get(params: dict[str, str]) -> dict:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{API_URL}?{query}",
        headers={"User-Agent": "RuMusicBot-LastfmSetup/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(
            f"Last.fm lỗi {payload.get('error')}: {payload.get('message')}"
        )
    return payload


def main() -> None:
    load_dotenv()
    api_key = (os.getenv("LASTFM_API_KEY") or "").strip()
    api_secret = (os.getenv("LASTFM_API_SECRET") or "").strip()
    if not api_key or not api_secret:
        raise SystemExit(
            "Cần LASTFM_API_KEY và LASTFM_API_SECRET trong .env trước khi chạy."
        )

    token_data = api_get(
        {
            "method": "auth.getToken",
            "api_key": api_key,
            "format": "json",
        }
    )
    token = str(token_data["token"])

    auth_url = (
        "https://www.last.fm/api/auth/?api_key="
        f"{urllib.parse.quote(api_key)}&token={urllib.parse.quote(token)}"
    )
    print("Mở link sau, đăng nhập Last.fm và bấm Authorize:")
    print(auth_url)
    input("Sau khi Authorize xong, nhấn Enter để lấy session key...")

    params = {
        "method": "auth.getSession",
        "api_key": api_key,
        "token": token,
    }
    params["api_sig"] = signature(params, api_secret)
    params["format"] = "json"
    data = api_get(params)
    session = data.get("session")
    if not isinstance(session, dict):
        raise SystemExit("Last.fm không trả về session.")

    print("\nThành công. Thêm các dòng sau vào .env:\n")
    print(f"LASTFM_SESSION_KEY={session.get('key', '')}")
    print(f"LASTFM_USERNAME={session.get('name', '')}")
    print(
        "\nLưu ý: session key là bí mật, không commit file .env lên git."
    )


if __name__ == "__main__":
    main()
