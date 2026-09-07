"""Shared JMA intensity labels and desktop notification thresholds."""

import math
import unicodedata


def normalize_intensity(value):
    """Normalize categorical JMA labels, without interpreting them as magnitude."""
    code = unicodedata.normalize("NFKC", str(value)).strip()
    code = code.replace("弱", "-").replace("強", "+").replace("强", "+")
    return code.replace("−", "-")


def meets_notification_threshold(message):
    """Notify for maximum intensity >= 4 OR magnitude >= 5.0 (inclusive)."""
    intensity = normalize_intensity(message.get("MaxIntensity"))
    if intensity in {"4", "5-", "5+", "6-", "6+", "7"}:
        return True

    value = message.get("Magunitude")  # Spelling used by the Wolfx API.
    if isinstance(value, bool):
        return False
    try:
        magnitude = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(magnitude) and magnitude >= 5.0
