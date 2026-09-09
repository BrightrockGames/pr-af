"""Configuration for PR-AF.

All behavioral tuning in one place. Model assignments, budget caps, loop limits,
comment formatting, review strictness. Most changes start here.

Follows the Contract-AF config pattern: centralized, typed, auditable.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .schemas.input import ReviewInput


# ---------------------------------------------------------------------------
# OpenRouter model routing
# ---------------------------------------------------------------------------

OPENROUTER_ROUTING_PREFIX = "openrouter/"


def openrouter_ai_model(model: str) -> str:
    """Ensure an ``.ai()`` model string routes through OpenRouter.

    The ``.ai()`` seam is hardwired to OpenRouter — app.py passes
    ``OPENROUTER_API_KEY`` and ``api_base=https://openrouter.ai/api/v1`` — but
    LiteLLM picks the provider from the model string's own prefix, and its
    provider base URL wins over ``api_base``. So a bare OpenRouter slug like
    ``deepseek/deepseek-v4-flash-0731`` is read as *provider* ``deepseek``:

        >>> litellm.get_llm_provider(model="deepseek/deepseek-v4-flash-0731")
        ('deepseek-v4-flash-0731', 'deepseek', ..., 'https://api.deepseek.com/beta')

    which sends the OpenRouter key to api.deepseek.com and comes back
    ``401 {"message": "User not found.", "code": 401}`` — labelled
    ``DeepseekException``, so the failure looks like a DeepSeek problem when
    nothing was meant to talk to DeepSeek at all.

    LiteLLM *consumes* a leading ``openrouter/`` as its routing prefix, leaving
    the real slug for the OpenRouter API. Adding the prefix is therefore what
    makes the configured credentials and the configured endpoint agree.

    Both forms of ``PR_AF_MODEL`` work as a result: prefixed is passed through,
    bare is normalised here. Note the HARNESS model is deliberately left alone —
    opencode's generated config wants the prefix (the entrypoint normalises it
    there), and the AgentField SDK's aforge provider strips it before invoking
    the CLI, so either form reaches the same model.
    """
    model = model.strip()
    if not model or model.startswith(OPENROUTER_ROUTING_PREFIX):
        return model
    return OPENROUTER_ROUTING_PREFIX + model


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def credential(name: str, default: str = "") -> str:
    """Read a credential from the environment, trimmed of surrounding whitespace.

    Secrets pass through several hands before they reach a provider — a GitHub
    Actions secret, a compose ``environment:`` entry, a ``.env`` file — and
    every one of them stores the value byte for byte. A key pasted with a
    trailing newline is forwarded verbatim, so the provider receives an
    ``Authorization: Bearer <key><newline>`` header it cannot match. OpenRouter
    answers that with

        401 {"error": {"message": "User not found.", "code": 401}}

    which is indistinguishable from a genuinely unknown key. The operator then
    validates the key by hand, finds that it works, and the real fault stays
    hidden — exactly the loop a production run spent several attempts inside.

    Trimming is safe: no provider issues a key whose value depends on leading
    or trailing whitespace. Read every credential through this rather than
    ``os.getenv`` so the trim cannot be forgotten at one of the seams.
    """
    return os.getenv(name, default).strip()


# ---------------------------------------------------------------------------
# Repo-specific review guidance (AGENTS.md)
# ---------------------------------------------------------------------------

REPO_GUIDANCE_FILENAME = "AGENTS.md"

# Cap on the guidance text injected into prompts. A repo-root AGENTS.md is
# normally a page or two; the cap stops a large one from crowding out the diff,
# evidence pack and PR context that the review actually depends on.
MAX_REPO_GUIDANCE_CHARS = 20_000


def read_repo_guidance(repo_path: str | None) -> str:
    """Read the checked-out repo's root ``AGENTS.md``, or "" when there is none.

    This is the same convention OpenAI Codex review uses, so a repo that already
    has an ``AGENTS.md`` for other agentic tools needs no PR-AF-specific file.
    Only the repository ROOT is read — PR-AF does not walk up from each changed
    file, because its reviewers are scoped to dimensions (which span files)
    rather than to a single file.

    Unreadable or oversized files degrade to a truncated value or "" rather than
    failing the review: missing conventions make the review less specific, not
    wrong.
    """
    if not repo_path:
        return ""
    path = Path(repo_path) / REPO_GUIDANCE_FILENAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""
    text = text.strip()
    if len(text) > MAX_REPO_GUIDANCE_CHARS:
        text = (
            text[:MAX_REPO_GUIDANCE_CHARS].rstrip()
            + "\n"
            + "\n"
            + "[truncated by PR-AF at "
            + str(MAX_REPO_GUIDANCE_CHARS)
            + " characters]"
        )
    return text


# ---------------------------------------------------------------------------
# Git subprocess settings
#
# These live here rather than in app.py so orchestrator.py (which also shells
# out to git, for the repo_path diff) can share them without importing app.py
# and creating an import cycle.
# ---------------------------------------------------------------------------

DEFAULT_GIT_TIMEOUT_SECONDS = 600


def git_timeout_seconds() -> float:
    """Wall-clock ceiling for every git subprocess, from PR_AF_GIT_TIMEOUT_SECONDS.

    One knob covers clone, fetch, checkout and diff. The old per-call literals
    were sized for github.com-sized repos: ``checkout`` in particular had 30s,
    which a large monorepo (deep tree, tens of thousands of files) blows through
    while git is still writing the working tree — the review then died with
    "git checkout ... timed out after 30 seconds" instead of reviewing.

    The default (600s) is the largest of the previous literals, so no operation
    gets a *shorter* budget than before. Non-positive or unparsable values fall
    back to the default rather than disabling the timeout: an unbounded git call
    hangs the whole review with no diagnostic, which is strictly worse than a
    timeout error.
    """
    raw = os.getenv("PR_AF_GIT_TIMEOUT_SECONDS", "")
    if not raw:
        return float(DEFAULT_GIT_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[PR-AF] Ignoring invalid PR_AF_GIT_TIMEOUT_SECONDS={raw!r} (must be a "
            f"positive number of seconds); using {DEFAULT_GIT_TIMEOUT_SECONDS}",
            flush=True,
        )
        return float(DEFAULT_GIT_TIMEOUT_SECONDS)
    if value <= 0:
        print(
            f"[PR-AF] Ignoring non-positive PR_AF_GIT_TIMEOUT_SECONDS={raw!r}; "
            f"using {DEFAULT_GIT_TIMEOUT_SECONDS}",
            flush=True,
        )
        return float(DEFAULT_GIT_TIMEOUT_SECONDS)
    return value


def skip_git_lfs() -> bool:
    """Whether to skip Git-LFS content download at checkout (default: yes).

    PR-AF reviews source code, not binary payloads, so pulling LFS objects is
    pure cost: on an asset-heavy repo (Unity/game art, ML weights) it can be
    orders of magnitude more bytes than the source, and the reviewers cannot
    read the result anyway. With this on, LFS-tracked paths check out as their
    small pointer stubs.

    This used to be an *implicit* side effect of git-lfs not being installed in
    the runtime images. That was fragile — installing git-lfs for any other
    reason, or running the node on a host that has it, silently switched every
    review to downloading gigabytes of assets. Setting GIT_LFS_SKIP_SMUDGE=1
    explicitly makes the behaviour independent of what happens to be on PATH.

    Set PR_AF_SKIP_GIT_LFS=0/false/no to let LFS smudge normally (requires
    git-lfs on PATH; the Docker images install and register it).
    """
    return os.getenv("PR_AF_SKIP_GIT_LFS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def git_env() -> dict[str, str]:
    """Environment for every git subprocess PR-AF runs.

    GIT_TERMINAL_PROMPT=0 / GIT_ASKPASS=echo make a missing credential fail fast
    instead of blocking on an interactive prompt. GIT_LFS_SKIP_SMUDGE is set
    explicitly (see skip_git_lfs) rather than left to whether git-lfs happens to
    be installed.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    if skip_git_lfs():
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
    else:
        # An inherited GIT_LFS_SKIP_SMUDGE would otherwise defeat an explicit
        # opt-in to real LFS content.
        env.pop("GIT_LFS_SKIP_SMUDGE", None)
    return env


