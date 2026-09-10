"""One-request execution with cumulative, reservation-aware job budgets."""

import asyncio
import math
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from cxplorer.ai.providers import OutputContractDetails, ProviderError
from cxplorer.insights.errors import InsightError

TRANSIENT_CODES = frozenset({"timeout", "provider_unavailable", "rate_limited"})

if TYPE_CHECKING:
    from cxplorer.ai.config import TaskSettings
    from cxplorer.ai.providers import AIProviderClient


@dataclass(frozen=True)
class Allowance:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0

    def __add__(self, other: "Allowance") -> "Allowance":
        return Allowance(
            self.calls + other.calls,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.tool_calls + other.tool_calls,
        )

    def fits(self, ceiling: "Allowance") -> bool:
        return (
            self.calls <= ceiling.calls
            and self.input_tokens <= ceiling.input_tokens
            and self.output_tokens <= ceiling.output_tokens
            and self.tool_calls <= ceiling.tool_calls
        )


@dataclass
class BudgetLedger:
    ceiling: Allowance
    spent: Allowance = field(default_factory=Allowance)
    in_flight: dict[str, Allowance] = field(default_factory=dict)
    protected: dict[str, Allowance] = field(default_factory=dict)
    exhausted: bool = False

    def total(self, *, excluding: str | None = None) -> Allowance:
        total = self.spent
        for key, reservation in self.in_flight.items():
            if key != excluding:
                total += reservation
        for key, reservation in self.protected.items():
            if key != excluding:
                total += reservation
        return total

    def affordable(self, key: str, reservation: Allowance) -> bool:
        return not self.exhausted and (self.total(excluding=key) + reservation).fits(self.ceiling)

    def reserve(self, key: str, reservation: Allowance) -> None:
        # No await occurs between this check and reservation, including parallel writers.
        if key in self.in_flight:
            raise TaskFailure("task_conflict", "This generation task is already running.")
        if not self.affordable(key, reservation):
            raise TaskFailure(
                "budget_exhausted",
                "The remaining generation budget cannot safely cover this task and its dependents.",
            )
        self.protected.pop(key, None)
        self.in_flight[key] = reservation

    def settle(self, key: str, actual: Allowance | None) -> None:
        reserved = self.in_flight.pop(key)
        self.spent += reserved if actual is None else actual
        if not self.total().fits(self.ceiling):
            self.exhausted = True


class TaskFailure(InsightError):
    def __init__(
        self,
        code: str,
        public_message: str,
        *,
        retryable: bool = False,
        task_key: str = "",
        contract_details: OutputContractDetails | None = None,
    ) -> None:
        super().__init__(code, public_message)
        self.retryable = retryable
        self.task_key = task_key
        self.contract_details = contract_details


