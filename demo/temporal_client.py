"""One place to connect to Temporal: local dev server, self-hosted, or Temporal Cloud.

    Local dev server   TEMPORAL_ADDRESS=localhost:7233 (default)
    Cloud, API key     TEMPORAL_ADDRESS=<ns>.<acct>.tmprl.cloud:7233  TEMPORAL_API_KEY=...
                       (or the regional endpoint, e.g. us-east-1.aws.api.temporal.io:7233)
    Cloud or self-hosted with mTLS
                       TEMPORAL_TLS_CERT=client.pem TEMPORAL_TLS_KEY=client.key [TEMPORAL_TLS_CA=ca.pem]
"""

from pathlib import Path

from temporalio.client import Client, TLSConfig

from . import settings


async def connect(identity: str = "") -> Client:
    kwargs = {"namespace": settings.TEMPORAL_NAMESPACE}
    if identity:
        kwargs["identity"] = identity
    if settings.TEMPORAL_TLS_CERT and settings.TEMPORAL_TLS_KEY:
        kwargs["tls"] = TLSConfig(
            client_cert=Path(settings.TEMPORAL_TLS_CERT).read_bytes(),
            client_private_key=Path(settings.TEMPORAL_TLS_KEY).read_bytes(),
            server_root_ca_cert=Path(settings.TEMPORAL_TLS_CA).read_bytes() if settings.TEMPORAL_TLS_CA else None,
        )
    elif settings.TEMPORAL_API_KEY:
        kwargs["api_key"] = settings.TEMPORAL_API_KEY
        kwargs["tls"] = True
    return await Client.connect(settings.TEMPORAL_ADDRESS, **kwargs)
