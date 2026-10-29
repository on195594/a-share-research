"""Mint a short-lived owner session for browser checks, without GitHub.

Only someone with shell access to the data directory (the host, or `docker exec` into the
web container) can run this, which already implies owner rights; nothing is exposed over HTTP.
Prints the value for the `session` cookie; logging out deletes it like any other session.
"""

import argparse
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from a_share_research import db


def mint(conn, minutes: int, *, agent: bool = False) -> str:
    if not 1 <= minutes <= 480:
        raise ValueError("会话有效期须在 1–480 分钟之间")
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions VALUES (?, ?)",
                 (("agent:" if agent else "") + hashlib.sha256(token.encode()).hexdigest(), time.time() + minutes * 60))
    return token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=int, default=60, choices=range(1, 481), metavar="1-480")
    parser.add_argument("--agent", action="store_true", help="mint a bearer token for the research API only")
    parser.add_argument("--storage-state", type=Path, help="write private Playwright browser state instead of printing a token")
    args = parser.parse_args()
    origin = urlparse(os.environ.get("PUBLIC_BASE_URL", ""))
    if args.storage_state and (args.agent or origin.scheme not in ("http", "https") or not origin.hostname
                               or origin.username or origin.password):
        parser.error("--storage-state requires a browser session and a valid PUBLIC_BASE_URL")
    conn = db.connect()
    try:
        if args.storage_state:
            # Exclusive creation keeps an existing credential file intact.
            descriptor = os.open(args.storage_state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with db.transaction(conn), os.fdopen(descriptor, "w") as file:
                    token = mint(conn, args.minutes)
                    cookie = {"name": "session", "value": token, "domain": origin.hostname, "path": "/",
                              "httpOnly": True, "secure": origin.scheme == "https", "sameSite": "Lax",
                              "expires": time.time() + args.minutes * 60}
                    json.dump({"cookies": [cookie], "origins": []}, file)
                    file.flush()
            except BaseException:
                args.storage_state.unlink()
                raise
            logging.getLogger(__name__).info("Browser state saved to %s; log out and remove it after use", args.storage_state)
        else:
            print(mint(conn, args.minutes, agent=args.agent))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
