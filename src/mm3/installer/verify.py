"""The one adapter for ``sigstore`` and ``pypi-attestations`` (CORE-349).

Two checks, both made by the installer itself and never trusted from the
engine (CORE-131):

- the index signature: the Sigstore bundle ``index.json.sigstore.json`` must
  verify for the index bytes with the fixed identity of the module-index
  publish workflow on ``refs/heads/main`` and the GitHub Actions issuer
  (CORE-350);
- a release's provenance: one PEP 740 attestation from PyPI's Integrity API
  must verify for the downloaded wheel against the entry's Trusted Publisher
  (CORE-352).

The engine never imports this module (``mm3-core[installer]`` only).

Implements:
    CORE-349: Core spec 0.33, §10.6 Module catalog.
    CORE-350: Core spec 0.33, §10.6 Module catalog.
    CORE-352, CORE-123: Core spec 0.33, §10.6 Module catalog.
    -: Core spec 0.33, §10.9 Dependency management.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from mm3.core.catalog import SIGNER_IDENTITY, SIGNER_ISSUER, Publisher

__all__ = ["SigstoreVerification", "Verification", "VerifyError"]


class VerifyError(ValueError):
    """A signature or attestation did not verify; the message says why."""


class Verification(Protocol):
    """The installer's two checks (a fake in tests)."""

    def verify_index(self, index: bytes, bundle: bytes) -> None:
        """Verify the index signature.

        Raises:
            VerifyError: The bundle does not verify with the fixed identity.
        """

    def verify_wheel(self, wheel: Path, provenance: bytes, publisher: Publisher) -> None:
        """Verify a wheel's PEP 740 provenance against its Trusted Publisher.

        Raises:
            VerifyError: No attestation verifies for this wheel and publisher.
        """


class SigstoreVerification:
    """The checks with ``sigstore`` and ``pypi-attestations``.

    Args:
        offline: Use the trust root shipped with sigstore instead of refreshing
            it over TUF (for tests).
    """

    def __init__(self, *, offline: bool = False) -> None:
        self._offline = offline

    def verify_index(self, index: bytes, bundle: bytes) -> None:
        """Verify ``bundle`` for ``index`` with the module-index identity."""
        from sigstore.errors import Error as SigstoreError
        from sigstore.models import Bundle, InvalidBundle
        from sigstore.verify import Verifier, policy

        try:
            parsed = Bundle.from_json(bundle)
        except (InvalidBundle, ValueError) as exc:
            raise VerifyError(f"index bundle is not a Sigstore bundle: {exc}") from exc
        identity = policy.Identity(identity=SIGNER_IDENTITY, issuer=SIGNER_ISSUER)
        try:
            Verifier.production(offline=self._offline).verify_artifact(index, parsed, identity)
        except SigstoreError as exc:
            raise VerifyError(f"index signature does not verify: {exc}") from exc

    def verify_wheel(self, wheel: Path, provenance: bytes, publisher: Publisher) -> None:
        """Verify ``wheel`` against the provenance from PyPI's Integrity API."""
        from pydantic import ValidationError
        from pypi_attestations import (
            AttestationError,
            Distribution,
            GitHubPublisher,
            Provenance,
        )
        from sigstore.errors import Error as SigstoreError

        try:
            parsed = Provenance.model_validate_json(provenance)
        except ValidationError as exc:
            raise VerifyError(f"invalid provenance for {wheel.name}: {exc}") from exc
        try:
            dist = Distribution.from_file(wheel)
        except (OSError, ValueError) as exc:
            raise VerifyError(f"cannot read {wheel.name}: {exc}") from exc
        expected = GitHubPublisher(
            repository=publisher.repository,
            workflow=publisher.workflow,
            environment=publisher.environment,
        )
        reasons: list[str] = []
        for bundle in parsed.attestation_bundles:
            # The bundle's own publisher claim is checked before any
            # cryptography; the certificate is then checked against the
            # expected publisher, not against the claim.
            claim = bundle.publisher
            if getattr(claim, "kind", None) != "GitHub" or (
                getattr(claim, "repository", "").lower() != publisher.repository.lower()
            ):
                reasons.append(f"attestation from another publisher ({claim!r})")
                continue
            for attestation in bundle.attestations:
                try:
                    attestation.verify(expected, dist, offline=self._offline)
                except (AttestationError, SigstoreError, ValueError) as exc:
                    reasons.append(str(exc))
                    continue
                return
        detail = "; ".join(reasons) or "no attestations"
        raise VerifyError(
            f"no attestation for {wheel.name} verifies against "
            f"{publisher.repository} ({publisher.workflow}): {detail}"
        )
