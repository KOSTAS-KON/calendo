from __future__ import annotations

def test_import_app():
    # Lightweight import test (does not start server)
    import app.main  # noqa: F401
