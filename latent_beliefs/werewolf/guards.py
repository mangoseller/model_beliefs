"""Answer-consistency guards used by the strict Werewolf parser.

A strict answer is rejected when its option letter contradicts the option text
it repeats, when a textual vote answer names a different player, or when a
late answer draft is reopened and not resolved by a later, guarded commitment
in the same direction.  The commitment detector below supplies those later
commitments; it never produces a parsed value on its own.
"""

from __future__ import annotations

import re

# Option texts of the two five-category questions, mapped to their codes.
LIKERT_PHRASES = {
    "very unlikely": 1.0,
    "somewhat unlikely": 2.0,
    "neither likely nor unlikely": 3.0,
    "somewhat likely": 4.0,
    "very likely": 5.0,
}
PROBABILITY_BIN_PHRASES = {
    "less than 10%": 1.0,
    "10% to less than 20%": 2.0,
    "20% to less than 33.3%": 3.0,
    "33.3% to less than 50%": 4.0,
    "50% or higher": 5.0,
}


def ordinal_phrases(family: str) -> dict[str, float]:
    return LIKERT_PHRASES if family == "likert" else PROBABILITY_BIN_PHRASES


# Definitional / conditional context immediately before a match, e.g.
# "a false positive means prioritizing ...", "to determine whether to screen".
_DEFN_GUARD = re.compile(
    r"(?:false positive|false negative|cost of|when|whether to|to determine|"
    r"to decide|to assess|to recommend|conditions under which|if )",
    re.IGNORECASE,
)

# Inability / open-question context immediately before a match.
_NONCOMMIT_NEAR = re.compile(
    r"(?:cannot|can't|could not|couldn't|unable to|not possible to|impossible|"
    r"do not know|don't know|insufficient (?:evidence|information)|"
    r"not enough (?:evidence|information)|no (?:clear )?(?:basis|way) to|"
    r"unclear whether|uncertain whether|whether)",
    re.IGNORECASE,
)
_HEDGED_NEAR = re.compile(
    r"\b(?:might|may|could|possibly|perhaps|tentatively)\b", re.IGNORECASE
)

# Cues that make the sentence containing a commitment conclusion-like.
_CONCLUSION_CUE = re.compile(
    r"\b(?:therefore|thus|overall|i would|we would|i will|i'd|recommend|"
    r"recommendation|final|conclusion|conclude|in summary|to summarize|"
    r"in conclusion|(?:best|correct|rational|reasonable|safest|optimal|"
    r"prudent|wise|proper|defensible|conservative|good)\s+"
    r"(?:course|option|choice|decision|action|answer|recommendation|strategy|thing))\b",
    re.IGNORECASE,
)

# Local negation inside a matched commitment clause.
_LOCAL_NEGATION = re.compile(
    r"\b(?:not|n't|never|refus\w*|advise[sd]? against|against)\b", re.IGNORECASE
)


def _negated_before_action(match: re.Match[str]) -> bool:
    """True when negation sits in the clause segment directly before the
    commitment action word (group 1). Segments are split on commas and
    semicolons so "would not be advisable, and I will therefore decline the
    bet" does not double-negate the later "decline"."""
    return _LOCAL_NEGATION.search(_local_clause_before(match.string, match.start(1))) is not None


def _negated_predicate(match: re.Match[str]) -> bool:
    """Reject subject-predicate claims whose favorable predicate is negated."""
    predicate = re.search(
        r"\b(?:is|would be|seems|has|appears)\b", match.group(0), re.IGNORECASE
    )
    if predicate is None:
        return False
    return _LOCAL_NEGATION.search(match.group(0)[predicate.end() :]) is not None


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    s = max(
        text.rfind(".", 0, start),
        text.rfind("\n", 0, start),
        text.rfind("!", 0, start),
        text.rfind("?", 0, start),
    )
    ends = [i for i in (text.find(c, end) for c in ".\n!?") if i != -1]
    return s + 1, (min(ends) if ends else len(text))


def _local_clause_before(text: str, pos: int) -> str:
    """Return the clause containing ``pos``, without crossing a sentence.

    Sentence and contrast boundaries delimit the semantic scope of the
    definitional, conditional, and negation guards.
    """
    sentence_start, _ = _sentence_bounds(text, pos, pos)
    segment = text[sentence_start:pos]
    return re.split(r"[,;]|\b(?:but|however|nevertheless)\b|[—–]", segment, flags=re.I)[
        -1
    ]


