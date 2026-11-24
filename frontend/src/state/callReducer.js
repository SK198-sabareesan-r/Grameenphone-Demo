export const initialCallState = {
  callSid: "",
  active: false,
  ended: false,
  turns: [],
  // One in-progress line per participant: customer, AI agent (voice bot), human agent.
  partials: { customer: "", ai_agent: "", human_agent: "" },
  card: null,
  mood: { customer: "neutral", agent: "neutral", tip: null },
  compliance: [],
  suggestions: [],
  answers: [],
  answerPending: null,
  summary: null,
  ticket: null,
  recordingUrl: "",
};

// Replace the whole call from a snapshot sent by the backend.
function fromView(view) {
  return {
    ...initialCallState,
    active: true,
    callSid: view.call_sid,
    ended: view.ended,
    turns: view.turns,
    card: view.card,
    mood: view.mood,
    compliance: view.compliance,
    suggestions: view.suggestions,
    answers: view.answers || [],
    summary: view.summary,
    ticket: view.ticket,
    recordingUrl: view.recording_url,
  };
}

function applyTranscript(state, data) {
  if (data.kind === "partial") {
    return { ...state, active: true, partials: { ...state.partials, [data.speaker]: data.text } };
  }
  const turns = state.turns.some((t) => t.id === data.id) ? state.turns : [...state.turns, data];
  return { ...state, active: true, turns, partials: { ...state.partials, [data.speaker]: "" } };
}

// A finished lookup replaces the one for the same sentence. Sentences that needed no lookup are not kept.
function applyAnswer(state, entry) {
  const pending = state.answerPending?.turn_id === entry.turn_id ? null : state.answerPending;
  if (!entry.needs_lookup) return { ...state, answerPending: pending };
  const answers = [...state.answers.filter((a) => a.turn_id !== entry.turn_id), entry].slice(-20);
  return { ...state, answers, answerPending: pending };
}

export function callReducer(state, action) {
  const { type, data } = action;
  switch (type) {
    case "snapshot":
    case "call.started":
      return fromView(data);
    case "transcript":
      return applyTranscript(state, data);
    case "card":
      return { ...state, card: data };
    case "mood":
      return { ...state, mood: data };
    case "compliance":
      return { ...state, compliance: data };
    case "suggestions":
      return { ...state, suggestions: data };
    case "answer.pending":
      return { ...state, answerPending: data };
    case "answer":
      return applyAnswer(state, data);
    case "call.ended":
      return {
        ...state,
        ended: true,
        answerPending: null,
        partials: { ...initialCallState.partials },
      };
    case "summary":
      return { ...state, summary: data };
    case "ticket.created":
      return { ...state, ticket: data };
    case "reset":
      return { ...initialCallState };
    default:
      return state;
  }
}
