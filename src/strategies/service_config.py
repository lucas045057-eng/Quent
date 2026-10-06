"""Local research credentials. Reading this file never activates trading."""
from decimal import Decimal
import os
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr

class Section(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    enabled: bool=Field(default=True,strict=True)

class CoinalyzeConfig(Section):
    base_url: Literal['https://api.coinalyze.net/v1']='https://api.coinalyze.net/v1'
    api_key: SecretStr=SecretStr('')
    max_markets: int=Field(default=2,ge=2,le=6,strict=True)

class PublicCrossMarketConfig(Section):
    enabled: bool=Field(default=False,strict=True)
    network_transport: Literal['native','windows_system_proxy']='native'
    timeout_seconds: float=Field(default=10,gt=0,le=20)

class OpenAIConfig(Section):
    base_url: Literal['https://api.openai.com/v1','https://xfastapi.ai']='https://api.openai.com/v1'
    api_key: SecretStr=SecretStr('')
    network_transport: Literal['native','windows_system_proxy']='native'
    model: str=Field(default='gpt-5.4-mini-2026-03-17',pattern=r'^[a-zA-Z0-9._-]{1,100}$')
    timeout_seconds: float=Field(default=10,gt=0,le=60)
    max_output_tokens: int=Field(default=4096,ge=512,le=8192,strict=True)
    max_input_bytes: int=Field(default=262144,ge=4096,le=524288,strict=True)
    # USD / 1M tokens: official GPT-5.4 mini pricing checked 2026-10-03.
    input_usd_per_million: Decimal=Field(default=Decimal('.75'),gt=0)
    cached_input_usd_per_million: Decimal=Field(default=Decimal('.075'),ge=0)
    output_usd_per_million: Decimal=Field(default=Decimal('4.50'),gt=0)

class ResearchServicesConfig(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    schema_version: Literal['RESEARCH_SERVICES_V2']='RESEARCH_SERVICES_V2'
    coinalyze: CoinalyzeConfig=Field(default_factory=CoinalyzeConfig)
    public_cross_market: PublicCrossMarketConfig=Field(default_factory=PublicCrossMarketConfig)
    openai: OpenAIConfig=Field(default_factory=OpenAIConfig)


def load_service_config(path: Path) -> ResearchServicesConfig:
    try:
        path=Path(path)
        if path.is_symlink():raise ValueError('symlink')
        payload=json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(payload,dict):raise ValueError('object required')
        # Retired source settings are discarded before strict validation. This
        # keeps old local files readable while making saved enable flags inert.
        for retired in ('gnews','xoomar','events'):
            payload.pop(retired,None)
        return ResearchServicesConfig.model_validate(payload)
    except (OSError,ValueError):
        raise ValueError('RESEARCH_SERVICES_CONFIG_INVALID') from None


def configured_services(env=None):
    """Explicit file is authoritative; invalid files never fall back to env keys."""
    env=os.environ if env is None else env
    path=env.get('QUANT_V2_SERVICES_CONFIG_PATH')
    default=Path(__file__).resolve().parents[2]/'config/research_services.local.json'
    if not path and default.is_file():path=str(default)
    if path:return load_service_config(Path(path))
    return ResearchServicesConfig(
        coinalyze=CoinalyzeConfig(api_key=env.get('COINALYZE_API_KEY','')),
        openai=OpenAIConfig(api_key=env.get('OPENAI_API_KEY','')))
