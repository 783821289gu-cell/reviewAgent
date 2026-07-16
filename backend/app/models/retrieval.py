from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RelatedClause:
    clause_id: str
    title: str
    clause_type: str
    text: str
    key_fields: dict
    source_location: dict
    vector_similarity: float
    embedding_mode: str
    embedding_model: str
    vector_dimension: int
    rerank_score: float
    rerank_factors: dict
    retrieval_scope: str
    query_context: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ReviewContext:
    context_id: str
    contract_type: str
    review_position: str
    current_clause: dict
    clause_type: str
    key_fields: dict
    matched_rule: dict
    related_clauses: list[dict]
    related_memory: list[dict]
    output_constraints: dict
    evidence_constraints: dict
    reduction_trace: list[str]
    formal_risk_generated: bool

    def to_dict(self) -> dict:
        return asdict(self)
