from __future__ import annotations

import secrets


def generate_temp_password(length: int = 14) -> str:
    """
    Generate a strong, human-friendly temporary password.

    - avoids ambiguous characters (0/O, 1/I/l)
    - includes uppercase, lowercase, digits, and safe symbols
    - guarantees at least one character from each category
    """
    if length < 10:
        length = 10

    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghijkmnopqrstuvwxyz"
    digits = "23456789"
    symbols = "!@#$%&*_-"
    alphabet = upper + lower + digits + symbols

    # ensure at least one of each category
    pwd = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    pwd += [secrets.choice(alphabet) for _ in range(length - 4)]

    rng = secrets.SystemRandom()
    rng.shuffle(pwd)
    return "".join(pwd)
