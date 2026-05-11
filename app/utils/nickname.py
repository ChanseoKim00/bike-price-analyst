"""
Random nickname generator — used to assign an automatic nickname for new social-login signups.

Format: adjective + noun + 3-digit number (e.g. "BraveRider122", "SwiftRacer356").
On collision, the number is re-rolled; after enough failures, expand to 4 digits.
"""
import random

from ..models import User, db


_ADJECTIVES = [
    "Brave", "Swift", "Lucky", "Happy", "Wild", "Calm", "Bold", "Clever",
    "Bright", "Mighty", "Nimble", "Sunny", "Lively", "Gentle", "Fierce", "Witty",
    "Quick", "Cheerful", "Steady", "Daring",
]

_NOUNS = [
    "Rider", "Racer", "Climber", "Sprinter", "Tourer", "Pedaler", "Cyclist", "Cruiser",
    "Pacer", "Roller", "Voyager", "Wanderer", "Explorer", "Pilot", "Drifter", "Champion",
    "Ranger", "Scout", "Pioneer", "Trekker",
]

_MAX_RETRIES_3DIGIT = 10
_MAX_RETRIES_4DIGIT = 20


def generate_random_nickname(digits: int = 3) -> str:
    """Adjective + noun + N-digit number."""
    low = 10 ** (digits - 1)
    high = 10 ** digits - 1
    return f"{random.choice(_ADJECTIVES)}{random.choice(_NOUNS)}{random.randint(low, high)}"


def generate_unique_nickname() -> str:
    """
    Return a nickname that does not collide with the users.nickname UNIQUE constraint.
    Try 3-digit numbers up to 10 times → fall back to 4-digit numbers up to 20 times → raise on failure.
    """
    for _ in range(_MAX_RETRIES_3DIGIT):
        nickname = generate_random_nickname(digits=3)
        if not User.query.filter_by(nickname=nickname).first():
            return nickname

    for _ in range(_MAX_RETRIES_4DIGIT):
        nickname = generate_random_nickname(digits=4)
        if not User.query.filter_by(nickname=nickname).first():
            return nickname

    raise RuntimeError("Nickname generation retry limit exceeded")
