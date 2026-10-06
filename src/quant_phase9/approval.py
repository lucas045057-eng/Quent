"""Explicit producer for PHASE9_POLICY_APPROVAL_V1 artifacts.

The caller must supply the human approver and exact Git revision. This module
never selects or edits policy patterns and never overwrites an existing file.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .canonical import canonical_sha256
from .policy import (
    PolicyApprovalStatusV1,
    PolicyApprovalV1,
    PolicyManifestV1,
    _read_json,
)

_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def create_approval_artifact(
    manifest_path: Path,
    output_path: Path,
    *,
    approved_by: str,
    approved_commit: str,
    approved_at: datetime | None = None,
) -> PolicyApprovalV1:
    """Create the schema-defined approval for a digest-verified manifest."""
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("approved_by must identify a human approver")
    if not isinstance(approved_commit, str) or _SHA1.fullmatch(approved_commit) is None:
        raise ValueError("approved_commit must be a full lowercase Git SHA")
    manifest = PolicyManifestV1.model_validate(_read_json(Path(manifest_path)))
    digest = str(canonical_sha256(manifest.policy_content.model_dump(mode="python")))
    if digest != manifest.manifest_digest:
        raise ValueError("policy content digest mismatch")
    approval = PolicyApprovalV1(
        schema="PHASE9_POLICY_APPROVAL_V1",
        manifest_version=manifest.manifest_version,
        manifest_digest=digest,
        approval_status=PolicyApprovalStatusV1.APPROVED,
        approved_at=approved_at or datetime.now(timezone.utc),
        approved_by=approved_by.strip(),
        approved_commit=approved_commit,
    )
    target = Path(output_path)
    serialized = json.dumps(
        approval.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False,
    ) + "\n"
    # Exclusive creation prevents clobbering a human-owned approval artifact.
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
    return approval


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant-phase9-approve-policy")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approved-commit", required=True)
    args = parser.parse_args(argv)
    approval = create_approval_artifact(
        args.manifest, args.output,
        approved_by=args.approved_by,
        approved_commit=args.approved_commit,
    )
    manifest = PolicyManifestV1.model_validate(_read_json(args.manifest))
    print(json.dumps({
        "approval_path": str(args.output),
        "manifest_version": manifest.manifest_version,
        "manifest_digest": approval.manifest_digest,
        "enabled_patterns": [p.pattern_type for p in manifest.policy_content.enabled_patterns],
        "approved_by": approval.approved_by,
        "approved_commit": approval.approved_commit,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
