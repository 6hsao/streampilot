"""阿里云内容安全适配器（ACS3-HMAC-SHA256 签名）。

签名与请求体构造按阿里云 OpenAPI 规范实现，并已用 staging 凭据对
内容审核增强版（2022-03-02）``TextModeration`` 做过真实调用核验。
"""

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import quote, urlsplit

import httpx

from streampilot.safety.cloud import (
    CloudVerdict,
    Direction,
    ModerationUnavailableError,
    Status,
)

DEFAULT_ENDPOINT = "green-cip.cn-hangzhou.aliyuncs.com"
ACTION = "TextModeration"
API_VERSION = "2022-03-02"
DEFAULT_SERVICE = "comment_detection"
_PASS_LABEL = "nonLabel"


@dataclass(frozen=True)
class SignedRequest:
    """签名后的 HTTP 请求。

    Attributes:
        url: 完整请求地址。
        headers: 请求头，含 Authorization。
        body: 原始请求体字节。
    """

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


def _hash_hex(data: bytes) -> str:
    """十六进制 SHA-256。"""
    return hashlib.sha256(data).hexdigest()


def _percent_encode(value: str) -> str:
    """按 RFC 3986 对路径与查询参数编码。"""
    return quote(value, safe="-_.~")


def build_canonical_request(
    *,
    method: str,
    path: str,
    query: str,
    headers: dict[str, str],
    signed_headers: str,
    payload_hash: str,
) -> str:
    """构造 ACS3 规范请求串。

    Args:
        method: HTTP 方法。
        path: 规范路径。
        query: 规范查询串。
        headers: 小写键、已排序的规范请求头。
        signed_headers: 分号分隔的签名头列表。
        payload_hash: 请求体 SHA-256。

    Returns:
        规范请求串。
    """
    canonical_headers = "".join(
        f"{name}:{headers[name]}\n" for name in sorted(headers)
    )
    return "\n".join(
        [
            method.upper(),
            path,
            query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )


def sign_request(
    *,
    method: str,
    url: str,
    body: bytes,
    access_key_id: str,
    access_key_secret: str,
    action: str = ACTION,
    api_version: str = API_VERSION,
    timestamp: str,
    nonce: str,
) -> SignedRequest:
    """按 ACS3-HMAC-SHA256 生成签名请求。

    Args:
        method: HTTP 方法。
        url: 目标地址。
        body: 请求体。
        access_key_id: AccessKey ID。
        access_key_secret: AccessKey Secret，仅用于本地计算签名。
        action: OpenAPI Action。
        api_version: OpenAPI 版本。
        timestamp: ISO-8601 UTC 时间。
        nonce: 一次性随机串。

    Returns:
        可直接发送的签名请求。
    """
    parts = urlsplit(url)
    path = parts.path or "/"
    query = "&".join(
        sorted(
            f"{_percent_encode(key)}={_percent_encode(value)}"
            for key, value in (
                item.split("=", 1) for item in parts.query.split("&") if item
            )
        )
    )
    payload_hash = _hash_hex(body)
    raw_headers = {
        "content-type": "application/json",
        "host": parts.netloc,
        "x-acs-action": action,
        "x-acs-content-sha256": payload_hash,
        "x-acs-date": timestamp,
        "x-acs-signature-nonce": nonce,
        "x-acs-version": api_version,
    }
    signed_headers = ";".join(sorted(raw_headers))
    canonical = build_canonical_request(
        method=method,
        path=path,
        query=query,
        headers=raw_headers,
        signed_headers=signed_headers,
        payload_hash=payload_hash,
    )
    string_to_sign = "ACS3-HMAC-SHA256\n" + _hash_hex(
        canonical.encode("utf-8")
    )
    signature = hmac.new(
        access_key_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = dict(raw_headers)
    headers["Authorization"] = (
        "ACS3-HMAC-SHA256 "
        f"Credential={access_key_id},"
        f"SignedHeaders={signed_headers},"
        f"Signature={signature}"
    )
    return SignedRequest(url=url, headers=headers, body=body)


def parse_verdict(
    payload: dict[str, Any],
    *,
    provider: str,
    latency_ms: int,
) -> CloudVerdict:
    """把阿里云响应解析为统一结论。

    增强版（2022-03-02）实际返回 ``Data.labels``（小写、逗号分隔），
    兼容旧版的 ``Data.Labels`` 与 ``Data.Result[].Label`` 形状。

    Args:
        payload: 响应 JSON。
        provider: 供应商名称。
        latency_ms: 调用耗时。

    Returns:
        审核结论。

    Raises:
        ModerationUnavailableError: 业务码非 200 或响应结构不符合预期。
    """
    code = payload.get("Code")
    if code is not None and str(code) != "200":
        raise ModerationUnavailableError(f"moderation returned code {code}")
    data = payload.get("Data")
    if not isinstance(data, dict):
        raise ModerationUnavailableError("unexpected moderation response")
    labels: list[str] = []
    raw_labels = data.get("labels", data.get("Labels"))
    if isinstance(raw_labels, str):
        labels.extend(
            item.strip() for item in raw_labels.split(",") if item.strip()
        )
    elif isinstance(raw_labels, list):
        labels.extend(
            str(item).strip() for item in raw_labels if str(item).strip()
        )
    raw_results = data.get("Result")
    if isinstance(raw_results, list):
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            label = item.get("Label")
            if isinstance(label, str) and label.strip():
                labels.append(label.strip())
    risk = tuple(
        label for label in dict.fromkeys(labels) if label != _PASS_LABEL
    )
    status: Status = "block" if risk else "pass"
    return CloudVerdict(
        status=status, labels=risk, provider=provider, latency_ms=latency_ms
    )


class AliyunGreenModerator:
    """阿里云内容安全文本审核适配器。

    Args:
        access_key_id: AccessKey ID。
        access_key_secret: AccessKey Secret。
        endpoint: 服务接入地址，不含协议。
        service: 审核场景，默认评论检测。
        timeout_s: 单次调用超时，默认 300ms 预算。
        client: 可选外部 HTTP 客户端。
        clock: 时间源，便于测试注入。
        nonce_factory: 随机串生成器。

    Raises:
        ValueError: 凭据缺失。
    """

    name = "aliyun_green"

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        service: str = DEFAULT_SERVICE,
        timeout_s: float = 0.3,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        if not access_key_id.strip() or not access_key_secret.strip():
            raise ValueError("aliyun access key is required")
        self._access_key_id = access_key_id.strip()
        self._access_key_secret = access_key_secret.strip()
        self._url = f"https://{endpoint.rstrip('/')}/"
        self._service = service
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_s))

    def close(self) -> None:
        """关闭内部创建的 HTTP 客户端。"""
        if self._owns_client:
            self._client.close()

    def _body(self, text: str) -> bytes:
        parameters = json.dumps({"content": text}, ensure_ascii=False)
        return json.dumps(
            {"Service": self._service, "ServiceParameters": parameters},
            ensure_ascii=False,
        ).encode("utf-8")

    def moderate(self, text: str, direction: Direction) -> CloudVerdict:
        """调用内容安全文本审核。"""
        body = self._body(text)
        timestamp = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())
        )
        signed = sign_request(
            method="POST",
            url=self._url,
            body=body,
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            timestamp=timestamp,
            nonce=self._nonce_factory(),
        )
        started = time.monotonic()
        try:
            response = self._client.post(
                signed.url, content=signed.body, headers=signed.headers
            )
        except httpx.HTTPError as exc:
            error = ModerationUnavailableError("moderation request failed")
            raise error from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code != 200:
            raise ModerationUnavailableError(
                f"moderation returned {response.status_code}"
            )
        try:
            payload = cast(dict[str, Any], response.json())
        except ValueError as exc:
            raise ModerationUnavailableError(
                "moderation returned non-json body"
            ) from exc
        return parse_verdict(
            payload, provider=self.name, latency_ms=latency_ms
        )
