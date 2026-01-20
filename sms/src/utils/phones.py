import re

def normalize_gr_msisdn(s: str) -> str:
    """Normalize to Greek MSISDN: 30 + 10 digits (e.g., 3069XXXXXXXX)."""
    if s is None:
        return ""
    digits = re.sub(r"\D+", "", str(s))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("30") and len(digits) == 12:
        return digits
    if digits.startswith("69") and len(digits) == 10:
        return "30" + digits
    if digits.startswith("0") and len(digits) == 11 and digits[1] == "6":
        return "30" + digits[1:]
    return ""
