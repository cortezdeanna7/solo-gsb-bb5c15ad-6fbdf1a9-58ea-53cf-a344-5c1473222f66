"""Shared pytest configuration.

The evidence module refuses to start without an EVIDENCE_SECRET_KEY of at
least 32 bytes. Set a test-only default here, before any test module
imports the app; individual tests may monkeypatch the variable to exercise
the missing/short-key startup failure paths.
"""

import os

os.environ.setdefault(
    "EVIDENCE_SECRET_KEY", "test-evidence-secret-key-0123456789abcdef"
)