class BudgetConfig(BaseModel):
    """Global and per-phase budget caps."""

    # Global caps
    max_cost_usd: float = 2.0
    max_duration_seconds: int = 3600

    # Phase-level cost allocation (USD)
    phase_budgets: dict[str, float] = Field(
        default_factory=lambda: {
            "intake": 0.05,
            "anatomy": 0.15,
            "meta_selectors": 0.30,  # 3 parallel lenses
            "review": 0.90,  # Most budget goes here
            "adversary": 0.40,  # Parallel batches
            "cross_ref": 0.30,
            "coverage": 0.10,
            "synthesis": 0.00,  # Code, no LLM cost
            "output": 0.00,  # Code, no LLM cost
        }
    )

    # Concurrency
    # Review-wide agent budget: the total number of leaf agent invocations
    # (reviewers, obligation verifiers, adversary batches, gates, …) allowed
    # in flight at once, across ALL phases — including phases that run
    # concurrently (coverage loop ‖ consistency-verify). This is the knob that
    # bounds peak opencode-subprocess count and therefore peak memory (#65).
    max_concurrent_agents: int = Field(
        default_factory=lambda: int(os.getenv("PR_AF_MAX_CONCURRENT_AGENTS", "8"))
    )
    # Deprecated alias: historically capped only reviewer dimensions. Still
    # honored — the effective budget is min(max_concurrent_agents, this).
    max_concurrent_reviewers: int = 8

    # Cap on consistency-verify obligations (one verifier agent each).
    max_consistency_obligations: int = Field(
        default_factory=lambda: int(os.getenv("PR_AF_MAX_CONSISTENCY_OBLIGATIONS", "12"))
    )

    # Inner loop caps (per-reviewer)
    max_reference_follows_per_reviewer: int = 3
    max_child_spawns_per_reviewer: int = 2

    # Middle loop caps (cross-agent)
    max_cross_ref_deep_dives: int = 5

    # Outer loop caps (pipeline)
    max_coverage_iterations: int = 2

    # Recursive sub-review depth (1=flat, 2=one sub-level, 3=max)
    max_review_depth: int = 2

    # Evidence-pack-first reviewers: pre-read each dimension's target files (+ imports)
    # and inject them so reviewers reason over a primed pack instead of cold-navigating
    # the repo over many opencode turns. Strictly-additive context; the latency lever.
    evidence_pack_reviewers: bool = Field(
        default_factory=lambda: os.getenv("PR_AF_EVIDENCE_PACK", "1").lower() not in ("0", "false", "no")
    )


