// Text shown for call state. Kept free of React so it can be tested with `node --test`.

// Three separate identities, plus speech whose speaker could not be verified. "agent" is the older
// name for the human agent (older call records).
export const SPEAKER_LABEL = {
  customer: "Customer",
  ai_agent: "AI agent",
  human_agent: "Agent",
  agent: "Agent",
  unknown: "Unverified speaker",
};

export const STATUS_LABEL = {
  in_progress: "Call in progress",
  ai_resolution_attempt: "AI resolution attempt",
  handoff_requested: "Human handoff requested",
  dialing_human: "Human agent dialing",
  with_human: "Human agent connected · AI background assist",
  on_hold: "Customer on hold",
  human_disconnected: "Human agent disconnected",
  handoff_failed: "Human handoff failed · AI agent handling",
  ended: "Call completed",
};

export const MODE_LABEL = {
  ai_agent: "Handled by AI agent",
  human_agent: "Handled by human agent",
};

// Status line for the call bar. Typed calls (no `status`) keep the original three states.
export function callStatusText(call) {
  if (!call.active) return "No call on screen";
  if (call.ended) return STATUS_LABEL.ended;
  if (call.status === "in_progress" && call.agentMode === "ai_agent") return "AI agent handling";
  if (call.status && STATUS_LABEL[call.status]) return STATUS_LABEL[call.status];
  return "Call in progress";
}

// Seconds since the epoch (the backend sends time.time()) -> "14:03:07". Empty if missing.
export function formatClock(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

// Seconds since the epoch -> "12 Mar, 14:03". Empty if missing.
export function formatDateTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString([], {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Length of a call in seconds -> "4 min 12 s". Empty if either time is missing.
export function formatDuration(startedAt, endedAt) {
  if (!startedAt || !endedAt || endedAt < startedAt) return "";
  const total = Math.round(endedAt - startedAt);
  const minutes = Math.floor(total / 60);
  return minutes ? `${minutes} min ${total % 60} s` : `${total} s`;
}
