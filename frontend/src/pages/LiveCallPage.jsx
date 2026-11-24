import AnswerPanel from "../components/AnswerPanel";
import AskCopilot from "../components/AskCopilot";
import CompliancePanel from "../components/CompliancePanel";
import CustomerCard from "../components/CustomerCard";
import IdentityForm from "../components/IdentityForm";
import MoodPanel from "../components/MoodPanel";
import NetworkCheck from "../components/NetworkCheck";
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
        {call.card ? (
          <CustomerCard card={call.card} callEnded={call.ended} />
        ) : (
          <IdentityForm callerNumber={call.callerNumber} callActive={call.active} callEnded={call.ended} />
        )}
        <NetworkCheck
          prompt={call.locationPrompt}
          current={call.currentLocation}
          registered={call.card?.registered_status}
          callActive={call.active}
          callEnded={call.ended}
        />
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
