"""Builds and holds every service once, at startup."""

import logging
from dataclasses import dataclass

from config.settings import Settings
from copilot.services.analyzer import TurnAnalyzer
from copilot.services.areas import NetworkAreas
from copilot.services.ask import AskCopilot
from copilot.services.assist import LiveAssist
from copilot.services.bedrock import BedrockClient
from copilot.services.compliance import ComplianceEngine
from copilot.services.customers import CustomerDirectory
from copilot.services.dashboard import DashboardService
from copilot.services.hub import PanelHub
from copilot.services.identity import IdentityService
from copilot.services.knowledge import KnowledgeBase
from copilot.services.location import LocationService
from copilot.services.nba import NbaCatalog
from copilot.services.sessions import SessionRegistry
from copilot.services.storage import CallStorage
from copilot.services.summarizer import CallSummarizer
from copilot.services.tickets import TicketStore

log = logging.getLogger(__name__)
REQUIRED_SETTINGS = (
    "public_base_url",
    "aws_region",
    "bedrock_inference_profile_arn_fast",
    "bedrock_inference_profile_arn_summary",
    "sarvam_api_key",
    "twilio_account_sid",
    "twilio_auth_token",
    "twilio_api_key_sid",
    "twilio_api_key_secret",
)


@dataclass
class Container:
    settings: Settings
    hub: PanelHub
    registry: SessionRegistry
    customers: CustomerDirectory
    areas: NetworkAreas
    identity: IdentityService
    location: LocationService
    knowledge: KnowledgeBase
    compliance: ComplianceEngine
    nba: NbaCatalog
    analyzer: TurnAnalyzer
    assist: LiveAssist
    summarizer: CallSummarizer
    ask: AskCopilot
    tickets: TicketStore
    storage: CallStorage
    dashboard: DashboardService


def build_container(settings: Settings) -> Container:
    missing = settings.missing(*REQUIRED_SETTINGS)
    if missing:
        log.warning("config/.env is missing: %s. Related features will not work.", ", ".join(missing))
    data = settings.data_dir
    hub, bedrock = PanelHub(), BedrockClient(settings)
    storage = CallStorage(settings)
    knowledge = KnowledgeBase(data / "knowledge" / "articles")
    nba = NbaCatalog(data / "mock" / "nba_rules.json")
    customers = CustomerDirectory(settings)
    areas = NetworkAreas(data / "mock" / "network_areas.json")
    return Container(
        settings=settings,
        hub=hub,
        registry=SessionRegistry(),
        customers=customers,
        areas=areas,
        identity=IdentityService(customers, areas, hub),
        location=LocationService(data / "mock" / "network_keywords.json", areas, hub),
        knowledge=knowledge,
        compliance=ComplianceEngine(data / "mock" / "compliance_rules.json"),
        nba=nba,
        analyzer=TurnAnalyzer(settings, bedrock, nba, hub),
        assist=LiveAssist(settings, bedrock, knowledge, hub),
        summarizer=CallSummarizer(settings, bedrock, hub, storage),
        ask=AskCopilot(settings, bedrock, knowledge),
        tickets=TicketStore(settings),
        storage=storage,
        dashboard=DashboardService(settings, storage),
    )
