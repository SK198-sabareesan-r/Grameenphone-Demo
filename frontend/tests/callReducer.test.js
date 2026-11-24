// Reducer and label tests. Run with: npm test   (uses Node's built-in test runner, no extra packages)
import assert from "node:assert/strict";
import { test } from "node:test";

import { callReducer, initialCallState } from "../src/state/callReducer.js";
import { MODE_LABEL, SPEAKER_LABEL, STATUS_LABEL, callStatusText, formatClock } from "../src/state/callLabels.js";

const started = (over = {}) => ({
  call_sid: "CALL_1", ended: false, status: "in_progress", agent_mode: "ai_agent", turns: [], card: null,
  mood: { customer: "neutral", agent: "neutral", tip: null }, compliance: [], suggestions: [], answers: [],
  summary: null, ticket: null, recording_url: "", ...over,
});
const run = (state, ...actions) => actions.reduce((s, [type, data]) => callReducer(s, { type, data }), state);

test("a started voice call carries status and who handles it", () => {
  const s = run(initialCallState, ["call.started", started()]);
  assert.equal(s.status, "in_progress");
  assert.equal(s.agentMode, "ai_agent");
  assert.equal(callStatusText(s), "AI agent handling");
});

test("a typed call has no status or agent mode", () => {
  const s = run(initialCallState, ["call.started", started({ status: undefined, agent_mode: undefined })]);
  assert.equal(s.status, null);
  assert.equal(s.agentMode, null);
});

test("partial lines update in place per speaker and a final line replaces the partial", () => {
  let s = run(initialCallState, ["call.started", started()]);
  s = run(s, ["transcript", { speaker: "customer", kind: "partial", text: "I need" }]);
  s = run(s, ["transcript", { speaker: "customer", kind: "partial", text: "I need help" }]);
  assert.equal(s.partials.customer, "I need help");
  assert.equal(s.turns.length, 0);
  s = run(s, ["transcript", { id: 1, speaker: "customer", kind: "final", text: "I need help with recharge", ts: 1 }]);
  assert.equal(s.partials.customer, "");
  assert.deepEqual(s.turns.map((t) => t.text), ["I need help with recharge"]);
});

test("customer, AI agent and human agent lines stay separate", () => {
  let s = run(initialCallState, ["call.started", started()]);
  s = run(
    s,
    ["transcript", { id: 1, speaker: "customer", kind: "final", text: "a", ts: 1 }],
    ["transcript", { id: 2, speaker: "ai_agent", kind: "final", text: "b", ts: 2 }],
    ["transcript", { id: 3, speaker: "human_agent", kind: "final", text: "c", ts: 3 }],
    ["transcript", { id: 4, speaker: "unknown", kind: "final", text: "d", ts: 4 }],
  );
  assert.deepEqual(s.turns.map((t) => t.speaker), ["customer", "ai_agent", "human_agent", "unknown"]);
  assert.equal(SPEAKER_LABEL.customer, "Customer");
  assert.equal(SPEAKER_LABEL.ai_agent, "AI agent");
  assert.equal(SPEAKER_LABEL.human_agent, "Agent");
  assert.equal(SPEAKER_LABEL.unknown, "Unverified speaker");
});

test("a repeated final line (same turn id) is not added twice", () => {
  const line = { id: 1, speaker: "customer", kind: "final", text: "a", ts: 1 };
  const s = run(initialCallState, ["call.started", started()], ["transcript", line], ["transcript", line]);
  assert.equal(s.turns.length, 1);
});

test("AI to human transition updates status and mode, then hold and resume", () => {
  let s = run(initialCallState, ["call.started", started()]);
  s = run(s, ["call.status", { status: "handoff_requested", agent_mode: "ai_agent" }]);
  assert.equal(callStatusText(s), STATUS_LABEL.handoff_requested);
  s = run(s, ["call.status", { status: "with_human", agent_mode: "human_agent" }]);
  assert.equal(s.agentMode, "human_agent");
  assert.equal(MODE_LABEL[s.agentMode], "Handled by human agent");
  s = run(s, ["call.status", { status: "on_hold", agent_mode: "human_agent" }]);
  assert.equal(callStatusText(s), "Customer on hold");
  s = run(s, ["call.status", { status: "with_human", agent_mode: "human_agent" }]);
  assert.equal(callStatusText(s), STATUS_LABEL.with_human);
});

test("live human-agent lines keep arriving after the transition", () => {
  let s = run(initialCallState, ["call.started", started()]);
  s = run(s, ["call.status", { status: "with_human", agent_mode: "human_agent" }]);
  s = run(s, ["transcript", { speaker: "human_agent", kind: "partial", text: "How can" }]);
  assert.equal(s.partials.human_agent, "How can");
  s = run(s, ["transcript", { id: 5, speaker: "human_agent", kind: "final", text: "How can I help?", ts: 5 }]);
  assert.equal(s.turns.at(-1).speaker, "human_agent");
});

test("call.ended marks the call ended and clears partials", () => {
  let s = run(initialCallState, ["call.started", started()]);
  s = run(s, ["transcript", { speaker: "customer", kind: "partial", text: "bye" }], ["call.ended", { call_sid: "CALL_1" }]);
  assert.equal(s.ended, true);
  assert.equal(s.status, "ended");
  assert.equal(s.partials.customer, "");
  assert.equal(callStatusText(s), "Call completed");
});

test("status text for typed calls and an empty screen", () => {
  assert.equal(callStatusText(initialCallState), "No call on screen");
  const typed = { ...initialCallState, active: true };
  assert.equal(callStatusText(typed), "Call in progress");
});

test("formatClock handles a missing time and a real one", () => {
  assert.equal(formatClock(undefined), "");
  assert.match(formatClock(1_700_000_000), /\d{1,2}:\d{2}:\d{2}/);
});
