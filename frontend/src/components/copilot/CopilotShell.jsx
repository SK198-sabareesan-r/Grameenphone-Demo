import ActionsSection from "./ActionsSection";
import AnswerSection from "./AnswerSection";
import CustomerSection from "./CustomerSection";
import SignalsSection from "./SignalsSection";

// The whole assistant in one panel. The agent types nothing to start it: the customer's number and
// location are picked up from the call, and the sections below fill in as the call goes on.
export default function CopilotShell({ call }) {
  const live = call.active && !call.ended;
  return (
    <div className="card shell">
      <div className="card-head">
        <h2>AI Copilot</h2>
        <span className={`pill ${live ? "pill-ok" : ""}`}>
          {live ? "Listening" : call.readOnly ? "Saved session" : call.ended ? "Call ended" : "Waiting for a call"}
        </span>
      </div>
      {/* Only this part scrolls: the heading and the card's edges stay in view. */}
      <div className="shell-body">
        <CustomerSection call={call} />
        <AnswerSection call={call} />
        <ActionsSection items={call.suggestions} readOnly={call.readOnly} />
        <SignalsSection mood={call.mood} compliance={call.compliance} />
        <p className="muted small">The agent decides. Copilot only suggests.</p>
      </div>
    </div>
  );
}
