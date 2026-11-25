export const initialCallState = {
  callSid: "",
  active: false,
  ended: false,
  // Voice calls: status (in_progress | handoff_requested | dialing_human | with_human | on_hold |
  // human_disconnected | ended) and who handles the call (ai_agent | human_agent). Null for typed calls.
  status: null,
  agentMode: null,
  turns: [],
  // One in-progress line per participant: customer, AI agent (voice bot), human agent.
  partials: { customer: "", ai_agent: "", human_agent: "" },
  callerNumber: "",
  card: null,
  currentLocation: null,
  locationPrompt: false,
  // The last mobile number heard on the call: { number, found, speaker }. Null until one is heard.
  heard: null,
  mood: { customer: "neutral", agent: "neutral", tip: null },
  compliance: [],
  suggestions: [],
  answers: [],
  answerPending: null,
  summary: null,
  ticket: null,
  recordingUrl: "",
  // True for a finished call opened from session history: shown as it was saved, nothing can be changed.
  readOnly: false,
  startedAt: null, // seconds since the epoch; only set for saved calls
};

// Replace the whole call from a snapshot sent by the backend.
export function callFromView(view, { readOnly = false } = {}) {
  return {
    ...initialCallState,
    active: true,
    callSid: view.call_sid,
    ended: view.ended,
    status: view.status ?? null,
    agentMode: view.agent_mode ?? null,
    turns: view.turns,
    callerNumber: view.caller_number || "",
    card: view.card,
    currentLocation: view.current_location || null,
    locationPrompt: Boolean(view.location_prompt),
    heard: view.heard || null,
    mood: view.mood,
    compliance: view.compliance,
    suggestions: view.suggestions,
    answers: view.answers || [],
    summary: view.summary,
    ticket: view.ticket,
    recordingUrl: view.recording_url,
    startedAt: view.started_at ?? null,
    readOnly,
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
      return callFromView(data);
    case "transcript":
      return applyTranscript(state, data);
    case "card":
      return { ...state, card: data, heard: data ? state.heard : null }; // null again when the agent picked the wrong customer
    case "identity.heard":
      return { ...state, heard: data };
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
    case "call.status":
      return { ...state, status: data.status, agentMode: data.agent_mode ?? state.agentMode };
    case "call.ended":
      return {
        ...state,
        ended: true,
        status: state.status ? "ended" : state.status,
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