def _definitional_context(text: str, pos: int) -> bool:
    return _DEFN_GUARD.search(_local_clause_before(text, pos)) is not None


def _noncommittal_context(text: str, pos: int) -> bool:
    return _NONCOMMIT_NEAR.search(_local_clause_before(text, pos)) is not None


def _conditional_context(text: str, start: int, end: int) -> bool:
    """Return whether the action belongs to a conditional sentence.

    A short fixed-width lookback misses definitions such as ``if we think
    ... we should accept the bet`` when the condition itself is more than a
    few dozen characters before the action.  Inspecting the whole sentence
    keeps those hypothetical rules from becoming inferred commitments.
    """
    sentence_start, sentence_end = _sentence_bounds(text, start, end)
    before_action = text[sentence_start:start]
    after_action = re.split(
        r"[,;]|[—–]|\b(?:because|since|as)\b",
        text[end:sentence_end],
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    conditional = re.compile(
        r"\b(?:if|unless|depending\s+on|"
        r"(?:situation|scenario|case)\s+in\s+which|"
        r"in\s+(?:that|such)\s+(?:a\s+)?case)\b",
        re.IGNORECASE,
    )
    branch_otherwise = re.search(
        r"(?:^|[.;\n])\s*otherwise\s*[,;:]",
        before_action,
        re.IGNORECASE,
    )
    return (
        conditional.search(before_action) is not None
        or conditional.search(after_action) is not None
        or branch_otherwise is not None
    )


def _conclusion_like(text: str, start: int, end: int) -> bool:
    """Commitment must sit in a conclusion-like sentence or the text's tail."""
    s, e = _sentence_bounds(text, start, end)
    if _CONCLUSION_CUE.search(text[s:e]):
        return True
    return len(text) > 0 and start >= 0.75 * len(text)


_BET_COMMIT = re.compile(
    r"\b(?:"
    r"(?:i|we|you|one|the\s+focal\s+player|a\s+reasonable\s+person)\s+"
    r"(?:therefore\s+)?(?:(?:would|will|should|must|ought\s+to)\s+)?"
    r"(?:therefore\s+)?(?:not\s+(?:simply\s+)?)?"
    r"(?:recommend(?:s|ed|ing)?\s+)?"
    r"(?:that\s+(?:i|we|you|one)\s+(?:should|would)\s+|to\s+)?|"
    r"(?:(?:my|our|the)\s+)?recommendation\s*"
    r"(?:is|would\s+be|:)\s+(?:not\s+(?:simply\s+)?)?(?:to\s+)?|"
    r"(?:it\s+(?:is|would\s+be)\s+)?(?:recommended|rational|reasonable|"
    r"wise|better|best|prudent|safest|optimal|advisable)\s+to\s+|"
    r"(?:the\s+)?(?:most\s+)?(?:likely\s+)?(?:best|correct|rational|"
    r"reasonable|safest|optimal|prudent|wise|proper|defensible|"
    r"conservative|good)\s+(?:decision|choice|answer|recommendation|"
    r"strategy|course|option|thing(?:\s+to\s+do)?)\s+"
    r"(?:is|would\s+be|leans?\s+towards?)?\s*(?:to\s+)?|"
    r"one\s+would\s+be\s+expected\s+to\s+)"
    r"(accept(?:s|ed|ing)?|declin(?:e|es|ed|ing)|"
    r"refus(?:e|es|ed|ing)|reject(?:s|ed|ing)?)\s+(?:th(?:e|is)\s+)?bet",
    re.IGNORECASE,
)
_BET_SUBJECT = re.compile(
    r"\b(accept(?:ing)?|declin(?:e|ing)|reject(?:ing)?|refus(?:e|ing))\s+"
    r"(?:th(?:e|is)\s+)?bet\s+"
    r"(?:is|would\s+be|seems(?:\s+to\s+be|\s+like)?|appears(?:\s+to\s+be)?)\s+"
    r"(?:clearly\s+|generally\s+|probably\s+|the\s+|a\s+|an\s+|more\s+)*"
    r"(?:favorable|good|beneficial|reasonable|advisable|positive|best|"
    r"prudent|safe|safer|better|wise|worthwhile|conservative)\b",
    re.IGNORECASE,
)
_BET_PASSIVE = re.compile(
    r"\b(?:the|this)\s+bet\s+(?:should|would|will|must|ought\s+to)\s+"
    r"(not\s+)?(?:be\s+)?(accept(?:ed)?|declin(?:ed)?|reject(?:ed)?|refus(?:ed)?)\b",
    re.IGNORECASE,
)
_BET_MAKE_CHOICE = re.compile(
    r"\bmake(?:s|ing)?\s+(accept(?:ing)?|declin(?:e|ing)|reject(?:ing)?|"
    r"refus(?:e|ing))\s+(?:the|this)\s+bet\s+(?:a|the)\s+"
    r"(?:good|best|correct|rational|reasonable|safe|wise|prudent)\s+choice\b",
    re.IGNORECASE,
)
_BET_STRONG_STRATEGY = re.compile(
    r"\b(?:"
    r"(?:i|we|one|a\s+reasonable\s+person)\s+(?:would|should)\s+"
    r"(?:be\s+(?:risk[- ]averse|cautious|conservative)\s+and\s+)?|"
    r"(?:the\s+)?(?:best|optimal)\s+(?:answer|strategy|decision|choice)"
    r"(?:\s+in\s+(?:this|the)\s+case)?\s+(?:is|would\s+be)\s+"
    r"(?:to\s+)?(?:hedge\s+and\s+)?|"
    r"it\s+would\s+be\s+better\s+to\s+)"
    r"(accept(?:s|ed|ing)?|declin(?:e|es|ed|ing)|"
    r"refus(?:e|es|ed|ing)|reject(?:s|ed|ing)?)\s+(?:th(?:e|is)\s+)?bet",
    re.IGNORECASE,
)
_BET_LABEL_DECISION = re.compile(
    r"(?im)^[ \t]*(?:final[ \t]+)?recommendation[ \t]*:[ \t]*"
    r"(accept|decline|reject|refuse)(?:[ \t]+the[ \t]+bet)?[ \t]*[.!]?[ \t]*$"
)
_BET_OPTION_PAIR = re.compile(
    r"\b(?:accept(?:ing|ed)?|declin(?:e|ing|ed))\b\s+"
    r"(?:or|versus|vs\.?|/)\s+"
    r"\b(?:accept(?:ing|ed)?|declin(?:e|ing|ed))\b(?:\s+(?:the|this)\s+bet)?",
    re.IGNORECASE,
)
_BET_META_GUARD = re.compile(
    r"\b(?:whether|determine\s+if|analy[sz]e\s+(?:if|whether)|"
    r"recommendation\s+(?:of|about)|would\s+need\s+to|need\s+to\s+consider)\b",
    re.IGNORECASE,
)


def _inside_bet_option_pair(text: str, position: int) -> bool:
    return any(match.start() <= position < match.end() for match in _BET_OPTION_PAIR.finditer(text))


_WEREWOLF_VOTE_ACTION = re.compile(
    r"\bvot(?:e|ing)\s+to\s+eliminat(?:e|ing)?\s+"
    r"[\"'“‘]?(?P<target>[A-Za-z][\w-]*)[\"'”’]?",
    re.IGNORECASE,
)
_WEREWOLF_COMMIT_NEAR = re.compile(
    r"\b(?:i|we)\s+(?:(?:would|will|should|must|intend|plan|choose|"
    r"decide|support|favor|opt)\b|(?:do|did)\s+not\b)|"
    r"\b(?:i|we)\s+(?:would\s+|will\s+)?recommend(?:ed|ing)?\b|"
    r"\b(?:my|our|the)\s+recommendation\s+(?:is|would\s+be)\b|"
    r"\b(?:correct|best|rational)\s+(?:action|choice|decision)\s+is\s+to\b|"
    r"\bit\s+(?:is|would\s+be)\s+(?:better|best|safer|prudent|rational|"
    r"reasonable)\s+to\s*$|"
    r"\b(?:do\s+not|don't)\s*$|"
    r"\b(?:therefore|thus|overall|in\s+conclusion)\b[^.\n]{0,35}$",
    re.IGNORECASE,
)
_WEREWOLF_AVOID_VOTE = re.compile(
    r"\b(?:i|we)\s+(?:would|will|should|must)?\s*"
    r"(?:therefore\s+)?avoid(?:ed|ing)?\s+vot(?:e|ing)\s+to\s+eliminat(?:e|ing)?\s+"
    r"[\"'“‘]?(?P<target>[A-Za-z][\w'-]*)[\"'”’]?",
    re.IGNORECASE,
)
_WEREWOLF_VOTE_SUBJECT = re.compile(
    r"\bvot(?:e|ing)\s+to\s+eliminat(?:e|ing)?\s+"
    r"[\"'“‘]?(?P<target>[A-Za-z][\w'-]*)[\"'”’]?\s+"
    r"(?:is|would\s+be|seems(?:\s+like)?|appears(?:\s+to\s+be)?)\s+"
    r"(?:the\s+|a\s+|an\s+|more\s+|most\s+)*"
    r"(?:best|correct|right|rational|reasonable|prudent|safe|safer|"
    r"effective|conservative)\b",
    re.IGNORECASE,
)
_WEREWOLF_TERMINAL_COMMIT_NEAR = re.compile(
    r"(?:\b(?:the\s+)?(?:best|optimal|correct|logical|rational|reasonable|"
    r"prudent|safest)\s+(?:course\s+of\s+action|decision|choice|option|"
    r"strategy|action|answer)(?:\s+for\s+[^,;.\n]{1,80})?\s+"
    r"(?:is|would\s+(?:therefore\s+)?be)\s+(?:to\s+)?(?:not\s+|do\s+not\s+)?|"
    r"\b(?:the\s+)?(?:decision|choice|answer|recommendation)\s+"
    r"(?:is|would\s+be|:)\s+(?:to\s+)?(?:not\s+|do\s+not\s+)?|"
    r"\b(?:you|one|the\s+focal\s+player|the\s+player|the\s+analysis|"
    r"this\s+analysis|the\s+framework|this\s+framework)\s+"
    r"(?:therefore\s+)?(?:should|must|will|would|ought\s+to|recommends?)\s+"
    r"(?:therefore\s+)?(?:not\s+|do\s+not\s+)?)$",
    re.IGNORECASE,
)
_WEREWOLF_TERMINAL_VOTE_PREDICATE = re.compile(
    # A parenthetical option label is descriptive here, not an answer in its
    # own right. Only A is accepted because the matched action is the
    # positive "vote to eliminate" option.
    r"^\s*(?:\((?:option\s+)?a\))?\s*"
    r"(?:(?:is|would\s+be|seems(?:\s+to\s+be|\s+like)?|"
    r"appears(?:\s+to\s+be)?)\s+(?:the\s+|a\s+|more\s+|most\s+)*"
    r"(?:best|better|optimal|correct|logical|rational|reasonable|prudent|"
    r"safe|safer|safest|right|effective|conservative)"
    r"(?:\s+(?:course\s+of\s+action|decision|choice|option|strategy|action|"
    r"answer))?|(?:has|would\s+have)\s+(?:a\s+|the\s+)?(?:higher|greater)\s+"
    r"expected\s+(?:utility|value))\b",
    re.IGNORECASE,
)
_WEREWOLF_POSTPOSED_REJECTION = re.compile(
    r"\b(?:is|was|would\s+be)\s+not\s+"
    r"(?:supported|correct|justified|recommended|warranted)\b",
    re.IGNORECASE,
)


def _inside_werewolf_option_pair(text: str, position: int) -> bool:
    """Reject action phrases that only enumerate the two requested options."""
    # Models often put the two catalogued choices on adjacent lines, so a
    # sentence-local scan misses exactly the common ``A, or\nB`` rendering.
    # Compare adjacent action mentions in the full response, while requiring
    # them to be close enough to form one option pair.
    votes = list(_WEREWOLF_VOTE_ACTION.finditer(text))
    for first, second in zip(votes, votes[1:]):
        if second.start() - first.end() > 160:
            continue
        if first.group("target").casefold() != second.group("target").casefold():
            continue
        between = text[first.end() : second.start()]
        if re.search(r"\b(?:or|versus|vs\.?)\b|/", between, re.IGNORECASE) is None:
            continue
        first_prefix = text[max(0, first.start() - 35) : first.start()]
        second_prefix = text[max(0, second.start() - 35) : second.start()]
        first_negative = _LOCAL_NEGATION.search(first_prefix) is not None
        second_negative = _LOCAL_NEGATION.search(second_prefix) is not None
        if first_negative != second_negative:
            if first.start() <= position <= second.end():
                return True
    return False


_WEREWOLF_NAMES = r"Alex|Casey|Charlie|Jamie|Jordan|Kai|Quinn|Taylor"
_BET_TARGET_RESTATEMENT = re.compile(
    rf"\b(?:bet(?:'s|ting)?|payoff|gain|loss)\b[^.\n]{{0,90}}?\b"
    rf"(?:if|whether|that|on|about)\s+[\"']?(?P<target>{_WEREWOLF_NAMES})"
    r"(?:'s)?\b[^.\n]{0,25}?\b"
    r"(?:werewolf|wolf|villager|town(?:ie)?)\b",
    re.IGNORECASE,
)
_BET_DIRECTION_RESTATEMENT = re.compile(
    rf"\bbet\s+(?:on|that|about)\s+[\"']?(?P<target>{_WEREWOLF_NAMES})"
    r"(?:'s)?\b[^.\n]{0,25}?\b(?P<role>werewolf|wolf|villager|town(?:ie)?)\b",
    re.IGNORECASE,
)


def explicit_bet_has_wrong_target_or_direction(
    text: str, target_player: str | None
) -> bool:
    """Reject a response that explicitly restates a different wager.

    Weak models sometimes answer a well-formed accept/decline question about
    another player, or reverse the wager into a bet that the named target is
    a villager.  The latest payoff/proposition restatement is authoritative,
    mirroring the target validation used for vote answers.
    """
    if target_player is None:
        return False
    target = str(target_player).casefold()
    # If the requested player is never named but the response explicitly
    # reasons about another named player's role, the generation has answered
    # a substituted wager rather than the prompt in front of it.
    if re.search(rf"\b{re.escape(str(target_player))}\b", text, re.I) is None:
        other_role = re.search(
            rf"\b(?P<target>{_WEREWOLF_NAMES})\b[^.\n]{{0,35}}?\b"
            r"(?:is|being|as)\s+(?:a\s+)?(?:werewolf|wolf|villager|town(?:ie)?)\b",
            text,
            re.IGNORECASE,
        )
        if other_role is not None and other_role.group("target").casefold() != target:
            return True
    restatements = list(_BET_TARGET_RESTATEMENT.finditer(text))
    if restatements and restatements[-1].group("target").casefold() != target:
        return True
    directions = list(_BET_DIRECTION_RESTATEMENT.finditer(text))
    if directions:
        direction = directions[-1]
        if direction.group("target").casefold() != target or direction.group(
            "role"
        ).casefold() in {"villager", "town", "townie"}:
            return True

    # Catch explicit prose that reinterprets "accept" as wagering on the
    # target *not* being a Werewolf (observed as "not suspicious ... safer to
    # bet on that").  This is not a low-probability judgment; it is the wrong
    # proposition altogether.
    reversed_reference = re.search(
        rf"(?:\b{re.escape(str(target_player))}\b[^.\n]{{0,100}}?"
        r"\b(?:not\s+(?:suspicious|a\s+werewolf|werewolf-like|wolf-like)|"
        r"(?:is|seems|appears)\s+(?:to\s+be\s+)?(?:a\s+)?(?:villager|town(?:ie)?))"
        rf"|\bnot\s+consider(?:ing|ed)?\s+{re.escape(str(target_player))}\b"
        r"[^.\n]{0,80}\b(?:suspicious|a\s+werewolf|werewolf-like|wolf-like))"
        r"[^.\n]{0,180}?\b(?:safer|better|preferable)\s+to\s+bet\s+on\s+that\b",
        text,
        re.IGNORECASE,
    )
    return reversed_reference is not None
_EXPLICIT_ANSWER_LINE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?"
    r"(?:choice|answer|final answer|best answer)[ \t]*"
    r"(?:is|[:=])[ \t]*(?P<payload>[^\n]*)$"
)


