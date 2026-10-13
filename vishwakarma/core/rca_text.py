import re

_HEADING = re.compile(r"^#{1,3} \S", re.M)
MAX_PREAMBLE = 800


def clean_rca(text: str) -> str:
    text = (text or "").strip()
    m = _HEADING.search(text)
    if not m or m.start() == 0 or m.start() > MAX_PREAMBLE:
        return text
    head = text[:m.start()]
    if _HEADING.search(head):
        return text
    return text[m.start():].strip()