class ModelConfig(BaseModel):
    """Model routing per agent.

    Philosophy: budget models for gates/classification,
    premium models for planning/reviewing/challenging.
    Plan quality = review quality, so planner gets premium.
    """

    intake_gate: str = "budget"  # .ai() fast classification
    intake_fallback: str = "mid"  # .harness() when not confident
    anatomy_semantic: str = "mid"  # Narrative understanding
    planner: str = "premium"  # THE critical agent: plan quality = review quality
    reviewer: str = "premium"  # Deep code analysis
    cross_ref: str = "premium"  # Interaction detection needs best reasoning
    adversary: str = "premium"  # Challenge quality matters
    coverage_gate: str = "budget"  # Simple completeness check
    dedup_gate: str = "budget"  # Near-duplicate detection


class ScoringConfig(BaseModel):
    """Deterministic scoring weights and multipliers.

    LLMs reason about issues; code computes scores.
    Same findings always produce same scores.
    """

    base_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "critical": 1.0,
            "important": 0.7,
            "suggestion": 0.3,
            "nitpick": 0.1,
        }
    )

    multipliers: dict[str, float] = Field(
        default_factory=lambda: {
            "cross_ref_compound": 1.5,  # Cross-ref found compound risk
            "adversary_confirmed": 1.3,  # Adversary confirmed exploitation
            "adversary_challenged": 0.5,  # Adversary successfully challenged
            "ai_generated_pr": 1.2,  # Extra weight for AI-generated PRs
            "blast_radius_high": 1.2,  # Change affects many files (>10)
        }
    )

    confidence_thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "critical": 0.2,
            "important": 0.3,
            "suggestion": 0.4,
            "nitpick": 0.4,
        }
    )


