from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import ImageOptions, Modality, SearchRequest
from .policy import ProviderPolicy
from .routing_policy import DEFAULT_ROUTING, RoutingRules, RoutingVersion

_MAX_POLICY_BYTES = 32 * 1024
ProviderName = Literal["ddgs", "searxng", "openalex", "crossref", "arxiv", "github", "wikipedia"]


class _PolicyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=False) for key, _ in node.value]
        if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("部署策略只允许不重复的字符串键")
        return super().construct_mapping(node, deep=deep)


class ProviderDefaults(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = Field(default=True, strict=True)
    priority: int = Field(default=0, strict=True, ge=-100, le=100)
    requests_per_minute: int = Field(default=120, strict=True, ge=1, le=120)


class DeploymentPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    routing: RoutingRules = RoutingRules()
    providers: dict[ProviderName, ProviderDefaults] = Field(default_factory=dict)
    image_options: ImageOptions | None = None

    def baseline(self) -> RoutingVersion:
        if self.routing == DEFAULT_ROUTING.policy:
            return DEFAULT_ROUTING
        return DEFAULT_ROUTING.model_copy(update={"policy": self.routing, "reason": "部署配置基线"})

    def provider(self, name: str) -> ProviderPolicy:
        values = self.providers.get(name)
        overrides = values.model_dump(exclude_unset=True) if values else {}
        if name == "wikipedia":
            overrides.setdefault("requests_per_minute", 10)
        return ProviderPolicy(**overrides)

    def apply_image_options(self, request: SearchRequest) -> SearchRequest:
        if self.image_options is None or Modality.IMAGE not in request.modalities:
            return request
        values = self.image_options.model_dump(exclude_unset=True)
        if request.image_options is not None:
            values.update(request.image_options.model_dump(exclude_unset=True))
        return request.model_copy(update={"image_options": ImageOptions.model_validate(values)})


def load_deployment_policy(path: str) -> DeploymentPolicy:
    if not path:
        return DeploymentPolicy()
    with Path(path).open("rb") as stream:
        payload = stream.read(_MAX_POLICY_BYTES + 1)
    if len(payload) > _MAX_POLICY_BYTES:
        raise ValueError("部署策略超过 32 KiB")
    try:
        if any(
            isinstance(token, (yaml.AliasToken, yaml.AnchorToken)) for token in yaml.scan(payload)
        ):
            raise ValueError("部署策略不使用 YAML 锚点或别名")
        values = yaml.load(payload, Loader=_PolicyLoader)
        return DeploymentPolicy.model_validate({} if values is None else values)
    except (yaml.YAMLError, ValidationError, ValueError, RecursionError):
        raise ValueError("部署策略 YAML 或字段无效") from None