def _explicit_answer_payloads(text: str) -> list[tuple[int, str]]:
    """Return explicit answer payloads, joining a split label/option line."""
    normalized = text.replace("*", " ").replace("_", " ").replace("`", " ")
    payloads: list[tuple[int, str]] = []
    for answer in _EXPLICIT_ANSWER_LINE.finditer(normalized):
        payload = answer.group("payload").strip()
        end = answer.end()
        if not payload:
            following = re.search(r"(?m)^[ \t]*(\S[^\n]*)$", normalized[end:])
            if following is not None:
                payload = following.group(1).strip()
                end += following.end()
        payloads.append((end, payload))
    return payloads


def explicit_answer_has_wrong_werewolf_target(
    text: str, target_player: str | None
) -> bool:
    """Flag the last textual vote answer when it does not name the target.

    A later correct textual answer supersedes an earlier draft.  A later bare
    letter does not sanitize an unresolved/wrong target in the last textual
    answer, because the letter alone cannot establish which player the model
    actually acted on.
    """
    if target_player is None:
        return False
    for _, payload in reversed(_explicit_answer_payloads(text)):
        votes = list(_WEREWOLF_VOTE_ACTION.finditer(payload))
        if not votes:
            continue
        return any(
            vote.group("target").casefold() != str(target_player).casefold()
            for vote in votes
        )
    return False


