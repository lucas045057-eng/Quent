from dataclasses import dataclass, field
from pathlib import Path
import os
import re

from psycopg.conninfo import conninfo_to_dict

LOOPBACK = {'127.0.0.1', 'localhost', '::1'}


@dataclass(frozen=True)
class DashboardConfig:
    root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[3])
    dsn: str | None = field(default=None, repr=False)
    schema: str = 'public'
    port: int = 3000

    def __post_init__(self):
        object.__setattr__(self, 'root', Path(self.root).resolve())
        if not self.root.is_dir() or not 1024 <= self.port <= 65535:
            raise ValueError('INVALID_LOCAL_CONFIGURATION')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', self.schema):
            raise ValueError('INVALID_SCHEMA')
        if self.dsn:
            try:
                info = conninfo_to_dict(self.dsn)
            except Exception:
                raise ValueError('INVALID_DATABASE_CONFIGURATION') from None
            if (info.get('host') not in LOOPBACK or info.get('hostaddr', info['host']) not in LOOPBACK
                    or 'service' in info):
                raise ValueError('DATABASE_MUST_BE_LOOPBACK')

    @classmethod
    def from_env(cls):
        try:
            return cls(root=Path(os.environ.get('QUANT_DASHBOARD_ROOT', Path(__file__).resolve().parents[3])),
                dsn=os.environ.get('QUANT_DASHBOARD_DSN') or None,
                schema=os.environ.get('QUANT_DASHBOARD_SCHEMA', 'public'),
                port=int(os.environ.get('QUANT_DASHBOARD_PORT', '3000')))
        except (ValueError, TypeError):
            raise ValueError('INVALID_LOCAL_CONFIGURATION') from None
