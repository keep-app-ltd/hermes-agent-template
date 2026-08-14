"""Load the keep-app evidence-integrity hook in every Hermes Python process."""

try:
    from keepapp.install_hook import install_import_hook

    install_import_hook()
except Exception:
    pass
