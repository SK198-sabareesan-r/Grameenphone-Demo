// Feeds panel WebSocket messages (a JSON array on stdin) through the real callReducer and prints each step.
// Used by backend/tests/test_live_transcript_flow.py so the Python tests exercise the real frontend state code.
import { callReducer, initialCallState } from "../../src/state/callReducer.js";
import { callStatusText } from "../../src/state/callLabels.js";

let input = "";
for await (const chunk of process.stdin) input += chunk;
let state = initialCallState;
const steps = [];
for (const message of JSON.parse(input)) {
  state = callReducer(state, { type: message.type, data: message.data });
  steps.push({
    type: message.type, active: state.active, ended: state.ended, status: state.status, agentMode: state.agentMode,
    statusText: callStatusText(state), turns: state.turns.map((t) => ({ speaker: t.speaker, text: t.text })), partials: state.partials,
  });
}
console.log(JSON.stringify(steps));
