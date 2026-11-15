"""接入网关服务：评论接入 → 验签 → 限流 → 幂等 → Redis Stream。"""

import os

from fastapi import FastAPI
from redis import Redis

from streampilot.ingest.app import IngestSettings, create_app
from streampilot.ingest.signing import RedisNonceStore
from streampilot.ingest.stores import (
    RedisEventPublisher,
    RedisIdempotencyStore,
    RedisRateLimiter,
)


def build_app() -> FastAPI:
    """按环境变量装配生产用依赖。

    Returns:
        可直接由 uvicorn 启动的应用。
    """
    settings = IngestSettings.from_env()
    client = Redis.from_url(os.environ["REDIS_URL"])
    return create_app(
        settings,
        nonce_store=RedisNonceStore(client),
        idempotency=RedisIdempotencyStore(client),
        rate_limiter=RedisRateLimiter(client, settings.rate_limit_per_second),
        publisher=RedisEventPublisher(client),
    )


app = build_app()
