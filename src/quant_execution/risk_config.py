"""User-editable risk limits; the selected revision is frozen into each plan."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Literal
import os
import tempfile
import fcntl
from pydantic import Field, model_validator
from strategies.contracts import Record
from .contracts import AccountSnapshotV1
from .risk import RiskPolicyV1

class HoldLimits(Record):
    one_three: int = Field(default=10800, gt=0, strict=True, alias="1_3H")
    three_eight: int = Field(default=10800, gt=0, strict=True, alias="3_8H")
    eight_twentyfour: int = Field(default=10800, gt=0, strict=True, alias="8_24H")

class RiskConfigV2(Record):
    schema_version: str = "RISK_CONFIG_V2"
    revision: int = Field(default=1,gt=0,strict=True)
    risk_per_trade_equity_ratio: D = Field(default=D("0.0025"),gt=0,le=1)
    max_position_notional_equity_ratio: D = Field(default=D("0.10"),gt=0)
    max_total_exposure_equity_ratio: D = Field(default=D("0.10"),gt=0)
    max_reserved_risk_equity_ratio: D = Field(default=D("0.0025"),gt=0,le=1)
    max_leverage: D = Field(default=D("1"),ge=1)
    max_open_positions: int = Field(default=1,gt=0,strict=True)
    max_open_intents: int = Field(default=1,gt=0,strict=True)
    sizing_mode: Literal["RISK_BASED","FIXED_NOTIONAL_RATIO"]="RISK_BASED"
    fixed_notional_equity_ratio: D = Field(default=D("0.10"),gt=0)
    allow_pyramiding: bool = Field(default=False,strict=True)
    allow_averaging_down: bool = Field(default=False,strict=True)
    max_hold_seconds_by_horizon: HoldLimits = Field(default_factory=HoldLimits)
    cooldown_seconds: int = Field(default=1800,ge=0,strict=True)
    max_spread_bps: D = Field(default=D("8"),ge=0)
    max_slippage_bps: D = Field(default=D("10"),ge=0,lt=10000)
    quote_max_age_seconds: int = Field(default=5,gt=0,strict=True)
    account_max_age_seconds: int = Field(default=5,gt=0,strict=True)
    intent_ttl_seconds: int = Field(default=30,gt=0,strict=True)
    max_risk_amount: D | None=Field(default=None,gt=0)
    max_position_notional_amount: D | None=Field(default=None,gt=0)
    max_total_exposure_amount: D | None=Field(default=None,gt=0)
    max_margin_amount: D | None=Field(default=None,gt=0)

    @model_validator(mode="after")
    def coherent_limits(self):
        if self.risk_per_trade_equity_ratio>self.max_reserved_risk_equity_ratio:
            raise ValueError("single risk exceeds total risk budget")
        if self.max_position_notional_equity_ratio>self.max_total_exposure_equity_ratio:
            raise ValueError("single position exceeds total exposure")
        if self.max_total_exposure_equity_ratio>self.max_leverage:
            raise ValueError("exposure exceeds configured leverage")
        if self.schema_version!="RISK_CONFIG_V2":
            raise ValueError("unsupported risk config schema")
        return self

    def hold_seconds(self,horizon: str) -> int:
        names={"1_3H":"one_three","3_8H":"three_eight","8_24H":"eight_twentyfour"}
        return getattr(self.max_hold_seconds_by_horizon,names[horizon])

def load_risk_config(path: Path) -> RiskConfigV2:
    if path.is_symlink():
        raise ValueError("RISK_CONFIG_SYMLINK_REJECTED")
    return RiskConfigV2.model_validate_json(path.read_text(encoding="utf-8"))

def save_risk_config(path: Path, config: RiskConfigV2, *, expected_digest: str | None) -> None:
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix(path.suffix+".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        prior=load_risk_config(path) if path.exists() else None
        if (prior.digest if prior else None)!=expected_digest:
            raise ValueError("CONCURRENT_RISK_CONFIG_EDIT")
        if prior and config.revision<=prior.revision:
            raise ValueError("RISK_REVISION_NOT_INCREASING")
        name=None
        try:
            with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,delete=False) as f:
                name=f.name
                f.write(config.model_dump_json(indent=2,by_alias=True)+"\n")
                f.flush();os.fsync(f.fileno())
            os.replace(name,path);name=None
        finally:
            if name is not None:Path(name).unlink(missing_ok=True)

@dataclass(frozen=True)
class RiskConfigState:
    status: str
    config: RiskConfigV2 | None
    reason: str | None
    @property
    def new_risk_allowed(self):return self.status=="ACTIVE" and self.config is not None

class RiskConfigLoader:
    def __init__(self,path: Path):
        self.path=Path(path);self.last_valid=None
    def poll(self) -> RiskConfigState:
        try:
            current=load_risk_config(self.path)
            if self.last_valid and current.digest!=self.last_valid.digest and current.revision<=self.last_valid.revision:
                raise ValueError("RISK_REVISION_NOT_INCREASING")
            self.last_valid=current
            return RiskConfigState("ACTIVE",current,None)
        except (OSError,ValueError):
            return RiskConfigState("ERROR",self.last_valid,"RISK_CONFIG_INVALID_OR_UNAVAILABLE")

@dataclass(frozen=True)
class RiskContextV2:
    active_positions: tuple = ()
    pending_intents: tuple = ()
    remaining_margin: D = D("0")
    reserved_risk: D = D("0")
    reconciliation_status: str = "UNKNOWN"
    last_flat_at: datetime | None = None
    total_exposure: D = D("0")

def _cap(value,absolute):return min(value,absolute) if absolute is not None else value

def resolve_risk_policy(config: RiskConfigV2, account: AccountSnapshotV1, *,
    code_version: str, decision_versions: tuple[str,...]) -> RiskPolicyV1:
    if account.equity<=0:raise ValueError("PAPER_EQUITY_REQUIRED")
    notional=_cap(account.equity*config.max_position_notional_equity_ratio,config.max_position_notional_amount)
    if config.sizing_mode=="FIXED_NOTIONAL_RATIO":
        notional=min(notional,account.equity*config.fixed_notional_equity_ratio)
    return RiskPolicyV1(version=f"risk-v2-r{config.revision}",max_notional=notional,
        max_margin=_cap(account.available_balance,config.max_margin_amount),
        max_leverage=config.max_leverage,
        max_risk=_cap(account.equity*config.risk_per_trade_equity_ratio,config.max_risk_amount),
        max_exposure=_cap(account.equity*config.max_total_exposure_equity_ratio,config.max_total_exposure_amount),
        max_reserved_risk=account.equity*config.max_reserved_risk_equity_ratio,
        risk_config_digest=config.digest,max_open_positions=config.max_open_positions,
        cooldown_seconds=config.cooldown_seconds,allow_pyramiding=config.allow_pyramiding,
        allow_averaging_down=config.allow_averaging_down,
        supported_aux_versions=(("pattern_policy_version","2.0.0"),("freshness_policy_version","2.0.0"),
            ("ttl_policy_version","2.0.0"),("evidence_schema_version","PHASE9_EVIDENCE_CHAIN_V1"),("prompt_version","2.0.0")),
        max_open_intents=config.max_open_intents,quote_max_age_seconds=config.quote_max_age_seconds,
        account_max_age_seconds=config.account_max_age_seconds,max_spread_bps=config.max_spread_bps,
        max_slippage_bps=config.max_slippage_bps,intent_ttl_seconds=config.intent_ttl_seconds,
        supported_decision_versions=decision_versions,supported_code_versions=(code_version,))
