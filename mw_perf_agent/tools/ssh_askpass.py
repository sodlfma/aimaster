#!/usr/bin/env python3
"""SSH_ASKPASS helper. Prints MW_SSH_PASSWORD; never logs it.

OpenSSH may pass the prompt text as argv[1]. Host-key yes/no prompts are
refused so a login password cannot accidentally accept a changed host key.
"""
from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    prompt = " ".join(argv[1:]).lower()
    if "yes/no" in prompt or "fingerprint" in prompt:
        return 1
    sys.stdout.write(os.environ.get("MW_SSH_PASSWORD", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
