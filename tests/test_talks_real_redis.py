"""Runs against a real Redis when REDIS_URL is set (CI provides one); skipped otherwise."""
import os

import pytest

from sysdesign.ext.authn import MemorySessionStore, RedisSessionStore

URL = os.environ.get("REDIS_URL")
pytestmark = pytest.mark.skipif(not URL, reason="REDIS_URL not set")


def test_session_survives_process_restart_on_real_redis():
    import redis

    sid = RedisSessionStore(redis.Redis.from_url(URL)).create("agent-real", ["support"])
    restarted = RedisSessionStore(redis.Redis.from_url(URL))  # brand-new client = new process
    assert restarted.get(sid)["user"] == "agent-real"
    mem = MemorySessionStore()
    msid = mem.create("agent-real", ["support"])
    assert MemorySessionStore().get(msid) is None  # memory store loses it on restart
