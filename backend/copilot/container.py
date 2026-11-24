"""Builds and holds every service once, at startup."""

import logging
from dataclasses import dataclass

from config.settings import Settings
from copilot.services.analyzer import TurnAnalyzer
from copilot.services.ask import AskCopilot
from copilot.services.auth import AuthService
from copilot.services.bedrock import BedrockClient
from copilot.services.compliance import ComplianceEngine
from copilot.services.customers import CustomerDirectory
from copilot.services.dashboard import DashboardService
from copilot.services.hub import PanelHub
from copilot.services.knowledge import KnowledgeBase
from copilot.services.nba import NbaCatalog
from copilot.services.sessions import SessionRegistry
from copilot.services.storage import CallStorage
from copilot.services.summarizer import CallSummarizer
from copilot.services.tickets import TicketStore

log = logging.getLogger(__name__)
REQUIRED_SETTINGS = (
    "copilot_session_secret",
    "aws_region",
    "bedrock_inference_profile_arn_fast",
    "bedrock_inference_profile_arn_summary",
)


@dataclass
class Container:
    settings: Settings
    hub: PanelHub
    registry: SessionRegistry
    customers: CustomerDirectory
    knowledge: KnowledgeBase
    compliance: ComplianceEngine
    nba: NbaCatalog
    analyzer: TurnAnalyzer
    summarizer: CallSummarizer
    ask: AskCopilot
    tickets: TicketStore
    storage: CallStorage
    dashboard: DashboardService
    auth: AuthService


def build_container(settings: Settings, *, bedrock: BedrockClient | None = None) -> Container:
    """Build every service. `bedrock` lets tests inject a fake client; None builds the real one."""
    missing = settings.missing(*REQUIRED_SETTINGS)
    if missing:
        log.warning("config/.env is missing: %s. Related features will not work.", ", ".join(missing))
    data = settings.data_dir
    hub = PanelHub()
    bedrock = bedrock or BedrockClient(settings)
    storage = CallStorage(settings)
    knowledge = KnowledgeBase(data / "knowledge" / "articles")
    nba = NbaCatalog(data / "mock" / "nba_rules.json")
    return Container(
        settings=settings,
        hub=hub,
        registry=SessionRegistry(),
        customers=CustomerDirectory(settings),
        knowledge=knowledge,
        compliance=ComplianceEngine(data / "mock" / "compliance_rules.json"),
        nba=nba,
        analyzer=TurnAnalyzer(settings, bedrock, nba, hub),
        summarizer=CallSummarizer(settings, bedrock, hub, storage),
        ask=AskCopilot(settings, bedrock, knowledge),
        tickets=TicketStore(settings),
        storage=storage,
        dashboard=DashboardService(settings, storage),
        auth=AuthService(settings),
    )