def _described_option_value(
    trailing: str, family: str, *, ordinal: bool
) -> float | None:
    lowered = trailing.lower()
    if ordinal:
        matches = {
            float(value)
            for phrase, value in ordinal_phrases(family).items()
            if phrase in lowered
        }
        return next(iter(matches)) if len(matches) == 1 else None

    if "bet" in family:
        negative = re.search(
            r"\b(?:declin\w*|reject\w*|refus\w*)\s+(?:the|this)?\s*bet\b|"
            r"\b(?:do not|don't|not|never)\b[^.\n]{0,30}\baccept\w*\s+"
            r"(?:the|this)?\s*bet\b",
            lowered,
        )
        positive = re.search(r"\baccept\w*\s+(?:the|this)?\s*bet\b", lowered)
        if negative is not None:
            return 0.0
        if positive is not None:
            return 1.0
        return None
    vote = _WEREWOLF_VOTE_ACTION.search(trailing)
    if vote is None:
        return None
    prefix = trailing[: vote.start()]
    return 0.0 if _LOCAL_NEGATION.search(prefix) else 1.0


def explicit_answer_has_conflicting_option(
    text: str, family: str, *, ordinal: bool
) -> bool:
    """Reject a final answer whose option letter contradicts its option text."""
    payloads = _explicit_answer_payloads(text)
    if not payloads:
        return False
    payload = payloads[-1][1].strip().strip("\"'“”‘’ ")
    max_letter = "E" if ordinal else "B"
    labeled = re.match(
        rf"^\(?([A-{max_letter}])\)?(?:[.):]|\s)\s*(?P<trailing>.*)$",
        payload,
        flags=re.IGNORECASE,
    )
    if labeled is None or not labeled.group("trailing").strip():
        return False
    letter = labeled.group(1).upper()
    letter_value = (
        float(ord(letter) - ord("A") + 1) if ordinal else float(letter == "A")
    )
    described = _described_option_value(
        labeled.group("trailing"), family, ordinal=ordinal
    )
    return described is not None and described != letter_value


