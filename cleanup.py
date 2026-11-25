"""Turn raw speech-to-text output into clean, typed text.

Everything here is plain rule-based Python: no network, no model.
An optional local-LLM polish step lives in hushtype.py.
"""
import re

FILLERS = r"(?:u+m+|u+h+m*|e+r+m*|a+h+|h+m+|m+h*m+)"

# "X, no wait, Y" -> replaces the tail of X with Y
CORRECTION_TRIGGERS = [
    r"no wait", r"no,? sorry", r"sorry,? i mean", r"no,? i mean", r"i mean,? no",
    r"wait,? no", r"correction",
]
# "... scratch that ..." -> drops the previous sentence/clause
SCRATCH_TRIGGERS = [r"scratch that", r"delete that", r"strike that"]


def _strip_tags(t: str) -> str:
    # whisper.cpp emits things like [BLANK_AUDIO], (music), [ Silence ]
    return re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*", " ", t)


def _remove_fillers(t: str) -> str:
    # "should, uh, ship" -> "should ship"; "Um, so" -> "so"
    return re.sub(rf"(?i),?\s*\b{FILLERS}\b,?", " ", t)


def _dedupe_words(t: str) -> str:
    # "the the" -> "the"   (case-insensitive, whole words only)
    return re.sub(r"(?i)\b(\w+)(\s+\1\b)+", r"\1", t)


def _voice_commands(t: str) -> str:
    # keep sentence-ending punctuation before the command, drop a comma
    t = re.sub(r"(?i)\s*,?\s*\bnew paragraph\b[\s,.]*", "\n\n", t)
    t = re.sub(r"(?i)\s*,?\s*\bnew line\b[\s,.]*", "\n", t)
    return t


def _scratch(t: str) -> str:
    pat = re.compile(rf"(?i)[\s,.]*\b(?:{'|'.join(SCRATCH_TRIGGERS)})\b[\s,.!]*")
    while True:
        m = pat.search(t)
        if not m:
            return t
        before, after = t[: m.start()], t[m.end():]
        # drop back to the previous sentence boundary
        cut = max(before.rfind("."), before.rfind("!"), before.rfind("?"), before.rfind("\n"))
        before = before[: cut + 1] if cut >= 0 else ""
        t = (before + " " + after).strip()


def _norm(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


def _is_num(w: str) -> bool:
    return bool(re.fullmatch(r"\d+(?::\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|"
                             r"eleven|twelve|noon|midnight", _norm(w)))


def _splice(clause: list, repl: list) -> list:
    """Decide which tail of `clause` the spoken correction `repl` replaces."""
    if not repl:
        return clause
    if not clause:
        return repl
    first = _norm(repl[0])
    # 1. "send it to John, no wait, to Sarah" -> restart from the last matching word
    for i in range(len(clause) - 1, -1, -1):
        if _norm(clause[i]) == first:
            return clause[:i] + repl
    # 2. "meet at 3, no wait, 4 o'clock" -> replace from the last number
    if _is_num(repl[0]):
        for i in range(len(clause) - 1, -1, -1):
            if _is_num(clause[i]):
                return clause[:i] + repl
    # 3. "bring cake, no wait, pie" -> swap the same number of trailing words
    n = min(len(repl), max(len(clause) - 1, 1))
    return clause[: len(clause) - n] + repl


def _corrections(t: str) -> str:
    pat = re.compile(rf"(?i)[\s,.]*\b(?:{'|'.join(CORRECTION_TRIGGERS)})\b[\s,.]*")
    while True:
        m = pat.search(t)
        if not m:
            return t
        before, after = t[: m.start()], t[m.end():]
        # replacement = words after trigger up to the next sentence end
        end = re.search(r"[.!?\n]", after)
        repl, rest = (after[: end.start()], after[end.start():]) if end else (after, "")
        repl_words = repl.split()
        # sentence-local part of `before`
        cut = max(before.rfind("."), before.rfind("!"), before.rfind("?"), before.rfind("\n"))
        head, clause = before[: cut + 1], before[cut + 1:]
        clause_words = clause.split()
        clause_words = _splice(clause_words, repl_words)
        t = (head + " " + " ".join(clause_words) + rest).strip()


def _apply_dictionary(t: str, dictionary: dict) -> str:
    for spoken, written in (dictionary or {}).items():
        t = re.sub(rf"(?i)\b{re.escape(spoken)}\b", written, t)
    return t


def _tidy(t: str) -> str:
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)          # no space before punctuation
    t = re.sub(r"([,.!?;:])(?=[A-Za-z])", r"\1 ", t)  # space after punctuation
    t = re.sub(r",\s*([.!?])", r"\1", t)               # ", ." -> "."
    t = re.sub(r"^[\s,.;:]+", "", t)                   # leading junk
    t = re.sub(r",+", ",", t)
    t = t.strip(" ,")
    # capitalise sentence starts
    t = re.sub(r"(^|[.!?]\s+|\n)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    t = re.sub(r"\bi\b", "I", t)
    return t


def clean(text: str, dictionary: dict | None = None) -> str:
    # whisper splits segments with newlines; real line breaks only come from voice commands
    t = re.sub(r"\s+", " ", _strip_tags(text))
    t = _remove_fillers(t)
    t = _dedupe_words(t)
    t = _scratch(t)
    t = _corrections(t)
    t = _voice_commands(t)
    t = _apply_dictionary(t, dictionary or {})
    return _tidy(t)


if __name__ == "__main__":
    import sys
    print(clean(" ".join(sys.argv[1:]) or sys.stdin.read()))
