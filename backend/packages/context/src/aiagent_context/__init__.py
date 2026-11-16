"""AI-02 isolated source-preserving Context Manager."""

from .compaction import ContextService, MockExcerptSummarizer, ProviderOverflow, audit_proposal, verify_tool_pairs
from .hybrid import HybridCompactionPipeline, HybridOptimizer
from .models import (
    Anchor,
    AnchorKind,
    ContextPlan,
    ContextRequest,
    MemoryProjection,
    ModelBudget,
    OptimizationReceipt,
    SafeStop,
    Scope,
    SourceBackedFact,
    SourceInput,
    SummaryClaim,
    SummaryProposal,
    ToolEvent,
)
from .ports import MockArtifactStore, MockHeadPort
from .semantic import GatedLlmNliVerifier, HeuristicNliVerifier, SemanticVerifierPort
from .tokenizer import ModelBoundTokenEstimator, TokenEstimatorPort

__all__ = [
    "Anchor",
    "AnchorKind",
    "ContextPlan",
    "ContextRequest",
    "ContextService",
    "GatedLlmNliVerifier",
    "HeuristicNliVerifier",
    "HybridCompactionPipeline",
    "HybridOptimizer",
    "MemoryProjection",
    "MockArtifactStore",
    "MockExcerptSummarizer",
    "MockHeadPort",
    "ModelBoundTokenEstimator",
    "ModelBudget",
    "OptimizationReceipt",
    "ProviderOverflow",
    "SafeStop",
    "Scope",
    "SemanticVerifierPort",
    "SourceBackedFact",
    "SourceInput",
    "SummaryClaim",
    "SummaryProposal",
    "TokenEstimatorPort",
    "ToolEvent",
    "audit_proposal",
    "verify_tool_pairs",
]
