"""模型网关基础设施适配器。"""

from .bifrost import BifrostClientError, BifrostHttpClient
from .bridge import BailianEgressBridge, ModelEgressBridgeError
from .composition import (
    ModelGatewayWiring,
    SqlAlchemyModelGatewayTransaction,
    compose_model_gateway_service,
)
from .egress_permit_issuer import (
    OpenBaoMetadataVersionResolver,
    PostgresEgressPermitIssuer,
    ProviderSecretVersionResolver,
)
from .openbao import (
    AuthorizedOpenBaoSecretBroker,
    OpenBaoAgentFileTokenProvider,
    OpenBaoKv2Client,
    OpenBaoSecretError,
    OpenBaoTokenProvider,
)

__all__ = [
    "BifrostClientError",
    "BifrostHttpClient",
    "BailianEgressBridge",
    "ModelEgressBridgeError",
    "AuthorizedOpenBaoSecretBroker",
    "OpenBaoAgentFileTokenProvider",
    "OpenBaoKv2Client",
    "OpenBaoSecretError",
    "OpenBaoTokenProvider",
    "ModelGatewayWiring",
    "SqlAlchemyModelGatewayTransaction",
    "compose_model_gateway_service",
    "OpenBaoMetadataVersionResolver",
    "PostgresEgressPermitIssuer",
    "ProviderSecretVersionResolver",
]
