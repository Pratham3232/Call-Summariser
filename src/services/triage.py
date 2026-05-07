"""
Cheap pre-LLM triage.

Goal: decide BEFORE spending any LLM tokens whether this call is "hot" (must
be analysed now) or "cold" (can be deferred or, in clear-cut cases, classified
heuristically without an LLM call at all).

Why not "just always run the LLM":
    The single biggest lever for staying inside rate limits at 100K calls is
    not running the LLM on calls where the outcome is obvious. Roughly half
    the calls in the sample fixtures fall into this category — "wrong number",
    "not interested", "already purchased". A keyword/regex pass over the last
    few customer turns catches these with very high confidence at zero token
    cost.

Why not "use a cheaper LLM for triage":
    A cheaper LLM is still rate limited and still costs tokens. Heuristic
    triage is free, deterministic, and easy to audit. The trade-off is that
    ambiguous calls (Hinglish "dekhta hoon", soft commitments) need the full
    model — and we route those to the hot lane only if the surface signals
    suggest a meaningful outcome.

Output contract:
    `triage(transcript, additional_data)` returns a `TriageDecision` with:
      - lane: 'hot' | 'cold' | 'skip'
      - label: heuristic classification (may be promoted to call_stage if
               confidence >= HEURISTIC_ACCEPT_THRESHOLD)
      - confidence: in [0, 1]
      - needs_llm: True if the LLM must run (label is uncertain)

The thresholds and keyword lists live in code intentionally — they need to
be reviewed by a human. Per-customer overrides live in customers.config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


HEURISTIC_ACCEPT_THRESHOLD = 0.85


# Each pattern fires against the *customer's* utterances. The group `weight`
# is the per-match confidence; multiple matches combine via noisy-OR
# (1 - (1 - w)^k), so a second match consolidates a borderline decision.
_PATTERNS: List[Dict] = [
    # Cold lane — high-confidence "no-op" outcomes. Single distinctive match
    # is usually enough.
    {
        "label": "not_interested",
        "lane": "cold",
        "weight": 0.85,
        "patterns": [
            r"\bnot interested\b",
            r"\bdon'?t call\b",
            r"\bno thanks?\b",
            r"\bremove (my )?number\b",
            r"\bmana kar rahe?\b",
            r"\bnahi chahiye\b",
        ],
    },
    {
        "label": "wrong_number",
        "lane": "cold",
        "weight": 0.95,
        "patterns": [
            r"\bwrong number\b",
            r"\bgalat number\b",
            r"\bkis ka call\b",
        ],
    },
    {
        "label": "already_done",
        "lane": "cold",
        "weight": 0.88,
        "patterns": [
            r"\balready (booked|purchased|bought|paid|completed)\b",
            r"\bkar (chuka|chuki) (hoon|hai)\b",
            r"\bho gaya hai\b",
        ],
    },
    # Hot lane — high-value outcomes. Heuristic confidence usually matters
    # less here because we always run the LLM on hot calls (we want the
    # entities); the heuristic's job is just to choose the lane.
    {
        "label": "rebook_confirmed",
        "lane": "hot",
        "weight": 0.7,
        "patterns": [
            r"\bconfirmed\b",
            r"\bbook(ed)? (the|my|a)? ?(slot|appointment|demo)\b",
            r"\bschedule[d]? (for|at|the)\b",
            r"\b(tomorrow|kal|aaj) (at|ko) \d",
        ],
    },
    {
        "label": "demo_booked",
        "lane": "hot",
        "weight": 0.7,
        "patterns": [
            r"\bdemo (is )?booked\b",
            r"\bcalendar invite\b",
            r"\bsend(ing)? (you )?(an? )?invite\b",
        ],
    },
    {
        "label": "escalation_needed",
        "lane": "hot",
        "weight": 0.9,
        "patterns": [
            r"\bspeak to (a |the )?manager\b",
            r"\bfile a complaint\b",
            r"\bunacceptable\b",
            r"\bescalat(e|ion)\b",
        ],
    },
]


@dataclass
class TriageDecision:
    lane: str                            # 'hot' | 'cold' | 'skip'
    label: str                           # heuristic guess; 'unknown' if none fired
    confidence: float                    # in [0, 1]
    needs_llm: bool                      # True iff confidence < HEURISTIC_ACCEPT_THRESHOLD
    matched_patterns: List[str] = field(default_factory=list)


def triage(
    transcript: List[Dict[str, str]],
    additional_data: Optional[Dict] = None,
) -> TriageDecision:
    """
    Cheap, deterministic, fully auditable.

    Empty / very short transcripts are routed to 'skip' so they bypass the LLM
    entirely. The endpoint already has a turns-count check, but it lives here
    too so a Celery retry that bypasses the endpoint cannot accidentally
    re-spend tokens on a wrong-number call.
    """
    if not transcript or len(transcript) < 4:
        return TriageDecision(
            lane="skip",
            label="short_call",
            confidence=1.0,
            needs_llm=False,
            matched_patterns=["__short_transcript__"],
        )

    customer_text = " ".join(
        (turn.get("content") or "").lower()
        for turn in transcript
        if turn.get("role") == "customer"
    )
    # Hot patterns are allowed to match agent confirmations too, because the
    # agent's "demo is booked"/"I'll send a calendar invite" is strong
    # evidence the call ended in a booking. Cold patterns are restricted
    # to the customer's words so the agent's apologies don't false-positive.
    full_text = " ".join(
        (turn.get("content") or "").lower() for turn in transcript
    )

    best_label = "unknown"
    best_lane = "cold"
    best_score = 0.0
    best_patterns: List[str] = []

    for group in _PATTERNS:
        haystack = full_text if group["lane"] == "hot" else customer_text
        hits: List[str] = []
        for pat in group["patterns"]:
            if re.search(pat, haystack):
                hits.append(pat)
        if not hits:
            continue
        # Noisy-OR combination across distinct patterns matched within
        # the same label group: P(at least one true) = 1 - (1-w)^k.
        w = group["weight"]
        raw = 1.0 - (1.0 - w) ** len(hits)
        if raw > best_score:
            best_score = raw
            best_label = group["label"]
            best_lane = group["lane"]
            best_patterns = hits

    if best_score >= HEURISTIC_ACCEPT_THRESHOLD:
        # Strong signal — trust the heuristic. Cold-lane "no-op" calls don't
        # need the LLM at all; their result is deterministic.
        return TriageDecision(
            lane=best_lane,
            label=best_label,
            confidence=round(best_score, 3),
            needs_llm=best_lane == "hot",
            matched_patterns=best_patterns,
        )

    # No strong heuristic signal. Default to LLM in the lane suggested by the
    # weak signal (cold if nothing fired). Hot-lane bias for ambiguous calls
    # would be too expensive — the cold lane will still process them, just
    # under headroom rather than at peak.
    return TriageDecision(
        lane=best_lane if best_score > 0 else "cold",
        label=best_label,
        confidence=round(best_score, 3),
        needs_llm=True,
        matched_patterns=best_patterns,
    )
