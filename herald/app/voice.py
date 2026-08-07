"""
HARALD voice specification.

Every check here is deterministic Python, not a prompt, which is why it
survived the move from Claude to OCI Generative AI unchanged. A prompt is a
request; this is the verification that the request was honoured. The banned
word list was tuned against one model's tells and will want revisiting against
Llama's, but the mechanism does not care which model produced the text.

One file. Every voice rule lives here exactly once, and both consumers
read from it:

  - render_rules()  builds the text that goes into the model prompts
  - score()         checks the returned text against the same constants

That coupling is the point. Before this, the banned-word list existed
twice, once as prose inside the humanize prompt and once as a Python list
used by the checker. Two copies of a rule are one rule and one bug
waiting.

--------------------------------------------------------------------
WHAT THE CORPUS ACTUALLY SAYS
--------------------------------------------------------------------
Measured over the 8 verified iteria proposals in the library, 508 chunks,
73,480 words. Worth reading before blaming the model:

  comprehensive        44 occurrences      6.0 per 10k words
  ensure/ensures/-ing  76                 10.3
  leverage             14                  1.9
  seamless/-ly         19                  2.6
  robust               12                  1.6
  streamline/-d        20                  2.7
  facilitate            9                  1.2
  rule-of-three lists  195                26.5
  em and en dashes    225                 30.6

The model was not inventing that register. It was copying it, faithfully,
out of iteria's own submitted proposals, which is exactly what a
retrieval system is built to do. So the vocabulary rules below are
enforced AGAINST the corpus, not learned from it.

Rhythm is the opposite case. The corpus is genuinely human there and the
targets below are taken straight from it:

  mean sentence 28.4 words, median 22, standard deviation 24.9
  39% of sentences fall in the 15 to 25 word band (an AI draft runs 75%+)
  7% under 10 words, 20% over 35
  signposting phrases: 5 occurrences in 73,480 words, effectively zero

So: keep iteria's cadence, drop iteria's buzzwords. That is the whole
specification.
"""

import re
import statistics

# ----------------------------------------------------------------------
# 1. VOCABULARY. word -> what to write instead.
#    A replacement is required. "Do not say X" without "say Y" produces
#    a sentence rewritten around the gap, which reads worse than the
#    buzzword did.
# ----------------------------------------------------------------------
BANNED = {
    "leverage":      "use",
    "robust":        "name the property: tested, redundant, supported",
    "seamless":      "cut it, or say what does not break",
    "seamlessly":    "cut it",
    "comprehensive": "cut it, or say what is covered",
    "holistic":      "cut it",
    "streamline":    "shorten, cut steps out of",
    "streamlined":   "shorter, fewer steps",
    "empower":       "let, allow, give access to",
    "cutting-edge":  "current, supported",
    "best-in-class": "cut it",
    "synergy":       "cut it",
    "facilitate":    "run, host, lead",
    "utilize":       "use",
    "utilizing":     "using",
    "ensure":        "make sure, so that",
    "ensures":       "makes sure, means",
    "ensuring":      "so that",
    "delve":         "look at, work through",
    "tapestry":      "cut it",
    "landscape":     "cut it, or name the systems",
    "realm":         "cut it",
    "foster":        "build, support",
    "unlock":        "cut it",
    "elevate":       "improve, raise",
    "pivotal":       "important, or cut it",
    "myriad":        "many, or give the number",
    "testament":     "cut it",
    "underscore":    "show, confirm",
    "navigate":      "work through (unless literally moving through a UI)",
    "in today's":    "cut the whole opener",
}

SIGNPOSTS = [
    "it is worth noting", "it should be noted", "importantly,",
    "in conclusion", "this ensures that", "in today's fast-paced",
    "it is important to note", "as previously mentioned",
    "in this section we will", "let us explore",
]

HEDGE_STACKS = [
    r"\bcan help to\b", r"\bmay potentially\b", r"\bcould possibly\b",
    r"\bmight be able to\b", r"\bhelps to potentially\b",
    r"\bis designed to help\b",
]

FORBIDDEN_CHARS = {"\u2014": "em dash", "\u2013": "en dash"}

