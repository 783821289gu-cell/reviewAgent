from threading import Lock

from services.langgraph_review_agent import ReviewOrchestratorAgent


_DEFAULT_AGENT: ReviewOrchestratorAgent | None = None
_DEFAULT_AGENT_LOCK = Lock()


def get_default_review_agent() -> ReviewOrchestratorAgent:
    global _DEFAULT_AGENT
    with _DEFAULT_AGENT_LOCK:
        if _DEFAULT_AGENT is None:
            _DEFAULT_AGENT = ReviewOrchestratorAgent()
        return _DEFAULT_AGENT
