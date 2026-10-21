"""POST /api/v1/insights/query: Groq-powered, data-grounded answers. Optional: core endpoints never depend on this."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, utcnow
from app.errors import ApiError
from app.schemas import InsightOut, InsightQuery
from app.security import rate_limit
from app.services.insights import answer_question
from app.services.llm import GroqClient, LLMError

router = APIRouter(prefix="/api/v1/insights", tags=["ai"])


def llm_dep(settings: Settings = Depends(get_settings)) -> GroqClient:  # overridden in tests
    return GroqClient(settings)


@router.post("/query", response_model=InsightOut,
             dependencies=[Depends(rate_limit("insights", "insights_rate_limit_per_minute"))])
def query(body: InsightQuery, db: Session = Depends(get_db), llm: GroqClient = Depends(llm_dep)):
    try:
        return answer_question(db, llm, body.question, body.location_id, utcnow())
    except LLMError as e:
        # FR-020: AI failure is reported cleanly; the map, charts and alerts keep working.
        raise ApiError(e.status, e.code, str(e)) from e
