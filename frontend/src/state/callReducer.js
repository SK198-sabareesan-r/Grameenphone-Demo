export const initialCallState = {
  callSid: "",
  active: false,
  ended: false,
  turns: [],
  partials: { customer: "", agent: "" },
  card: null,
  mood: { customer: "neutral", agent: "neutral", tip: null },
  compliance: [],
  suggestions: [],
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
    card: view.card,
    mood: view.mood,
    compliance: view.compliance,
    suggestions: view.suggestions,
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

export function callReducer(state, action) {
  const { type, data } = action;
  switch (type) {
    case "snapshot":
    case "call.started":
      return fromView(data, state);
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
    case "call.ended":
      return { ...state, ended: true, partials: { customer: "", agent: "" } };
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
