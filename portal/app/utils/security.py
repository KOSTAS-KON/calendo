from __future__ import annotations

import secrets


def generate_temp_password(length: int = 14) -> str:
<<<<<<< HEAD
    """Generate a strong temporary password suitable for onboarding/reset.

    Avoids easily confused characters (0/O, 1/I/l) and includes a safe symbol set.
=======
    """Generate a strong temporary password.

    Human-friendly temporary password generator:
    - avoids ambiguous characters (0/O, 1/I/l)
    - includes letters, digits, and a safe symbol set
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
    """
    if length < 10:
        length = 10

<<<<<<< HEAD
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ" "abcdefghijkmnopqrstuvwxyz" "23456789" "!@#$%&*_-"
    return "".join(secrets.choice(alphabet) for _ in range(length))
=======
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

    # shuffle
    rng = secrets.SystemRandom()
    rng.shuffle(pwd)
    return "".join(pwd)
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
