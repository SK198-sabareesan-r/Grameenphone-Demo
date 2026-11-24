"""The three people (and bot) a call can have, kept as separate identities.

* customer     - the caller
* ai_agent     - the voice bot (its replies are context for the Copilot, never the human's words)
* human_agent  - the Grameenphone agent using the Copilot

The older value "agent" (typed lines, rule files) means the human agent and is normalised on input.
"""

CUSTOMER = "customer"
AI_AGENT = "ai_agent"
HUMAN_AGENT = "human_agent"
PARTICIPANTS = (CUSTOMER, AI_AGENT, HUMAN_AGENT)
LEGACY_ALIASES = {"agent": HUMAN_AGENT}

# How each role is written in transcripts sent to the model.
TRANSCRIPT_LABELS = {CUSTOMER: "CUSTOMER", AI_AGENT: "AI AGENT", HUMAN_AGENT: "HUMAN AGENT"}

# Compliance reminders are about what people say: AI speech never triggers or satisfies a rule.
COMPLIANCE_SPEAKERS = frozenset({CUSTOMER, HUMAN_AGENT})
# Only the human agent's words can mark a next-best-action as used.
SUGGESTION_SPEAKERS = frozenset({HUMAN_AGENT})
# Turns that trigger mood / next-best-action analysis. AI turns are context only.
ANALYSIS_TRIGGERS = frozenset({CUSTOMER, HUMAN_AGENT})


def normalize_speaker(value: str) -> str:
    """Map an input speaker to a participant role. Raises ValueError for anything unknown."""
    key = (value or "").strip().lower()
    role = LEGACY_ALIASES.get(key, key)
    if role not in PARTICIPANTS:
        raise ValueError(f"unknown speaker {value!r}; expected one of {', '.join(PARTICIPANTS)}")
    return role
