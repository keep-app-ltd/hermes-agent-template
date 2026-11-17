"""Keep-app: merge bare local paths into send_message media (Telegram send_document).

Upstream 0.23.2 extract_media only handles MEDIA: tags. Platform replies already
use extract_local_files; this restores the same for the send_message tool path.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/repo/gateway/outbound/send.py")
NEEDLE = "media_files, cleaned_message = BasePlatformAdapter.extract_media(message)\n"
INSERT = (
    "media_files, cleaned_message = BasePlatformAdapter.extract_media(message)\n"
    "    # keep-app: also pick up bare local paths (e.g. /tmp/report.md) so Telegram\n"
    "    # gets a real send_document instead of only the path as chat text.\n"
    "    local_files, cleaned_message = BasePlatformAdapter.extract_local_files(cleaned_message)\n"
    "    for local_path in BasePlatformAdapter.filter_local_delivery_paths(local_files):\n"
    "        media_files.append((local_path, False))\n"
)

def main() -> None:
    text = TARGET.read_text()
    if "keep-app: also pick up bare local paths" in text:
        print("send.py already patched")
        return
    if NEEDLE not in text:
        raise SystemExit("send.py patch needle not found — rebase required")
    # Only first occurrence in send_message_tool body
    text = text.replace(NEEDLE, INSERT, 1)
    TARGET.write_text(text)
    print("patched", TARGET)

if __name__ == "__main__":
    main()
