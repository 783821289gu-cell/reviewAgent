from collections import Counter
import math
import re


TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.I)


def embed_text(text: str) -> dict[str, float]:
    tokens = _tokenize(text)
    if not tokens:
        return {}

    counts = Counter(tokens)
    length = math.sqrt(sum(value * value for value in counts.values()))
    if length == 0:
        return {}
    return {token: value / length for token, value in counts.items()}


def cosine_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(token, 0.0) for token, value in left.items())


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw_token in TOKEN_PATTERN.findall(str(text or "").lower()):
        if _is_chinese(raw_token):
            tokens.append(raw_token)
            tokens.extend(raw_token[index : index + 2] for index in range(max(0, len(raw_token) - 1)))
        else:
            tokens.append(raw_token)
    return [token for token in tokens if token]


def _is_chinese(token: str) -> bool:
    return all("\u4e00" <= char <= "\u9fff" for char in token)
