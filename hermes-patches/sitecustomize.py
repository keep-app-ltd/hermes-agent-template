"""Load the keep-app evidence-integrity hook in every Hermes Python process."""

import sys

if "/opt/hermes-agent" not in sys.path:
    sys.path.insert(0, "/opt/hermes-agent")

try:
    from keepapp.install_hook import install_import_hook

    install_import_hook()
except Exception:
    pass
