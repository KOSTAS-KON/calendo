from __future__ import annotations

import secrets


def generate_temp_password(length: int = 14) -> str:
    """Generate a strong temporary password suitable for onboarding/reset.

    Avoids easily confused characters (0/O, 1/I/l) and includes a safe symbol set.
    """
    if length < 10:
        length = 10

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ" "abcdefghijkmnopqrstuvwxyz" "23456789" "!@#$%&*_-"
    return "".join(secrets.choice(alphabet) for _ in range(length))