def _guarded(text: str, start: int, end: int) -> bool:
    """True when a commitment match must be rejected."""
    if _semantic_guarded(text, start, end):
        return True
    return not _conclusion_like(text, start, end)


def _semantic_guarded(text: str, start: int, end: int) -> bool:
    """Reject definitions, open questions, and conditional decision rules."""
    return (
        _definitional_context(text, start)
        or _noncommittal_context(text, start)
        or _conditional_context(text, start, end)
        or _HEDGED_NEAR.search(_local_clause_before(text, start)) is not None
    )


def commitment_candidates(
    text: str, family: str, target_player: str | None
) -> list[tuple[int, float]]:
    """Guarded prose commitments ``(position, value)`` for a binary answer."""
    candidates: list[tuple[int, float]] = []
    if "bet" in family:
        if explicit_bet_has_wrong_target_or_direction(text, target_player):
            return []
        for match in _BET_COMMIT.finditer(text):
            if _inside_bet_option_pair(text, match.start(1)):
                continue
            if _BET_META_GUARD.search(text[match.start() : match.start(1)]):
                continue
            if _semantic_guarded(text, match.start(), match.end()):
                continue
            value = 1.0 if match.group(1).lower().startswith("accept") else 0.0
            if _negated_before_action(match):
                value = 1.0 - value
            if re.search(r"\bless\s*$", _local_clause_before(text, match.start()), re.I):
                value = 1.0 - value
            candidates.append((match.start(), value))
        for match in _BET_SUBJECT.finditer(text):
            if _guarded(text, match.start(), match.end()) or _negated_predicate(match):
                continue
            value = 1.0 if match.group(1).lower().startswith("accept") else 0.0
            candidates.append((match.start(), value))
        for match in _BET_PASSIVE.finditer(text):
            if _semantic_guarded(text, match.start(), match.end()):
                continue
            value = 1.0 if match.group(2).lower().startswith("accept") else 0.0
            if match.group(1):
                value = 1.0 - value
            candidates.append((match.start(), value))
        for match in _BET_MAKE_CHOICE.finditer(text):
            if _semantic_guarded(text, match.start(), match.end()):
                continue
            value = 1.0 if match.group(1).lower().startswith("accept") else 0.0
            candidates.append((match.start(), value))
        for match in _BET_STRONG_STRATEGY.finditer(text):
            # These are explicit decision constructions ("the optimal
            # strategy is to decline"), not mere mentions of an action.
            # Retain the semantic definition/hedge guards, but do not let an
            # explanatory "if this is true" clause erase the stated choice.
            sentence_start, _ = _sentence_bounds(text, match.start(), match.end())
            resolved_reference = re.search(
                r"\bif\s+this\s+is\s+true\b",
                text[sentence_start : match.start()],
                re.IGNORECASE,
            )
            if (
                _definitional_context(text, match.start())
                or _noncommittal_context(text, match.start())
                or _HEDGED_NEAR.search(_local_clause_before(text, match.start()))
                is not None
                or (
                    _conditional_context(text, match.start(), match.end())
                    and resolved_reference is None
                )
                or not _conclusion_like(text, match.start(), match.end())
            ):
                continue
            value = 1.0 if match.group(1).lower().startswith("accept") else 0.0
            candidates.append((match.start(), value))
        for match in _BET_LABEL_DECISION.finditer(text):
            value = 1.0 if match.group(1).lower().startswith("accept") else 0.0
            candidates.append((match.start(), value))
    else:
        vote_candidates: list[tuple[int, str, float]] = []
        for match in _WEREWOLF_VOTE_ACTION.finditer(text):
            if _inside_werewolf_option_pair(text, match.start()):
                continue
            if _semantic_guarded(text, match.start(), match.end()):
                continue
            sentence_start, sentence_end = _sentence_bounds(
                text, match.start(), match.end()
            )
            sentence = text[sentence_start:sentence_end]
            if re.search(
                r"\b(?:might|may|could|possibly|perhaps|hypothetic\w*)\b|"
                r"\bby\s+voting\b",
                sentence,
                re.IGNORECASE,
            ):
                continue
            local_clause = _local_clause_before(text, match.start())
            subject_claim = _WEREWOLF_VOTE_SUBJECT.match(text, match.start())
            terminal_claim = (
                _WEREWOLF_TERMINAL_COMMIT_NEAR.search(local_clause) is not None
                or _WEREWOLF_TERMINAL_VOTE_PREDICATE.match(
                    text[match.end() : sentence_end]
                )
                is not None
            )
            if _WEREWOLF_POSTPOSED_REJECTION.search(text[match.end() : sentence_end]):
                terminal_claim = False
            if (
                not _WEREWOLF_COMMIT_NEAR.search(local_clause)
                and subject_claim is None
                and not terminal_claim
            ):
                continue
            negated = (
                _LOCAL_NEGATION.search(local_clause) is not None
                or re.search(r"\bavoid(?:ed|ing)?\s*$", local_clause, re.I) is not None
            )
            vote_candidates.append(
                (match.start(), match.group("target").casefold(), 0.0 if negated else 1.0)
            )
        for match in _WEREWOLF_AVOID_VOTE.finditer(text):
            if _semantic_guarded(text, match.start(), match.end()):
                continue
            vote_candidates.append((match.start(), match.group("target").casefold(), 0.0))

        # A final commitment to act on a different player supersedes an
        # earlier answer about the requested target.
        if vote_candidates:
            final_vote = max(vote_candidates, key=lambda item: item[0])
            if target_player is not None and final_vote[1] != str(target_player).casefold():
                requested = [
                    candidate
                    for candidate in vote_candidates
                    if candidate[1] == str(target_player).casefold()
                ]
                # "Do not vote for X; instead vote for Y" is a consistent
                # negative answer about X.  A positive answer about X later
                # redirected to Y is a retraction and remains ambiguous.
                if not requested or max(requested, key=lambda item: item[0])[2] != 0.0:
                    return []
            candidates.extend(
                (position, value)
                for position, candidate_target, value in vote_candidates
                if target_player is None
                or candidate_target == str(target_player).casefold()
            )
    return candidates
