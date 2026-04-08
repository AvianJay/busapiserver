from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


DEFAULT_TDX_BASE_URL = "https://tdx.transportdata.tw/api/basic"
DEFAULT_TDX_TOKEN_URL = (
    "https://tdx.transportdata.tw/auth/realms/TDXConnect/"
    "protocol/openid-connect/token"
)
DEFAULT_TDX_CITIES = (
    "Taipei",
    "NewTaipei",
    "Taoyuan",
    "Taichung",
    "Tainan",
    "Kaohsiung",
    "Keelung",
    "Hsinchu",
    "HsinchuCounty",
    "MiaoliCounty",
    "ChanghuaCounty",
    "NantouCounty",
    "YunlinCounty",
    "Chiayi",
    "ChiayiCounty",
    "PingtungCounty",
    "YilanCounty",
    "HualienCounty",
    "TaitungCounty",
    "PenghuCounty",
    "KinmenCounty",
    "LienchiangCounty",
)

CITY_PREFIX_TO_NAME = {
    "CHA": "ChanghuaCounty",
    "CYI": "Chiayi",
    "CYQ": "ChiayiCounty",
    "HSQ": "HsinchuCounty",
    "HSZ": "Hsinchu",
    "HUA": "HualienCounty",
    "ILA": "YilanCounty",
    "KEE": "Keelung",
    "KHH": "Kaohsiung",
    "KIN": "KinmenCounty",
    "LIE": "LienchiangCounty",
    "MIA": "MiaoliCounty",
    "NAN": "NantouCounty",
    "NWT": "NewTaipei",
    "PEN": "PenghuCounty",
    "PIF": "PingtungCounty",
    "TAO": "Taoyuan",
    "TNN": "Tainan",
    "TPE": "Taipei",
    "TTT": "TaitungCounty",
    "TXG": "Taichung",
    "YUN": "YunlinCounty",
}


def load_dotenv(dotenv_path: str | Path) -> None:
    path = Path(dotenv_path)
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def _split_csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default

    items = []
    seen = set()
    for item in value.split(","):
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        items.append(cleaned)
    return tuple(items) or default


@dataclass(frozen=True)
class Settings:
    project_dir: Path
    db_path: Path
    tdx_client_id: str | None
    tdx_client_secret: str | None
    tdx_base_url: str
    tdx_token_url: str
    tdx_cities: tuple[str, ...]
    tdx_request_timeout: int
    tdx_token_refresh_skew: int
    realtime_cache_ttl: int

    @classmethod
    def from_env(cls) -> "Settings":
        project_dir = Path(__file__).resolve().parent.parent
        load_dotenv(project_dir / ".env")
        db_path = Path(os.getenv("BUS_DB_PATH", project_dir / "bus.db")).resolve()
        return cls(
            project_dir=project_dir,
            db_path=db_path,
            tdx_client_id=os.getenv("TDX_CLIENT_ID"),
            tdx_client_secret=os.getenv("TDX_CLIENT_SECRET"),
            tdx_base_url=os.getenv("TDX_BASE_URL", DEFAULT_TDX_BASE_URL).rstrip("/"),
            tdx_token_url=os.getenv("TDX_TOKEN_URL", DEFAULT_TDX_TOKEN_URL),
            tdx_cities=_split_csv(os.getenv("TDX_CITIES"), DEFAULT_TDX_CITIES),
            tdx_request_timeout=int(os.getenv("TDX_REQUEST_TIMEOUT", "30")),
            tdx_token_refresh_skew=int(os.getenv("TDX_TOKEN_REFRESH_SKEW", "300")),
            realtime_cache_ttl=int(os.getenv("REALTIME_CACHE_TTL", "15")),
        )

    def require_tdx_credentials(self) -> None:
        if self.tdx_client_id and self.tdx_client_secret:
            return
        raise RuntimeError(
            "TDX_CLIENT_ID and TDX_CLIENT_SECRET must both be set in the environment."
        )


def guess_city_from_routeid(routeid: str, allowed_cities: tuple[str, ...]) -> str | None:
    prefix = routeid[:3].upper()
    city = CITY_PREFIX_TO_NAME.get(prefix)
    if city and city in allowed_cities:
        return city
    return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
