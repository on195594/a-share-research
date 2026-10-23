"""Mint a short-lived owner session for browser checks, without GitHub.

Only someone with shell access to the data directory (the host, or `docker exec` into the
web container) can run this, which already implies owner rights; nothing is exposed over HTTP.
Prints the value for the `session` cookie; logging out deletes it like any other session.
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import time

from a_share_research import db


def mint(conn, minutes: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions VALUES (?, ?)",
                 (hashlib.sha256(token.encode()).hexdigest(), time.time() + minutes * 60))  # same hash as web._hash
    return token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=int, default=60, choices=range(1, 481), metavar="1-480")
    args = parser.parse_args()
    conn = db.connect()
    print(mint(conn, args.minutes))
    conn.close()


if __name__ == "__main__":
    main()
