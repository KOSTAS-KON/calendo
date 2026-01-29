from __future__ import annotations

import secrets


def generate_temp_password(length: int = 14) -> str:
    """Generate a strong but human-typable temporary password.

    Avoids ambiguous characters (0/O, 1/I/l) and includes mixed character classes.
    """
    if length < 10:
        length = 10

    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghijkmnopqrstuvwxyz"
    digits = "23456789"
    symbols = "!@#$%&*_-"
    alphabet = upper + lower + digits + symbols

    # Ensure at least one from each category
    pwd = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    pwd += [secrets.choice(alphabet) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)
