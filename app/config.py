"""Settings from the environment. The key is never logged or returned."""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path


def parse_trusted_proxies(raw: str) -> tuple[IPv4Network | IPv6Network, ...]:
    """TRUSTED_PROXY: comma-separated IPs or CIDR ranges. Empty = trust nobody."""
    return tuple(ip_network(p.strip(), strict=False) for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    filter_model: str
    daily_budget_rub: float
    rate_limit_per_hour: int
    retention_days: int
    var_dir: Path
    examples_path: Path
    trusted_proxies: tuple
    llm_timeout_s: float

    @property
    def live(self) -> bool:
        return bool(self.api_key and self.base_url)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env
        root = Path(__file__).resolve().parent.parent
        return cls(
            api_key=env.get("LLM_API_KEY", "").strip(),
            base_url=env.get("LLM_BASE_URL", "").strip(),
            model=env.get("ARENA_MODEL", "timeweb/gpt-oss-120b").strip(),
            filter_model=env.get("FILTER_MODEL", "timeweb/gpt-oss-120b").strip(),
            daily_budget_rub=float(env.get("DAILY_BUDGET_RUB", "15")),
            rate_limit_per_hour=int(env.get("RATE_LIMIT_PER_HOUR", "20")),
            retention_days=int(env.get("RETENTION_DAYS", "30")),
            var_dir=Path(env.get("VAR_DIR", str(root / "var"))),
            examples_path=Path(env.get("EXAMPLES_PATH", str(root / "evals" / "examples.json"))),
            trusted_proxies=parse_trusted_proxies(env.get("TRUSTED_PROXY", "")),
            llm_timeout_s=float(env.get("LLM_TIMEOUT_S", "30")),
        )
