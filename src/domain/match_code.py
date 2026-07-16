from __future__ import annotations

import hashlib
import secrets


MATCH_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
MATCH_CODE_LENGTH = 4


def random_match_code() -> str:
    return "".join(
        secrets.choice(MATCH_CODE_ALPHABET)
        for _ in range(MATCH_CODE_LENGTH)
    )


def deterministic_match_code(seed: str, attempt: int = 0) -> str:
    digest = hashlib.sha256(f"{seed}:{attempt}".encode()).digest()
    value = int.from_bytes(digest[:8], "big")
    characters: list[str] = []
    for _ in range(MATCH_CODE_LENGTH):
        value, index = divmod(value, len(MATCH_CODE_ALPHABET))
        characters.append(MATCH_CODE_ALPHABET[index])
    return "".join(characters)
