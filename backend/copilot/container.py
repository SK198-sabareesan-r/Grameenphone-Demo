"""Builds and holds every service once, at startup."""

import logging
from dataclasses import dataclass

from config.settings import Settings
from copilot.repo import Repo, build_repo
from copilot.services.analyzer import TurnAnalyzer
from copilot.services.areas import NetworkAreas
from copilot.services.ask import AskCopilot
from copilot.services.assist import LiveAssist
from copilot.services.auth import AuthService
from copilot.services.bedrock import BedrockClient
from copilot.services.compliance import ComplianceEngine
from copilot.services.conversation_store import ConversationStore
from copilot.services.dashboard import DashboardService
from copilot.services.hub import PanelHub
from copilot.services.identity import IdentityService
from copilot.services.kb_retriever import KnowledgeRetriever
from copilot.services.knowledge import KnowledgeBase
from copilot.services.location import LocationService
from copilot.services.nba import NbaCatalog
from copilot.services.sessions import SessionRegistry
from copilot.services.storage import CallStorage
from copilot.services.summarizer import CallSummarizer
from copilot.services.tickets import TicketService

log = logging.getLogger(__name__)
REQUIRED_SETTINGS = (
    "copilot_session_secret",
    "aws_region",
    "bedrock_inference_profile_arn_fast",
    "bedrock_inference_profile_arn_summary",
    "internal_api_token",  # voice-agent events (M3) are refused with 503 without it
)


@dataclass
class Container:
    settings: Settings
    repo: Repo
    hub: PanelHub
    registry: SessionRegistry
    areas: NetworkAreas
    identity: IdentityService
    location: LocationService
    knowledge: KnowledgeBase
    retriever: KnowledgeRetriever
    compliance: ComplianceEngine
    nba: NbaCatalog
    analyzer: TurnAnalyzer
    assist: LiveAssist
    summarizer: CallSummarizer
    ask: AskCopilot
    tickets: TicketService
    storage: CallStorage
    dashboard: DashboardService
    auth: AuthService
    conversations: ConversationStore


def build_container(settings: Settings, *, bedrock: BedrockClient | None = None) -> Container:
    """Build every service. `bedrock` lets tests inject a fake client; None builds the real one."""
    missing = settings.missing(*REQUIRED_SETTINGS)
    if missing:
        log.warning("config/.env is missing: %s. Related features will not work.", ", ".join(missing))
    data = settings.data_dir
    repo = build_repo(settings)
    hub = PanelHub()
    bedrock = bedrock or BedrockClient(settings)  # tests inject a fake client
    storage = CallStorage(settings, repo)
    knowledge = KnowledgeBase(data / "knowledge" / "articles")
    retriever = KnowledgeRetriever(settings, bedrock, knowledge)
    nba = NbaCatalog(data / "mock" / "nba_rules.json", repo)
    areas = NetworkAreas(repo)
    return Container(
        settings=settings,
        repo=repo,
        hub=hub,
        registry=SessionRegistry(),
        areas=areas,
        identity=IdentityService(repo, areas, hub),
        location=LocationService(data / "mock" / "network_keywords.json", areas, hub),
        knowledge=knowledge,
        retriever=retriever,
        compliance=ComplianceEngine(data / "mock" / "compliance_rules.json"),
        nba=nba,
        analyzer=TurnAnalyzer(settings, bedrock, nba, hub),
        assist=LiveAssist(settings, bedrock, retriever, hub, repo),
        summarizer=CallSummarizer(settings, bedrock, hub, storage),
        ask=AskCopilot(settings, bedrock, retriever),
        tickets=TicketService(settings, repo),
        storage=storage,
        dashboard=DashboardService(settings, storage),
        auth=AuthService(settings),
        conversations=ConversationStore(settings.conversations_dir),
    )
