"""The Shared Crew enterprise seam: one slot an edition fills, disabled by default.

``PlatformContext.shared_crew_enterprise`` carries the edition's providers for
human credential verification, authoritative individual grants, workload
identity, the synthetic Crew identity, the outbound heartbeat transport, the
inference budget, human sign-in, the revocation feed, the workload verifier,
Crew registration, the Crew directory and the channel actor mapping. An edition
written before an accessor existed may omit it; that reads as not configured.
The public build composes :class:`DefaultSharedCrewEnterpriseProvider`, which
answers ``enabled() -> False`` and ``None`` for every provider, so every
enterprise capability reports unavailable and nothing reaches the network.

The accessors return ``object | None`` on purpose. The contracts each provider
must satisfy live in ``kiro_crew.shared_crew.enterprise``, which checks every
returned object against its protocol before anything uses it; this module stays
import-free so the platform package does not depend on Shared Crew.

A provider holds its own credentials and key material. Nothing here reads
``config.json``: an agent-writable setting must never be able to enable an
enterprise authority or name a key.
"""

from __future__ import annotations

from typing import Mapping, Optional, Protocol


class SharedCrewEnterpriseProvider(Protocol):
    """The edition's Shared Crew enterprise providers (public default: none)."""

    def enabled(self) -> bool:
        """True only when the edition supplies at least one real provider."""
        ...

    def credential_verifier(self) -> Optional[object]:
        """A ``shared_crew.enterprise.CredentialVerifier`` or ``None``."""
        ...

    def grant_authority(self) -> Optional[object]:
        """A ``shared_crew.enterprise.GrantAuthority`` or ``None``."""
        ...

    def workload_identity(self) -> Optional[object]:
        """A ``shared_crew.enterprise.WorkloadIdentityProvider`` or ``None``."""
        ...

    def crew_binding(self) -> Optional[object]:
        """A ``shared_crew.enterprise.CrewBindingProvider`` or ``None``."""
        ...

    def heartbeat_transport(self) -> Optional[object]:
        """A ``shared_crew.enterprise.HeartbeatTransport`` or ``None``."""
        ...

    def inference_budget(self) -> Optional[object]:
        """A ``shared_crew.enterprise.InferenceBudgetProvider`` or ``None``."""
        ...

    def deployment_context(self) -> Mapping[str, str]:
        """Opaque tenant / deployment / resource-policy references (default: empty)."""
        ...

    def sign_in(self) -> Optional[object]:
        """A ``shared_crew.enterprise_signin.RelyingParty`` or ``None``."""
        ...

    def revocation_feed(self) -> Optional[object]:
        """A ``shared_crew.enterprise_revocation.RevocationFeed`` or ``None``."""
        ...

    def workload_verifier(self) -> Optional[object]:
        """A ``shared_crew.enterprise_workload.WorkloadVerifierConfig`` or ``None``."""
        ...

    def crew_registry(self) -> Optional[object]:
        """A ``shared_crew.enterprise_registration.CrewRegistry`` or ``None``."""
        ...

    def directory(self) -> Optional[object]:
        """A ``shared_crew.enterprise_directory.DirectoryProvider`` or ``None``."""
        ...

    def channel_actors(self) -> Optional[object]:
        """A ``shared_crew.enterprise_signin.ChannelActorMapping`` or ``None``."""
        ...


class DefaultSharedCrewEnterpriseProvider:
    """Disabled seam: no providers, no references, no network."""

    def enabled(self) -> bool:
        return False

    def credential_verifier(self) -> Optional[object]:
        return None

    def grant_authority(self) -> Optional[object]:
        return None

    def workload_identity(self) -> Optional[object]:
        return None

    def crew_binding(self) -> Optional[object]:
        return None

    def heartbeat_transport(self) -> Optional[object]:
        return None

    def inference_budget(self) -> Optional[object]:
        return None

    def deployment_context(self) -> Mapping[str, str]:
        return {}

    def sign_in(self) -> Optional[object]:
        return None

    def revocation_feed(self) -> Optional[object]:
        return None

    def workload_verifier(self) -> Optional[object]:
        return None

    def crew_registry(self) -> Optional[object]:
        return None

    def directory(self) -> Optional[object]:
        return None

    def channel_actors(self) -> Optional[object]:
        return None
