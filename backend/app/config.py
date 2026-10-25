"""Settings from environment variables (no extra dependency)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./roadloop.db"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6379/0"))
    media_dir: str = field(default_factory=lambda: _env("MEDIA_DIR", "/media"))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"))
    webhook_token: str = field(default_factory=lambda: _env("WEBHOOK_TOKEN", "dev-token"))

    tinyfish_mode: str = field(default_factory=lambda: _env("TINYFISH_MODE", "mock"))
    tinyfish_api_key: str = field(default_factory=lambda: _env("TINYFISH_API_KEY") or "mock-key")
    mock_base: str = field(default_factory=lambda: _env("MOCK_TINYFISH_URL", "http://mock:8100").rstrip("/"))

    sla_days: int = field(default_factory=lambda: int(_env("SLA_DAYS", "7")))
    monitor_cron: str = field(default_factory=lambda: _env("MONITOR_CRON", "CRON_TZ=Asia/Kolkata 0 */6 * * *"))
    merge_radius_m: float = field(default_factory=lambda: float(_env("MERGE_RADIUS_M", "12")))
    min_riders: int = field(default_factory=lambda: int(_env("MIN_RIDERS", "2")))
    min_confidence: float = field(default_factory=lambda: float(_env("MIN_CONFIDENCE", "0.85")))
    default_portal: str = field(default_factory=lambda: _env("DEFAULT_PORTAL", "demo"))
    max_escalations: int = field(default_factory=lambda: int(_env("MAX_ESCALATIONS", "3")))
    status_check_hours: float = field(default_factory=lambda: float(_env("STATUS_CHECK_HOURS", "6")))
    geocoder: str = field(default_factory=lambda: _env("GEOCODER", "nominatim"))   # nominatim | off
    geocoder_user_agent: str = field(default_factory=lambda: _env(
        "GEOCODER_USER_AGENT", "RoadLoop-hackathon/0.1 (HackIIITD demo)"))
    verify_radius_m: float = field(default_factory=lambda: float(_env("VERIFY_RADIUS_M", "25")))
    # Optional: when set, /v1/admin/* needs the header X-Admin-Token (the dashboard sends it).
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN"))
    # Area memory: reuse a known authority for new potholes within this geohash precision
    # (6 = cells about 1.2 x 0.6 km, plus neighbours) instead of asking TinyFish again.
    area_precision: int = field(default_factory=lambda: int(_env("AREA_PRECISION", "6")))
    # Hands-free jolt capture on the phone (sent to the rider app via /v1/config).
    bump_min_g: float = field(default_factory=lambda: float(_env("BUMP_MIN_G", "0.4")))
    bump_noise_k: float = field(default_factory=lambda: float(_env("BUMP_NOISE_K", "3.5")))
    bump_min_speed_kmh: float = field(default_factory=lambda: float(_env("BUMP_MIN_SPEED_KMH", "8")))

    @property
    def portal_base(self) -> str:
        """Where the fictional demo portal is reached by whoever files on it.
        mock: the mock container over the compose network. live: TinyFish's cloud browser must
        reach it from the internet, so it goes through the API's /portal proxy on the tunnel."""
        explicit = _env("PORTAL_BASE_URL")
        if explicit:
            return explicit.rstrip("/")
        return self.mock_base if self.tinyfish_mode == "mock" else self.public_base_url

    @property
    def agent_base(self) -> str:
        return self.mock_base if self.tinyfish_mode == "mock" else "https://agent.tinyfish.ai"

    @property
    def search_base(self) -> str:
        return f"{self.mock_base}/search" if self.tinyfish_mode == "mock" else "https://api.search.tinyfish.ai"

    @property
    def fetch_base(self) -> str:
        return f"{self.mock_base}/fetch" if self.tinyfish_mode == "mock" else "https://api.fetch.tinyfish.ai"

    @property
    def webhooks_enabled(self) -> bool:
        # The real API only accepts https:// webhook URLs; without a tunnel we poll instead.
        return self.tinyfish_mode == "mock" or self.public_base_url.startswith("https://")

    def webhook_url(self, kind: str) -> str:
        # In mock mode the mock container calls the API over the compose network.
        base = _env("WEBHOOK_BASE_URL") or ("http://api:8000" if self.tinyfish_mode == "mock" else self.public_base_url)
        return f"{base.rstrip('/')}/webhooks/{self.webhook_token}/{kind}"

    def media_url(self, rel_path: str) -> str:
        return f"{self.public_base_url}/media/{rel_path.lstrip('/')}"


settings = Settings()
