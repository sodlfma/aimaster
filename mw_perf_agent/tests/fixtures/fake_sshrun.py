#!/usr/bin/env python3
"""사내 SSH 래퍼 자리표시자. 사용법: fake_sshrun.py <host> cat <path>"""
from __future__ import annotations

import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent


def _select_fixture(remote_path: str) -> Path:
    lowered = remote_path.replace("\\", "/").lower()
    name = lowered.rsplit("/", 1)[-1]
    if "tomcat.env" in lowered or name.endswith(".env"):
        return FIXTURES / "tomcat.env"
    if "httpd" in lowered or name.endswith(".conf"):
        return FIXTURES / "httpd-mpm.conf"
    raise FileNotFoundError(remote_path)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: fake_sshrun.py <host> cat <path>", file=sys.stderr)
        return 2
    host = argv[1]
    remote_path = argv[-1]
    if host in ("fail.example", "denied.local"):
        print(f"Permission denied: {host}", file=sys.stderr)
        return 1
    try:
        fixture = _select_fixture(remote_path)
        sys.stdout.write(fixture.read_text(encoding="utf-8"))
        return 0
    except FileNotFoundError:
        print(f"No such file: {remote_path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