class CommentConfig(BaseModel):
    """Comment formatting and posting preferences."""

    min_severity: str = "nitpick"  # Minimum severity to include in summary/comments
    max_comments: int = 25  # Cap inline comments to avoid overwhelming
    include_suggestions: bool = True  # Include ```suggestion blocks
    include_dimension_attribution: bool = True  # Show which dimension found it
    include_confidence: bool = True  # Show confidence score
    suggestion_mode: str = "comment"  # comment | code

    # Parallel `.ai()` polish pass: rewrites each comment body to be more
    # concise and developer-focused right before posting. On any per-call
    # failure, the original body is kept.
    polish_enabled: bool = True

    # Parallel `.ai()` merge-gate pass: classifies each finding as blocking
    # vs non-blocking using a tight release-manager bar (build/security/data
    # loss/contract break/regression only). Findings that don't meet the bar
    # stay as advisory inline comments and never trigger REQUEST_CHANGES.
    # Default ON for production noise reduction. Failures default to advisory.
    merge_gate_enabled: bool = True

    # Calibrated post-worthiness gate: an experienced-reviewer pass that posts only
    # genuinely worth-posting findings (drops nits + unverifiable claims). Validated
    # as a precision/F1 lever (F1 ~0.38->0.48) at a recall cost (~0.69->0.52) — a
    # precision<->recall dial. DEFAULT OFF to preserve recall-first behavior; flip on
    # (PR_AF_POSTWORTHINESS_GATE=1) for F1-max / leaderboard mode.
    post_worthiness_gate: bool = Field(
        default_factory=lambda: os.getenv("PR_AF_POSTWORTHINESS_GATE", "").lower() in ("1", "true", "yes")
    )

    severity_emojis: dict[str, str] = Field(
        default_factory=lambda: {
            "critical": "🔴",
            "important": "🟠",
            "suggestion": "🔵",
            "nitpick": "⚪",
        }
    )

    # Review event logic
    # Any critical → REQUEST_CHANGES
    # Important only → COMMENT
    # Suggestions/nitpicks only → APPROVE with comments
    # Nothing → APPROVE clean


class DepthProfile(BaseModel):
    """Pre-built profiles for review depth."""

    max_dimensions: int = 6
    model_tier: str = "standard"  # budget | standard | premium


DEPTH_PROFILES: dict[str, DepthProfile] = {
    "quick": DepthProfile(max_dimensions=3, model_tier="budget"),
    "standard": DepthProfile(max_dimensions=6, model_tier="standard"),
    "deep": DepthProfile(max_dimensions=12, model_tier="premium"),
}

# Auto-depth thresholds (lines changed → depth)
AUTO_DEPTH_THRESHOLDS = {
    100: "quick",  # < 100 lines → quick
    500: "standard",  # 100-500 lines → standard
    # > 500 lines → deep
}


class HITLConfig(BaseModel):
    """Human-in-the-loop review gate (mirrors SWE-AF's plan-phase approval).

    When enabled, PR-AF does not post its review directly. Instead it summarizes
    the findings, sends a hax form request to a workspace member, and pauses
    until they approve a subset, request a re-review with instructions, or
    reject. Auto-enables when ``HAX_API_KEY`` is set — same trigger SWE-AF uses
    (``build_hax_client_from_env`` returns ``None`` when it is unset, which the
    orchestrator treats as "HITL off, post directly").
    """

    # Mirrors the on/off switch in build_hax_client_from_env: HITL is active
    # only when HAX_API_KEY is present. Kept here for observability/overrides.
    enabled: bool = Field(
        default_factory=lambda: bool(os.getenv("HAX_API_KEY", "").strip())
    )
    # Optional routing: which hax workspace user receives the request.
    approval_user_id: str | None = Field(
        default_factory=lambda: os.getenv("AGENTFIELD_APPROVAL_USER_ID") or None
    )
    # How long the pause stays open before it expires (treated as a reject).
    # Plain config default — matches SWE-AF's BuildConfig.approval_expires_in_hours
    # (not env-driven, to avoid introducing PR-AF-specific env var names).
    approval_expires_in_hours: int = 72
    # How many "re-review with instructions" rounds before giving up (no post).
    # Matches SWE-AF's BuildConfig.max_plan_revision_iterations.
    max_review_revisions: int = 2


