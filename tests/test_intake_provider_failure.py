"""A provider failure in Phase 1 must surface the provider's own message.

Regression test for the misdiagnosis a real run produced. `aforge` failed three
times with

    API error (401): User not found.

and `intake_phase` still returned ``{}``, recorded as ``"status":"succeeded"``.
The pipeline carried on and died one layer up validating ``{}`` into
``IntakeResult``, so what reached the operator was::

    Error details: 400: {'error': '8 validation errors for IntakeResult
    pr_type
      Field required [type=missing, input_value={}, input_type=dict] ...

The 401 was only in the container logs, and the `.ai()` gate's copy of the same
failure had been swallowed by a bare ``except Exception``. Both are now
reported, and the phase fails instead of laundering the failure downstream.
"""

from __future__ import annotations

from typing import Any

import pytest

from pr_af.reasoners import harnesses
from pr_af.reasoners.harnesses import (
    IntakeUnavailableError,
    _intake_failure_message,
    intake_phase,
)
from pr_af.schemas.gates import IntakeGate
from pr_af.schemas.input import ChangedFile, GitHubPRData

PROVIDER_ERROR = "API error (401): User not found."


def _pr_data() -> dict[str, Any]:
    return GitHubPRData(
        owner="acme",
        repo="widgets",
        number=378,
        title="Add pooling to the spawner",
        description="Reduces per-frame allocations.",
        diff="",
        changed_files=[
            ChangedFile(path="Assets/Scripts/Spawner.cs", status="modified")
        ],
    ).model_dump()


class _StubResult:
    """Stands in for a harness result whose schema parse produced nothing."""

    def __init__(self, error_message: str | None) -> None:
        self.parsed = None
        self.error_message = error_message


class _StubApp:
    """Minimal router.app: an .ai() that raises and a .harness() that fails."""

    def __init__(self, ai_exc: Exception | None, harness_error: str | None) -> None:
        self._ai_exc = ai_exc
        self._harness_error = harness_error

    async def ai(self, *args: object, **kwargs: object) -> IntakeGate:
        if self._ai_exc is not None:
            raise self._ai_exc
        # Succeeded but not confident: the documented escalation path to the
        # harness classifier, and the case where nothing reports an error.
        return IntakeGate(pr_type="feature", complexity="standard", confident=False)

    async def harness(self, *args: object, **kwargs: object) -> _StubResult:
        return _StubResult(self._harness_error)


@pytest.fixture
def stub_app(monkeypatch: pytest.MonkeyPatch):
    def _install(ai_exc: Exception | None, harness_error: str | None) -> None:
        # Same seam the existing prompt-contract tests use: router.app is a
        # property over _agent.
        monkeypatch.setattr(
            harnesses.router, "_agent", _StubApp(ai_exc, harness_error)
        )

    return _install


async def test_provider_error_surfaces_instead_of_an_empty_result(stub_app) -> None:
    """The 401 must be in the raised message, not buried in container logs."""
    stub_app(RuntimeError(PROVIDER_ERROR), PROVIDER_ERROR)

    with pytest.raises(IntakeUnavailableError) as excinfo:
        await intake_phase(pr_data=_pr_data())

    message = str(excinfo.value)
    assert PROVIDER_ERROR in message
    # And it must not be the old silent-empty behaviour.
    assert "produced no usable output" in message


async def test_both_failing_calls_are_reported(stub_app) -> None:
    """The swallowed .ai() gate error is usually the same root cause."""
    stub_app(RuntimeError("AuthenticationError: DeepseekException - 401"), PROVIDER_ERROR)

    message = str((await _capture(intake_phase(pr_data=_pr_data()))))
    assert "harness error" in message
    assert "ai gate error" in message
    assert "DeepseekException" in message


async def test_failure_is_a_500_not_a_400(stub_app) -> None:
    """A provider that will not answer is a deployment fault, not bad input.

    review() maps ValueError to 400 and everything else to 500, so the error
    type decides the status the CI runner reports.
    """
    stub_app(RuntimeError(PROVIDER_ERROR), PROVIDER_ERROR)
    with pytest.raises(IntakeUnavailableError) as excinfo:
        await intake_phase(pr_data=_pr_data())
    assert not isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value, RuntimeError)


async def test_no_provider_message_still_points_somewhere(stub_app) -> None:
    """When nothing reports a reason, say where to look rather than nothing.

    The gate escalates cleanly (not confident, no exception) and the harness
    then produces no output and no error message — so there is no provider text
    to quote.
    """
    stub_app(None, None)

    with pytest.raises(IntakeUnavailableError) as excinfo:
        await intake_phase(pr_data=_pr_data())
    message = str(excinfo.value)
    assert "check the node logs" in message
    assert "PR_AF_MODEL" in message


async def test_message_less_exception_reports_its_type(stub_app) -> None:
    """An exception with no text must not render as a dangling "Type:"."""
    stub_app(RuntimeError(""), None)

    with pytest.raises(IntakeUnavailableError) as excinfo:
        await intake_phase(pr_data=_pr_data())
    message = str(excinfo.value)
    assert "ai gate error: RuntimeError;" in message
    assert "RuntimeError:" not in message


def test_message_builder_shapes() -> None:
    both = _intake_failure_message(harness_error=" boom ", gate_error=" gate ")
    assert "harness error: boom" in both
    assert "ai gate error: gate" in both

    harness_only = _intake_failure_message(harness_error="boom", gate_error=None)
    assert "harness error: boom" in harness_only
    assert "ai gate error" not in harness_only

    # Whitespace-only messages count as absent.
    neither = _intake_failure_message(harness_error="  ", gate_error="")
    assert "check the node logs" in neither
    # Every variant ends with the actionable hint.
    for msg in (both, harness_only, neither):
        assert "PR_AF_MODEL" in msg


async def _capture(coro: object) -> Exception:
    """Await a coroutine expected to raise, returning the exception."""
    try:
        await coro  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001 - the point of the helper
        return exc
    raise AssertionError("expected the coroutine to raise")