# ----------------------------------------------------------------------
# 2. RHYTHM. Calibrated against the STYLE ANCHOR, not the pooled corpus.
#
# The first version of this file took its targets from all eight
# proposals averaged together. That was wrong. Pooling put Brown County,
# which lost, and the buzzword-heavy Ozaukee and RTA responses into the
# same average as the canonical document, and the resulting thresholds
# failed the canonical document.
#
#                       anchor    rest of library    Brown County (lost)
#   mean sentence        19.1          28.4              29.7
#   median               17            22                24
#   under 10 words       19%            7%                6%
#   over 35 words         8%           20%               24%
#   standard deviation   11.2          24.9              22.8
#   banned per 10k        5.2          30.1              23.8
#
# The anchor is tighter and shorter. Its lower standard deviation is not
# mechanical writing; it is the absence of the sixty-word sentences the
# rest of the library is full of. Standard deviation was measuring
# "contains some enormous sentences" and scoring it as variety, so it is
# now a weak floor and the real discriminator is the share of genuinely
# short sentences. An AI draft has almost none.
#
# Re-derive these with calibrate_from_anchor() whenever the anchor
# changes. Do not hand-edit them from memory.
# ----------------------------------------------------------------------
RHYTHM = {
    "mean_words":          19.1,   # anchor
    "median_words":        17,     # anchor
    "stdev_min":            9.0,   # anchor 11.2, weak floor only
    "band_15_25_max":       0.55,  # anchor 35%; an AI draft runs 75%+
    "short_under_10_min":   0.12,  # anchor 19%; this is the real tell
    "long_over_35_max":     0.20,  # anchor 8%; the losing corpus runs 20-24%
}

CALIBRATION = {
    "source": "City of St. Petersburg RFP-26-078, retrieval_tier=CANONICAL, style_anchor=Y",
    "words": 13400,
    "sentences": 700,
    "note": ("Vocabulary rules are enforced AGAINST the corpus, including the "
             "anchor, which still runs 5.2 banned words per 10k. Rhythm targets "
             "are taken FROM the anchor. Those are deliberately different "
             "directions."),
}

# rule of three: three parallel items joined by "and", used for rhythm
RULE_OF_THREE = re.compile(r"\b[\w'-]+,\s+[\w'-]+,\s+and\s+[\w'-]+\b")

# Bracketed placeholders point in the opposite direction to chunking's
# annotation stripping, and both are correct. In library chunks an unresolved
# "[CLIENT-SPECIFIC - VERIFY]" is removed, because chunks are voice reference
# and the model would learn to emit it. In a generated draft the same brackets
# are a note to the consultant and dropping one is a blocking finding: the
# draft then reads as finished when a fact is still missing.
PLACEHOLDER = re.compile(r"\[[^\]\n]{3,200}\]")
NUMBERISH = re.compile(
    r"\$[\d,]+(?:\.\d+)?|\b\d{1,3}(?:,\d{3})+\b|\b\d+(?:\.\d+)?\s?%|"
    r"\b(?:19|20)\d{2}\b|"
    r"\b\d+\s?(?:years?|months?|weeks?|days?|FTEs?|staff|employees|users|sites?)\b",
    re.I)

_BANNED_RE = {w: re.compile(r"\b" + re.escape(w) + r"\b", re.I) for w in BANNED}
_SIGNPOST_RE = [re.compile(re.escape(s), re.I) for s in SIGNPOSTS]
_HEDGE_RE = [re.compile(h, re.I) for h in HEDGE_STACKS]


# ----------------------------------------------------------------------
# 3. PROMPT TEXT, generated from the constants above
# ----------------------------------------------------------------------
def render_rules(include_replacements=True):
    """The style block injected into the humanize prompt."""
    if include_replacements:
        vocab = "\n".join(f"  {w} -> {r}" for w, r in BANNED.items())
    else:
        vocab = "  " + ", ".join(BANNED)
    return f"""VOCABULARY. Replace every one of these. The replacement is given; use it or cut the sentence down.
{vocab}

RHYTHM. iteria's own submitted proposals average {RHYTHM['mean_words']} words per sentence with a standard deviation of 24.9, and only 39 percent of sentences land in the 15 to 25 word band. Match that. Some sentences five words. Some over forty. If your paragraph reads at an even pace, it is wrong.

STRUCTURE.
  No three-item parallel lists used for rhythm ("faster, cheaper, and more reliable"). Two items or four, or rewrite.
  No signposting: {', '.join(SIGNPOSTS[:6])}.
  No hedge stacking. "can help to potentially" is one claim pretending to be careful. State it.
  Name the actor. iteria does things. Passive voice only where the actor genuinely does not matter.

CHARACTERS. No em dashes and no en dashes anywhere. Commas, periods, semicolons, parentheses, or split the sentence. Number ranges use "to", as in "12 to 24 months".

FACTS. Do not add any. Do not remove any [BRACKETED] placeholder. Every date, count, duration, dollar figure and named reference must already appear in the material you were given."""


# ----------------------------------------------------------------------
# 4. SCORING. Deterministic. This is what actually enforces the above.
# ----------------------------------------------------------------------
def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) > 2]