class TaskExecutor:
    def __init__(
        self,
        provider: "AIProviderClient",
        tasks: "Mapping[str, TaskSettings]",
        ledger: BudgetLedger,
        *,
        retry_limits: Mapping[str, int],
        vendor_semaphores: Mapping[str, asyncio.Semaphore],
    ) -> None:
        self.provider = provider
        self.tasks = tasks
        self.ledger = ledger
        self.retry_limits = retry_limits
        self.vendor_semaphores = vendor_semaphores
        self.deadline = 0.0

    def allowance(self, task_id: str, *, input_tokens: int | None = None) -> Allowance:
        task = self.tasks[task_id]
        return Allowance(
            calls=1,
            input_tokens=task.max_input_tokens if input_tokens is None else input_tokens,
            output_tokens=task.max_output_tokens,
            tool_calls=task.max_tool_calls if task.use_web_search else 0,
        )

    def protect(self, plans: Mapping[str, str]) -> None:
        self.protect_allowances({key: self.allowance(task_id) for key, task_id in plans.items()})

    def protect_allowances(self, plans: Mapping[str, Allowance]) -> None:
        for key, reservation in plans.items():
            if key not in self.ledger.in_flight:
                self.ledger.protected[key] = reservation
        if not self.ledger.total().fits(self.ledger.ceiling):
            raise TaskFailure(
                "budget_exhausted",
                "The remaining budget cannot cover the required generation and quality-review tasks.",
            )

    def unprotect(self, *keys: str) -> None:
        for key in keys:
            self.ledger.protected.pop(key, None)

    def estimate(
        self,
        task_id: str,
        instructions: str,
        data: dict,
        output_type: type[BaseModel],
        *,
        allowed_domains: tuple[str, ...] = (),
    ) -> int:
        try:
            estimate = self.provider.estimate_input_tokens(
                task_id, instructions, data, output_type, allowed_domains=allowed_domains
            )
        except ProviderError as error:
            raise TaskFailure(error.code, error.public_message) from None
        if not isinstance(estimate, int) or isinstance(estimate, bool) or estimate <= 0:
            raise TaskFailure(
                "invalid_estimate", "The configured provider cannot estimate this task."
            )
        return estimate

    def plan(
        self,
        key: str,
        task_id: str,
        instructions: str,
        data: dict,
        output_type: type[BaseModel],
        *,
        allowed_domains: tuple[str, ...] = (),
    ) -> Allowance:
        estimate = self.estimate(
            task_id, instructions, data, output_type, allowed_domains=allowed_domains
        )
        if estimate > self.tasks[task_id].max_input_tokens:
            raise TaskFailure(
                "task_input_limit",
                f"The bounded {task_id.replace('_', ' ')} input is too large. Use narrower sources.",
                task_key=key,
            )
        return self.allowance(task_id, input_tokens=estimate)

    async def execute[T: BaseModel](
        self,
        key: str,
        task_id: str,
        instructions: str,
        data: dict,
        output_type: type[T],
        *,
        allowed_domains: tuple[str, ...] = (),
    ) -> T:
        task = self.tasks[task_id]
        reservation = self.plan(
            key, task_id, instructions, data, output_type, allowed_domains=allowed_domains
        )
        task_deadline = min(self.deadline, time.monotonic() + task.timeout_seconds)
        retry_limit = self.retry_limits[task.vendor]
        for attempt in range(retry_limit + 1):
            remaining = task_deadline - time.monotonic()
            if remaining <= 0:
                raise TaskFailure(
                    "task_timeout",
                    "The generation task reached its time limit.",
                    retryable=True,
                    task_key=key,
                )
            submitted = False
            try:
                async with asyncio.timeout(remaining):
                    async with self.vendor_semaphores[task.vendor]:
                        self.ledger.reserve(key, reservation)
                        submitted = True
                        generated = await self.provider.generate(
                            task_id,
                            instructions,
                            data,
                            output_type,
                            allowed_domains=allowed_domains,
                        )
            except asyncio.CancelledError:
                if submitted:
                    self.ledger.settle(key, None)
                raise
            except (ProviderError, TimeoutError) as error:
                if submitted:
                    # A lost response can still be billed, including output and search tools.
                    actual = None
                    if isinstance(error, ProviderError) and error.usage is not None:
                        actual = Allowance(
                            1,
                            error.usage.input_tokens,
                            error.usage.output_tokens,
                            error.usage.tool_calls,
                        )
                    self.ledger.settle(key, actual)
                    if actual is not None and not actual.fits(self.allowance(task_id)):
                        self.ledger.exhausted = True
                if isinstance(error, ProviderError):
                    failure = TaskFailure(
                        error.code,
                        error.public_message,
                        retryable=error.retryable and error.code in TRANSIENT_CODES,
                        task_key=key,
                        contract_details=error.contract_details,
                    )
                    retry_after = error.retry_after
                else:
                    failure = TaskFailure(
                        "provider_timeout",
                        "The AI provider did not finish before the task deadline.",
                        retryable=True,
                        task_key=key,
                    )
                    retry_after = None
                if not failure.retryable or attempt >= retry_limit:
                    raise failure from None
                delay = (
                    retry_after
                    if retry_after is not None
                    else min(2**attempt, 8) + random.uniform(0, 0.25)
                )
                if (
                    not math.isfinite(delay)
                    or delay < 0
                    or delay >= task_deadline - time.monotonic()
                    or not self.ledger.affordable(key, reservation)
                ):
                    raise failure from None
                await asyncio.sleep(delay)
                continue
            except BaseException:
                if submitted:
                    self.ledger.settle(key, None)
                raise

            usage = generated.usage
            counts = (usage.input_tokens, usage.output_tokens, usage.tool_calls)
            if any(
                not isinstance(count, int) or isinstance(count, bool) or count < 0
                for count in counts
            ):
                self.ledger.settle(key, None)
                self.ledger.exhausted = True
                raise TaskFailure(
                    "invalid_usage",
                    "The provider returned unusable usage accounting.",
                    task_key=key,
                )
            actual = Allowance(1, usage.input_tokens, usage.output_tokens, usage.tool_calls)
            self.ledger.settle(key, actual)
            if not actual.fits(self.allowance(task_id)) or self.ledger.exhausted:
                self.ledger.exhausted = True
                raise TaskFailure(
                    "budget_exhausted",
                    "The provider exceeded an approved task or job allowance.",
                    task_key=key,
                )
            if task.use_web_search and usage.tool_calls == 0:
                raise TaskFailure(
                    "search_not_performed",
                    "The provider did not perform the required official company search.",
                    task_key=key,
                )
            try:
                if not isinstance(generated.value, output_type):
                    raise TaskFailure(
                        "invalid_schema",
                        "The AI task did not return its registered structured artifact.",
                        task_key=key,
                    )
                return output_type.model_validate_json(
                    generated.value.model_dump_json(), strict=True
                )
            except ValidationError:
                raise TaskFailure(
                    "invalid_schema",
                    "The AI task returned an invalid structured artifact.",
                    task_key=key,
                ) from None
        raise AssertionError("Unreachable retry state")
