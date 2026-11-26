"""The fixed first steps of every call, shown at the top of the Next best action list.

1. Confirm the customer's mobile number. Shown from the customer's first line. Every account answer depends on it,
   so it always comes first. Done when the number is confirmed (said on the call, read back and agreed, or typed).
2. Confirm where the customer is right now. Shown when the customer reports a network problem. Done when the
   current location is set.

The AI's own suggestions (nba_rules.json) follow below them. A step is in the call's language, like the rest.
"""
from __future__ import annotations

from copilot.models.participants import CUSTOMER
from copilot.services import language
from copilot.services.hub import PanelHub
from copilot.services.sessions import CallSession

PROCESS = "process"  # the `source` of a step: not a knowledge article, and left out of the adoption numbers
CONFIRM_NUMBER = "step-confirm-number"
CONFIRM_LOCATION = "step-confirm-location"
STEP_IDS = (CONFIRM_NUMBER, CONFIRM_LOCATION)

TEXTS = {
    CONFIRM_NUMBER: {
        language.ENGLISH: (
            "Confirm the mobile number",
            "Ask the customer to confirm their mobile number first. The customer card and every account answer depend on it.",
        ),
        language.BANGLA: (
            "মোবাইল নম্বর নিশ্চিত করুন",
            "প্রথমে গ্রাহকের কাছ থেকে তাঁর মোবাইল নম্বর নিশ্চিত করে নিন। গ্রাহকের কার্ড এবং অ্যাকাউন্টের সব উত্তর এর উপর নির্ভর করে।",
        ),
        language.BANGLISH: (
            "Mobile number confirm korun",
            "Age customer er kach theke tar mobile number confirm kore nin. Customer card ar account er shob uttor er upor nirbhor kore.",
        ),
    },
    CONFIRM_LOCATION: {
        language.ENGLISH: (
            "Confirm where the customer is now",
            "This is a network problem. Ask where the customer is right now and check the network status there.",
        ),
        language.BANGLA: (
            "গ্রাহক এখন কোথায় আছেন তা নিশ্চিত করুন",
            "এটি নেটওয়ার্কের সমস্যা। গ্রাহক এই মুহূর্তে কোথায় আছেন জিজ্ঞেস করুন এবং সেখানকার নেটওয়ার্কের অবস্থা দেখুন।",
        ),
        language.BANGLISH: (
            "Customer ekhon kothay achen confirm korun",
            "Eta network er shomossha. Customer ekhon kothay achen jiggesh korun ar shekhane network er obostha check korun.",
        ),
    },
}


def _item(step: str, lang: str, used: bool) -> dict:
    title, text = TEXTS[step].get(lang) or TEXTS[step][language.ENGLISH]
    return {"id": step, "title": title, "text": text, "source": PROCESS, "used": used}


class CallGuide:
    def __init__(self, hub: PanelHub) -> None:
        self._hub = hub

    @staticmethod
    def apply(session: CallSession, *, reset_number: bool = False) -> bool:
        """Add or tick the steps, and keep them first in the list. Returns True when anything changed."""
        before = [(s["id"], s["used"]) for s in session.suggestions.values()]
        items = dict(session.suggestions)
        lang = session.language()
        wanted = {
            CONFIRM_NUMBER: any(t.speaker == CUSTOMER for t in session.turns) or session.customer is not None,
            CONFIRM_LOCATION: session.location_prompt or session.current_location is not None,
        }
        done = {CONFIRM_NUMBER: session.identity_confirmed, CONFIRM_LOCATION: session.current_location is not None}
        steps = {}
        for step in STEP_IDS:
            shown = items.pop(step, None)
            if shown is None and not wanted[step]:
                continue
            if shown is None:
                shown = _item(step, lang, done[step])  # its language is fixed when it first appears
            elif step == CONFIRM_NUMBER and reset_number:
                shown["used"] = done[step]  # "Not this customer": the number has to be confirmed again
            else:
                shown["used"] = shown["used"] or done[step]  # a step the agent marked done stays done
            steps[step] = shown
        session.suggestions = {**steps, **items}
        return [(s["id"], s["used"]) for s in session.suggestions.values()] != before

    async def update(self, session: CallSession, *, reset_number: bool = False) -> None:
        """Apply the steps and tell the panel when they changed."""
        if session.ended_at is None and self.apply(session, reset_number=reset_number):
            await self._hub.broadcast("suggestions", list(session.suggestions.values()))
