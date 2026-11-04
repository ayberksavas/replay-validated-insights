"""Text patterns used by the knowledge base. Duplicated from the oracle on purpose (predictors must not import it)."""
import re

FIX_RE = re.compile(r"\b(fix(e[sd])?|bug(fix)?|hotfix|regression|resolve[sd]?|patch)\b", re.I)
REVERT_RE = re.compile(r"^\s*revert\b", re.I)
REF_RE = re.compile(r"(?<![\w/])#(\d{1,6})\b")
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d{1,6})\b", re.I)


def refs(text: str) -> set[int]:
    """Issue/PR numbers mentioned as #N."""
    return {int(m) for m in REF_RE.findall(text or "")}


def closes(text: str) -> set[int]:
    """Numbers named by 'fixes #N' / 'closes #N' / 'resolves #N'."""
    return {int(m) for m in CLOSES_RE.findall(text or "")}


def is_fix_message(message: str) -> bool:
    first = (message or "").split("\n")[0]
    return bool(FIX_RE.search(first)) and not REVERT_RE.match(first)
