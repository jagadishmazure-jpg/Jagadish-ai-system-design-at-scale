import fakeredis
import pytest

from sysdesign.auth import Identity, LocalIssuer
from sysdesign.clock import FakeClock


@pytest.fixture
def r():
    return fakeredis.FakeRedis()


@pytest.fixture
def clock():
    return FakeClock(1_800_000_000.0)


@pytest.fixture(scope="session")
def issuer():
    return LocalIssuer()


@pytest.fixture
def ava():
    return Identity("u-ava", "retail", "standard", frozenset({"chat"}))
