import AnswerPanel from "../components/AnswerPanel";
import AskCopilot from "../components/AskCopilot";
import CompliancePanel from "../components/CompliancePanel";
import CustomerCard from "../components/CustomerCard";
import MoodPanel from "../components/MoodPanel";
import Softphone from "../components/Softphone";
import SummaryTicket from "../components/SummaryTicket";
import Suggestions from "../components/Suggestions";
import Transcript from "../components/Transcript";

export default function LiveCallPage({ call, softphone }) {
  return (
    <div className="live-grid">
      <section className="col-main">
        <Softphone softphone={softphone} callActive={call.active} callEnded={call.ended} />
        <Transcript turns={call.turns} partials={call.partials} />
        <SummaryTicket call={call} />
      </section>
      <aside className="col-side">
        <CustomerCard card={call.card} />
        <AnswerPanel
          answers={call.answers}
          pending={call.answerPending}
          callActive={call.active}
          callEnded={call.ended}
        />
        <MoodPanel mood={call.mood} />
        <CompliancePanel items={call.compliance} />
        <Suggestions items={call.suggestions} />
        <AskCopilot />
      </aside>
    </div>
  );
}
