export const initialCallState = {
  callSid: "",
  active: false,
  ended: false,
  turns: [],
  partials: { customer: "", agent: "" },
  callerNumber: "",
  card: null,
  currentLocation: null,
  locationPrompt: false,
  mood: { customer: "neutral", agent: "neutral", tip: null },
  compliance: [],
  suggestions: [],
  answers: [],
  answerPending: null,
  summary: null,
  ticket: null,
  recordingUrl: "",
  sttStatus: {},
};

// Replace the whole call from a snapshot sent by the backend.
function fromView(view, previous) {
  return {
    ...initialCallState,
    sttStatus: previous.sttStatus,
    active: true,
    callSid: view.call_sid,
    ended: view.ended,
    turns: view.turns,
    callerNumber: view.caller_number || "",
    card: view.card,
    currentLocation: view.current_location || null,
    locationPrompt: Boolean(view.location_prompt),
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
      return fromView(data, state);
    case "transcript":
      return applyTranscript(state, data);
    case "card":
      return { ...state, card: data }; // null again when the agent picked the wrong customer
    case "location":
      return { ...state, currentLocation: data.current_location, locationPrompt: false };
    case "location.prompt":
      return { ...state, locationPrompt: Boolean(data.show) };
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
      return { ...state, ended: true, answerPending: null, partials: { customer: "", agent: "" } };
    case "summary":
      return { ...state, summary: data };
    case "ticket.created":
      return { ...state, ticket: data };
    case "recording":
      return { ...state, recordingUrl: data.url };
    case "stt.status":
      return { ...state, sttStatus: { ...state.sttStatus, [data.speaker]: data.status } };
    case "reset":
      return { ...initialCallState };
    default:
      return state;
  }
}