def score(final, draft=None, source_text="", client_facts_json=""):
    """
    Check generated text against the specification.

    Returns findings plus two verdicts:
      blocking  a wrong fact would reach an evaluator. Never auto-waive.
      clean     nothing at all to fix.
    """
    findings = {}

    findings["banned_words"] = sorted({
        w for w, rx in _BANNED_RE.items() if rx.search(final)})
    findings["banned_word_count"] = sum(
        len(rx.findall(final)) for rx in _BANNED_RE.values())

    findings["forbidden_chars"] = {
        name: final.count(ch) for ch, name in FORBIDDEN_CHARS.items()
        if final.count(ch)}

    findings["signposts"] = sorted({
        s for s, rx in zip(SIGNPOSTS, _SIGNPOST_RE) if rx.search(final)})
    findings["hedge_stacks"] = sorted({
        rx.pattern for rx in _HEDGE_RE if rx.search(final)})
    # Advisory only. The anchor uses three-item lists at 32.1 per 10k words,
    # higher than the rest of the library, so this is part of how iteria
    # writes rather than a machine tell. Reported, never a failure.
    findings["rule_of_three"] = RULE_OF_THREE.findall(final)[:8]

    lengths = [len(s.split()) for s in _sentences(final)]
    if lengths:
        sd = statistics.pstdev(lengths) if len(lengths) > 1 else 0.0
        band = sum(1 for x in lengths if 15 <= x <= 25) / len(lengths)
        short = sum(1 for x in lengths if x < 10) / len(lengths)
        longs = sum(1 for x in lengths if x > 35) / len(lengths)
        findings["rhythm"] = {
            "sentences": len(lengths),
            "mean": round(statistics.mean(lengths), 1),
            "stdev": round(sd, 1),
            "share_15_25": round(band, 2),
            "share_under_10": round(short, 2),
            "share_over_35": round(longs, 2),
        }
        findings["rhythm_flat"] = (
            band > RHYTHM["band_15_25_max"]
            or short < RHYTHM["short_under_10_min"]
            or longs > RHYTHM["long_over_35_max"]
            or sd < RHYTHM["stdev_min"])
    else:
        findings["rhythm"] = {}
        findings["rhythm_flat"] = False

    # fact integrity, only checkable when the draft is supplied
    if draft is not None:
        findings["placeholders_dropped"] = sorted(
            set(PLACEHOLDER.findall(draft)) - set(PLACEHOLDER.findall(final)))
        haystack = " ".join([source_text, client_facts_json, draft]).lower()
        findings["unsourced_numbers"] = sorted({
            n.strip() for n in NUMBERISH.findall(final)
            if n.strip() and n.strip().lower() not in haystack})
    else:
        findings["placeholders_dropped"] = []
        findings["unsourced_numbers"] = []

    findings["blocking"] = bool(
        findings["placeholders_dropped"] or findings["unsourced_numbers"])
    findings["clean"] = not (
        findings["banned_words"] or findings["forbidden_chars"]
        or findings["signposts"] or findings["hedge_stacks"]
        or findings["rhythm_flat"]
        or findings["blocking"])
    return findings


def repair_brief(findings):
    """
    Turn findings into a short, specific instruction for a repair pass.
    Generic re-prompting ("try again, less AI") does not work. Naming the
    exact word in the exact text does.

    Returns None when there is nothing to repair.
    """
    if findings.get("clean"):
        return None
    parts = []
    if findings.get("placeholders_dropped"):
        parts.append("Put these bracketed placeholders back exactly as written: "
                     + "; ".join(findings["placeholders_dropped"]))
    if findings.get("unsourced_numbers"):
        parts.append("These figures appear in your text but in none of the source "
                     "material. Remove each one or replace it with a bracketed note "
                     "for the consultant: " + ", ".join(findings["unsourced_numbers"]))
    if findings.get("banned_words"):
        subs = "; ".join(f"{w} -> {BANNED[w]}" for w in findings["banned_words"])
        parts.append("Replace every occurrence: " + subs)
    if findings.get("forbidden_chars"):
        parts.append("Remove every " + " and ".join(
            f"{name} ({n} present)" for name, n in findings["forbidden_chars"].items())
            + ". Use a comma, a semicolon, parentheses, or split the sentence. "
              "Number ranges use the word to.")
    if findings.get("signposts"):
        parts.append("Delete these openers and start with the point: "
                     + ", ".join(findings["signposts"]))
    if findings.get("hedge_stacks"):
        parts.append("Unstack these hedges and state the claim plainly.")
    if findings.get("rhythm_flat"):
        r = findings.get("rhythm", {})
        parts.append(
            f"Sentence length is too even (standard deviation {r.get('stdev')}, "
            f"{int(100 * r.get('share_15_25', 0))} percent of sentences in the 15 to 25 "
            f"word band, {int(100 * r.get('share_under_10', 0))} percent under ten words). "
            f"The iteria proposal this voice is modelled on averages 19 words a sentence "
            f"with 19 percent of them under ten words and only 8 percent over thirty-five. "
            f"Cut hard. Short declaratives, then one longer sentence, then short again.")
    return ("Fix only what is listed. Change nothing else, add no facts, and return "
            "only the corrected section.\n\n" + "\n".join(f"- {p}" for p in parts))


