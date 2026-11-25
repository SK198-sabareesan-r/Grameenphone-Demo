import CallControls from "../components/CallControls";
import CopilotShell from "../components/copilot/CopilotShell";
import SummaryTicket from "../components/SummaryTicket";
import Transcript from "../components/Transcript";

export default function LiveCallPage({ call }) {
  return (
    <div className="live-grid">
      <section className="col-main">
        <CallControls call={call} />
        <Transcript turns={call.turns} partials={call.partials} />
        <SummaryTicket call={call} />
      </section>
      <aside className="col-side">
        <CopilotShell call={call} />
      </aside>
    </div>
  );
}
