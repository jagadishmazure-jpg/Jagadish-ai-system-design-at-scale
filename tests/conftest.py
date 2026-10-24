import asyncio
import copy

import httpx
import pytest

from sysdesign import tools
from sysdesign.app import create_app
from sysdesign.build import build
from sysdesign.gateway import LocalIssuer

_ORDERS = copy.deepcopy(tools.ORDERS)
ANA = {"sub": "u-ana", "tenant": "quillmoor", "scope": "chat orders:read", "tier": "standard"}
BEN = {"sub": "u-ben", "tenant": "quillmoor", "scope": "chat orders:read", "tier": "standard"}


@pytest.fixture(autouse=True)
def reset_orders():
    tools.ORDERS.clear()
    tools.ORDERS.update(copy.deepcopy(_ORDERS))
    yield


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def system():
    return build()


@pytest.fixture
def issuer():
    return LocalIssuer()


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=10)


@pytest.fixture
def app(system, issuer):
    return create_app(system, issuer)
