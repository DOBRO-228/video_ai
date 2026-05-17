from __future__ import annotations


def compact_join(parts: list[str]) -> str:
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def word_count(text: str) -> int:
    return len([token for token in text.split() if token.strip()])