# ----------------------------------------------------------------------
# 5. EXEMPLAR SELECTION
# ----------------------------------------------------------------------
def exemplar_score(text):
    """
    How well a corpus chunk models the voice we actually want. Higher is
    better. Used to pick voice references, because the highest-ranked
    retrieval hit is chosen for topical relevance and may be one of the
    buzzword-heavy passages. 31 percent of the library scores clean here.
    """
    words = text.split()
    if len(words) < 60:
        return -1.0
    sents = _sentences(text)
    if len(sents) < 3:
        return -1.0
    # bullet dumps have few sentence terminators per word
    if len(words) / len(sents) > 60:
        return -1.0

    lengths = [len(s.split()) for s in sents]
    sd = statistics.pstdev(lengths)
    banned = sum(len(rx.findall(text)) for rx in _BANNED_RE.values())
    threes = len(RULE_OF_THREE.findall(text))
    dashes = sum(text.count(ch) for ch in FORBIDDEN_CHARS)
    per1k = 1000.0 / len(words)

    return round(
        min(sd, 30.0) / 30.0 * 2.0
        - banned * per1k * 3.0
        - threes * per1k * 2.0
        - dashes * per1k * 2.0, 3)


def pick_voice_exemplars(excerpts, n=2, prefer_verified=True):
    """
    Choose which retrieved excerpts to hold up as the voice to imitate.
    Falls back gracefully: verified and clean, then any clean, then
    whatever was retrieved.
    """
    pool = list(excerpts)
    if prefer_verified:
        v = [e for e in pool
             if (e.get("trust_level") or "VERIFIED").upper() == "VERIFIED"]
        pool = v or pool
    scored = []
    for e in pool:
        t = e.get("chunk_text") or e.get("text") or ""
        scored.append((exemplar_score(t), t))
    scored.sort(key=lambda x: -x[0])
    good = [t for s, t in scored if s > 0][:n]
    if good:
        return good
    return [t for _, t in scored[:n] if t]


# ----------------------------------------------------------------------
# 6. CALIBRATION. Re-derive RHYTHM from the current style anchor.
#
# The numbers above are a snapshot. When the anchor changes, or when
# St. Petersburg is rechunked at proper section boundaries, they go stale.
# Run this against the anchor text and paste the result back into RHYTHM
# rather than adjusting the thresholds by feel.
# ----------------------------------------------------------------------
def calibrate_from_anchor(anchor_texts):
    """
    anchor_texts: list of chunk_text strings from
                  SELECT chunk_text FROM TABLE(iteria_ai.harald_style_anchor(NULL, 999))
                  or any document you want to define the voice.

    Returns a RHYTHM dict plus the measurements behind it.
    """
    text = " ".join(t for t in anchor_texts if t)
    sents = _sentences(text)
    if len(sents) < 30:
        raise ValueError(f"need at least 30 sentences to calibrate, got {len(sents)}")
    lengths = [len(s.split()) for s in sents]
    words = len(text.split())

    mean = statistics.mean(lengths)
    median = statistics.median(lengths)
    sd = statistics.pstdev(lengths)
    band = sum(1 for x in lengths if 15 <= x <= 25) / len(lengths)
    short = sum(1 for x in lengths if x < 10) / len(lengths)
    longs = sum(1 for x in lengths if x > 35) / len(lengths)
    banned = sum(len(rx.findall(text)) for rx in _BANNED_RE.values())

    return {
        "measured": {
            "words": words,
            "sentences": len(sents),
            "mean_words": round(mean, 1),
            "median_words": median,
            "stdev": round(sd, 1),
            "share_15_25": round(band, 2),
            "share_under_10": round(short, 2),
            "share_over_35": round(longs, 2),
            "banned_per_10k": round(banned * 10000 / words, 1),
        },
        # thresholds sit slightly outside the measurement so the anchor
        # itself always passes; a target the reference text fails is a
        # broken target
        "RHYTHM": {
            "mean_words": round(mean, 1),
            "median_words": median,
            "stdev_min": round(max(sd * 0.8, 6.0), 1),
            "band_15_25_max": round(min(band + 0.20, 0.75), 2),
            "short_under_10_min": round(max(short - 0.07, 0.03), 2),
            "long_over_35_max": round(min(longs + 0.12, 0.35), 2),
        },
    }
