"""Offline token estimates, not a substitute for the model's actual tokenizer."""

import re


def local_tokenizer(text: str) -> list[str]:
    # Chinese characters / punctuation individually; long ASCII runs in 4-char
    # pieces. No remote tiktoken vocabulary download is needed at startup.
    return re.findall(r"[A-Za-z0-9_]{1,4}|[^\sA-Za-z0-9_]", text)