class ReviewConfig(BaseModel):
    """Top-level configuration combining all sub-configs."""

    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    models: ModelConfig = Field(default_factory=ModelConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    comments: CommentConfig = Field(default_factory=CommentConfig)
    hitl: HITLConfig = Field(default_factory=HITLConfig)

    # File ignore patterns (glob)
    ignore_paths: list[str] = Field(
        default_factory=lambda: [
            "*.md",
            "*.txt",
            ".github/**",
            "vendor/**",
            "node_modules/**",
            "**/*.generated.*",
            "**/*.min.js",
            "**/*.min.css",
            "**/package-lock.json",
            "**/yarn.lock",
            "**/poetry.lock",
        ]
    )

    # Project-specific review hints (passed to planner as additional context)
    # These are NOT hardcoded rules — the planner decides how to use them.
    hints: list[str] = Field(default_factory=list)

    # Depth override rules
    depth_rules: list[dict] = Field(default_factory=list)

    @classmethod
    def from_input(cls, review_input: ReviewInput) -> ReviewConfig:
        """Merge per-call API overrides into defaults (SEC-AF pattern)."""
        config = cls()

        config.budget.max_cost_usd = review_input.max_cost_usd
        config.budget.max_duration_seconds = review_input.max_duration_seconds
        if review_input.max_concurrent_agents is not None:
            config.budget.max_concurrent_agents = review_input.max_concurrent_agents
        if review_input.max_concurrent_reviewers is not None:
            config.budget.max_concurrent_reviewers = review_input.max_concurrent_reviewers
        if review_input.max_coverage_iterations is not None:
            config.budget.max_coverage_iterations = review_input.max_coverage_iterations
        config.budget.max_review_depth = min(review_input.max_review_depth, 3)

        if review_input.models:
            for field_name, model_id in review_input.models.items():
                if hasattr(config.models, field_name):
                    setattr(config.models, field_name, model_id)

        if review_input.ignore_paths:
            config.ignore_paths = list(set(config.ignore_paths + review_input.ignore_paths))

        if review_input.hints:
            config.hints = review_input.hints

        if hasattr(review_input, "suggestion_mode") and review_input.suggestion_mode:
            config.comments.suggestion_mode = review_input.suggestion_mode

        return config

    @classmethod
    def from_yaml(cls, path: str) -> ReviewConfig:
        """Load config from .pr-af.yml file."""
        from pathlib import Path as _Path

        import yaml

        config_path = _Path(path)
        if not config_path.exists():
            return cls()

        with config_path.open() as f:
            data = yaml.safe_load(f) or {}

        return cls(**data)


class AIIntegrationConfig(BaseModel):
    provider: str = Field(default_factory=lambda: os.getenv("PR_AF_PROVIDER", "aforge"))
    harness_model: str = Field(
        default_factory=lambda: os.getenv("PR_AF_MODEL", "minimax/minimax-m2.5")
    )
    ai_model: str = Field(
        default_factory=lambda: os.getenv(
            "PR_AF_AI_MODEL",
            os.getenv("PR_AF_MODEL", "minimax/minimax-m2.5"),
        )
    )
    max_turns: int = Field(default_factory=lambda: int(os.getenv("PR_AF_MAX_TURNS", "50")))
    max_retries: int = Field(default_factory=lambda: int(os.getenv("PR_AF_AI_MAX_RETRIES", "3")))
    initial_backoff_seconds: float = Field(
        default_factory=lambda: float(os.getenv("PR_AF_AI_INITIAL_BACKOFF_SECONDS", "2.0"))
    )
    max_backoff_seconds: float = Field(default_factory=lambda: float(os.getenv("PR_AF_AI_MAX_BACKOFF_SECONDS", "8.0")))
    opencode_bin: str = Field(default_factory=lambda: os.getenv("PR_AF_OPENCODE_BIN", "opencode"))
    aforge_bin: str = Field(
        default_factory=lambda: os.getenv(
            "PR_AF_AFORGE_BIN",
            os.getenv("AFORGE_BIN", "aforge"),
        )
    )
    harness_bin: str = Field(default_factory=lambda: os.getenv("PR_AF_HARNESS_BIN", ""))
    opencode_server: str | None = Field(default_factory=lambda: os.getenv("PR_AF_OPENCODE_SERVER"))

    @classmethod
    def from_env(cls) -> AIIntegrationConfig:
        return cls()

    def provider_env(self) -> dict[str, str]:
        env_keys = (
            "OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "GH_TOKEN",
        )
        env: dict[str, str] = {key: value for key in env_keys if (value := credential(key))}
        env["AGENTFIELD_AFORGE_COMMAND"] = os.getenv("AGENTFIELD_AFORGE_COMMAND", "exec")
        xdg = os.getenv("XDG_DATA_HOME") or os.path.join(tempfile.gettempdir(), "opencode-shared-data")
        os.makedirs(xdg, exist_ok=True)
        env["XDG_DATA_HOME"] = xdg
        return env
