from cleanup import clean

CASES = [
    ("Um, so I think we should, uh, ship it.", "So I think we should ship it."),
    ("Let's meet at 3, no wait, 4.", "Let's meet at 4."),
    ("Send it to John, no wait, to Sarah.", "Send it to Sarah."),
    ("This is wrong. Scratch that. This is right.", "This is right."),
    ("Hello there new line how are you", "Hello there\nHow are you"),
    ("first point new paragraph second point", "First point\n\nSecond point"),
    ("[BLANK_AUDIO]", ""),
    ("I think the the plan is fine", "I think the plan is fine"),
    ("i opened cloud code today", "I opened Claude Code today"),
    ("hmm okay that works", "Okay that works"),
    # real whisper output from the end-to-end test
    ("So I think we should, uh, meet at 3, no wait, 4 o'clock tomorrow.\n New line.\n Thanks for using Cloud Code.",
     "So I think we should meet at 4 o'clock tomorrow.\nThanks for using Claude Code."),
    ("I'll bring cake, no wait, pie.", "I'll bring pie."),
    ("Call Mom, sorry I mean call Dad.", "Call Dad."),
    ("Done. New paragraph. Next topic", "Done.\n\nNext topic"),
]


def test_cases():
    d = {"cloud code": "Claude Code"}
    for raw, want in CASES:
        got = clean(raw, d)
        assert got == want, f"{raw!r}\n  got:  {got!r}\n  want: {want!r}"


if __name__ == "__main__":
    fails = 0
    d = {"cloud code": "Claude Code"}
    for raw, want in CASES:
        got = clean(raw, d)
        ok = got == want
        fails += not ok
        print(("PASS" if ok else "FAIL"), repr(raw), "->", repr(got), "" if ok else f"(want {want!r})")
    raise SystemExit(fails)
