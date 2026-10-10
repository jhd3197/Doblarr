"""What the dialogue says about who each voice is.

People in a script name each other. "What is it, Mina?" names the person
spoken to, and the next line in another voice, a moment later, is usually
Mina answering. "Kaito-sensei!" says the speaker is not Kaito. Read over a
whole episode, these clues point at a voice group's name: the name it answers
to, and not a name it calls. That is what catches a group named after a
character the scene only talks about.

Plain text rules, no model: a candidate name is one of the show's cast (or an
alias), or a capitalised word the episode uses as a name at least twice
(called out, or with an honorific). The result is evidence shown beside the
group, never a name by itself.
"""

from __future__ import annotations

import re

READER = "dialogue-clues/1"
ANSWER_GAP = 6.0         # seconds: a reply later than this is not an answer
LOOKAHEAD = 2            # lines after a call where the answer may come

# Sibling honorifics are also written as their own word: "Kaito Nii-chan".
_SIBLING = r"(?:[Oo]?[Nn](?:ii|ee)(?:-?(?:chan|san|sama))?)"
HONORIFIC = (r"(?:-(?:sama|sensei|san|kun|chan|dono|senpai|sempai|nee|nii|neechan|niichan"
             rf"|nee-chan|nii-chan)|\s{_SIBLING}\b)")
# A name called out: at the start or after a pause, alone up to the next
# punctuation ("Mina!", "Well done, Mina.", "Yes, Ren-sama!"), or anywhere
# with an honorific ("Kaito-sensei's").
_CALLED = re.compile(rf"(?:^|[,.!?…;:]\s*)([A-Z][a-z]+){HONORIFIC}?\s*(?=[!?.,…]|$)")
_HONOURED = re.compile(rf"\b([A-Z][a-z]+){HONORIFIC}")
_CAPITAL = re.compile(r"\b([A-Z][a-z]+)\b")

# Words that open sentences or name ranks and places, not people.
_WORDS = """
I A An The Then Well Okay Ok Yes No Oh Ah Eh Hey Huh Hm Hmm Heh Geez Gee What Why How Who
Where When Is Are Do Does Did So And But Or If Now Thanks Thank Sorry Please Hello Hi Yo Bye
Wait Listen Look Show Come Go Stop Next Good Nice Great Right Sure Fine Nothing Everyone
Absolutely Really Seriously However Besides Anyway Still Just Maybe Not Let Lets Don't Yeah
Captain Sir Madam Lord Lady Master Sensei Teacher Boss Chief Sister Brother Mom Dad Father Mother
Grandpa Grandma Uncle Aunt Doctor Mister Miss Status Opponent Exactly Mm Uh Um Whoa Wow Damn
Nii Nee Onii Onee Niichan Neechan
"""
_NOT_NAMES = frozenset(_WORDS.split())


def _cue(seg) -> str:
    return str(((seg.get("cue") or {}).get("cue_id")) if isinstance(seg, dict) else "")


def candidates(lines: list[dict], cast: list[str] | None = None) -> dict[str, str]:
    """Folded name -> spelling, for names this episode can be talking about."""
    known: dict[str, str] = {}
    for name in cast or []:
        for part in str(name).split():
            if len(part) > 1:
                known.setdefault(part.casefold(), part[:1].upper() + part[1:])
    seen: dict[str, int] = {}
    for line in lines:
        text = str(line.get("text") or "")
        for match in (*_CALLED.finditer(text), *_HONOURED.finditer(text)):
            word = match.group(1)
            if word not in _NOT_NAMES:
                seen[word] = seen.get(word, 0) + 1
    text = " ".join(str(line.get("text") or "") for line in lines)
    lowered = {w.casefold() for w in re.findall(r"\b[a-z]+\b", text)}
    # "the previous Lord", "our Captain": a word that takes an article is a
    # rank or a thing, whatever its capital letter.
    titles = {w.casefold() for w in re.findall(
        r"\b(?:the|a|an|our|my|your|his|her|their|this|that)\s+(?:\w+\s+)?([A-Z][a-z]+)", text)}
    for word, count in seen.items():
        # A word the script also writes in lower case is a word, not a name.
        if count >= 2 and word.casefold() not in lowered | titles:
            known.setdefault(word.casefold(), word)
    return known


def called(text: str, names: dict[str, str]) -> list[str]:
    """Names the line calls out (the people spoken to), in order, once each."""
    found: list[str] = []
    for match in (*_CALLED.finditer(text), *_HONOURED.finditer(text)):
        name = names.get(match.group(1).casefold())
        if name and name not in found:
            found.append(name)
    return found


def read(lines: list[dict], cast: list[str] | None = None) -> dict:
    """Per voice group: names it answers to, names it calls, and a suggestion.

    `lines` are {speaker, start, end, text, cue}. A suggestion is the name a
    group answers to most, at least twice and at least half of its answers,
    and not a name the same group calls.
    """
    names = candidates(lines, cast)
    answers: dict[str, dict[str, list[str]]] = {}
    calls: dict[str, dict[str, list[str]]] = {}
    for i, line in enumerate(lines):
        voice = str(line.get("speaker") or "")
        for name in called(str(line.get("text") or ""), names):
            calls.setdefault(voice, {}).setdefault(name, []).append(str(line.get("cue") or ""))
            for j in range(i + 1, min(i + 1 + LOOKAHEAD, len(lines))):
                after = lines[j]
                if float(after["start"]) - float(line["end"]) > ANSWER_GAP:
                    break
                if str(after.get("speaker") or "") != voice:
                    answers.setdefault(str(after.get("speaker") or ""), {}).setdefault(
                        name, []).append(str(after.get("cue") or ""))
                    break
    out = {}
    for voice in sorted({str(line.get("speaker") or "") for line in lines}):
        heard = sorted(((n, cues) for n, cues in (answers.get(voice) or {}).items()),
                       key=lambda kv: -len(kv[1]))
        said = sorted(((n, cues) for n, cues in (calls.get(voice) or {}).items()),
                      key=lambda kv: -len(kv[1]))
        total = sum(len(c) for _, c in heard)
        suggestion = ""
        # Nobody answers to a name they call themselves.
        fitting = [(n, cues) for n, cues in heard if n not in (calls.get(voice) or {})]
        if fitting:
            name, cues = fitting[0]
            if len(cues) >= 2 and len(cues) * 2 >= total:
                suggestion = name
        out[voice] = {"answers": [{"name": n, "count": len(c), "cues": c} for n, c in heard[:4]],
                      "calls": [{"name": n, "count": len(c)} for n, c in said[:4]],
                      "suggests": suggestion}
    return {"reader": READER, "groups": out}
