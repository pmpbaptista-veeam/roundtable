#!/usr/bin/env python3
"""Best-effort notification for a human handle. Never fails.

Usage: notify.py --to pedro.baptista --message "text"

Two channels, both optional, both swallowing every error — a notification must
never be able to break a coordination action:

1. Desktop, via the OS. This reaches the person only if they are AT the machine
   running the command, which for an agent's own machine means it reaches nobody.
   Humans who want desktop popups should watch their own inbox instead:
   `collab.py inbox --as <handle> --wait` polls on their machine, so its
   notification fires on the right screen by construction.
2. A webhook, if $ROUNDTABLE_NOTIFY_WEBHOOK is set — the only channel that
   crosses machines. The payload is `{"text": "..."}`, which Teams and Slack
   incoming webhooks both accept as-is. Unset by default, so the zero-infra
   story holds.

Stdlib only.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request


def notify(to: str, message: str):
    title = f"Roundtable → {to}"
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{message}" with title "{title}"'],
                capture_output=True, timeout=5,
            )
        elif system == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message],
                           capture_output=True, timeout=5)
        elif system == "Windows":
            script = (
                "[reflection.assembly]::loadwithpartialname('System.Windows.Forms');"
                "$n=new-object system.windows.forms.notifyicon;"
                "$n.icon=[system.drawing.systemicons]::information;$n.visible=$true;"
                f"$n.showballoontip(5000,'{title}','{message}',"
                "[system.windows.forms.tooltipicon]::none)"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, timeout=10)
    except Exception:
        pass
    webhook(title, message)
    print(f"{title}: {message}")


def webhook(title: str, message: str):
    """POST to $ROUNDTABLE_NOTIFY_WEBHOOK. The only channel that reaches a
    human who is not sitting at the machine that ran the command."""
    url = os.environ.get("ROUNDTABLE_NOTIFY_WEBHOOK", "").strip()
    if not url:
        return
    payload = json.dumps({"text": f"{title}: {message}"}).encode()
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=5).close()
    except Exception as exc:
        print(f"note: webhook notification failed ({exc.__class__.__name__}) — "
              "the question is filed regardless", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True)
    parser.add_argument("--message", required=True)
    args = parser.parse_args()
    notify(args.to, args.message)
    sys.exit(0)


if __name__ == "__main__":
    main()
