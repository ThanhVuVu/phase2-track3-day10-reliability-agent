import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        cost_budget: float = 100.0,
        static_fallback_message: str = "Hệ thống hiện đang quá tải. Vui lòng thử lại sau.",
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cost_budget = cost_budget
        self.static_fallback_message = static_fallback_message
        self.cumulative_cost = 0.0

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback."""
        start_time = time.monotonic()
        
        # 1. Cache Check
        if self.cache is not None:
            cached, score = self.cache.get(prompt)
            if cached is not None:
                duration_ms = (time.monotonic() - start_time) * 1000
                return GatewayResponse(
                    text=cached,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=duration_ms,
                    estimated_cost=0.0
                )

        # 2. Provider Routing with Fallback
        last_error: str | None = None
        for i, provider in enumerate(self.providers):
            # Cost check: skip expensive providers if over 80% budget (stretch goal logic)
            if self.cumulative_cost >= self.cost_budget * 0.8 and provider.cost_per_1k_tokens > 0.01:
                last_error = "budget_limit_approaching"
                continue

            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                
                # Update cumulative cost
                self.cumulative_cost += response.estimated_cost
                
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                
                duration_ms = (time.monotonic() - start_time) * 1000
                route_type = "primary" if i == 0 else "fallback"
                route = f"{route_type}:{provider.name}"
                
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=duration_ms,
                    estimated_cost=response.estimated_cost,
                )
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                continue

        # 3. Static Fallback
        duration_ms = (time.monotonic() - start_time) * 1000
        return GatewayResponse(
            text=self.static_fallback_message,
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=duration_ms,
            estimated_cost=0.0,
            error=last_error,
        )
