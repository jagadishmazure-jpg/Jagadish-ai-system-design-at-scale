"""Talk 17 add-on to backend-evolution step 4: where session state lives. Seeded replay."""
import fakeredis
import pytest

from sysdesign.app import build_app
from sysdesign.ext import stateless as sl
from sysdesign.tools import Commerce, NotFound, RedisJSONMap

R = sl.results()
PLAIN = {x["setup"]: x for x in R["plain"]}
DIES = {x["setup"]: x for x in R["replica_dies"]}
HEAVY = {x["setup"]: x for x in R["heavy_user"]}


def test_memory_only_logs_people_out():
    assert PLAIN["memory"]["logouts"] > 50 and PLAIN["memory"]["lost_cart_items"] > 0


def test_memory_only_loses_form_sections():
    assert PLAIN["memory"]["lost_form_sections"] > 0


def test_sticky_works_while_nothing_fails():
    assert PLAIN["sticky"]["logouts"] == 0 and PLAIN["sticky"]["lost_cart_items"] == 0


def test_shared_store_loses_nothing():
    assert PLAIN["shared"]["logouts"] == PLAIN["shared"]["lost_cart_items"] == PLAIN["shared"]["lost_form_sections"] == 0


def test_sticky_loses_state_when_replica_dies():
    assert DIES["sticky"]["logouts"] > 0 and DIES["sticky"]["lost_cart_items"] > 0


def test_shared_store_survives_replica_death():
    d = DIES["shared"]
    assert d["logouts"] == d["lost_cart_items"] == d["lost_form_sections"] == 0


def test_sticky_piles_heavy_user_on_one_replica():
    assert HEAVY["sticky"]["max_over_avg"] > 1.5


def test_round_robin_spreads_heavy_user():
    assert HEAVY["shared"]["max_over_avg"] == pytest.approx(1.0, abs=0.02)


def test_counter_memory_splits_across_replicas():
    m = sl.counter_demo("memory")
    assert m["final"] == 3 and m["per_replica"] == [3, 3, 3]


def test_counter_shared_counts_nine():
    assert sl.counter_demo("shared")["final"] == 9


def test_sticky_repins_when_pinned_dies():
    bal = sl.build("sticky")
    first = bal.pick("u001")
    first.alive = False
    assert bal.pick("u001") is not first


def test_redis_session_store_round_trip():
    s = sl.RedisSessionStore(fakeredis.FakeRedis())
    s.put("u", {"cart": ["a"]})
    assert s.get("u") == {"cart": ["a"]} and s.get("missing") is None


# ---------------------------------------------------------------- real fix: carts in Redis
def test_two_replicas_see_the_same_cart():
    r = fakeredis.FakeRedis()
    a, b = Commerce(r), Commerce(r)
    a.add_to_cart("u-ava", "SKU-KETTLE")
    b.add_to_cart("u-ava", "SKU-KETTLE", 2)
    assert a.cart("u-ava") == b.cart("u-ava") == {"SKU-KETTLE": 3}


def test_two_app_replicas_share_commerce():
    r = fakeredis.FakeRedis()
    a, b = build_app(redis=r), build_app(redis=r)
    a.state.agent.commerce.add_to_cart("u-ben", "SKU-HEADPHONES")
    assert b.state.agent.commerce.cart("u-ben") == {"SKU-HEADPHONES": 1}


def test_refund_on_one_replica_is_idempotent_on_another():
    r = fakeredis.FakeRedis()
    a, b = Commerce(r), Commerce(r)
    x = a.issue_refund("KG-100002", "u-ava", 5.0, idempotency_key="idem-share-0001")
    y = b.issue_refund("KG-100002", "u-ava", 5.0, idempotency_key="idem-share-0001")
    assert x == y and len(b.refunds) == 1


def test_refund_ids_unique_across_replicas():
    r = fakeredis.FakeRedis()
    a, b = Commerce(r), Commerce(r)
    x = a.issue_refund("KG-100002", "u-ava", 1.0, idempotency_key="idem-uniq-0001")
    y = b.issue_refund("KG-100002", "u-ava", 1.0, idempotency_key="idem-uniq-0002")
    assert x["refund_id"] != y["refund_id"]


def test_seed_does_not_overwrite_existing_orders():
    r = fakeredis.FakeRedis()
    a = Commerce(r)
    o = a.orders["KG-100004"]
    o["status"] = "shipped"
    a.orders["KG-100004"] = o
    assert Commerce(r).orders["KG-100004"]["status"] == "shipped"


def test_cart_rejects_bad_qty_and_unknown_sku():
    c = Commerce(fakeredis.FakeRedis())
    with pytest.raises(ValueError):
        c.add_to_cart("u-ava", "SKU-KETTLE", -1)
    with pytest.raises(NotFound):
        c.add_to_cart("u-ava", "SKU-NOPE")


def test_in_process_commerce_still_works():
    c = Commerce()
    c.add_to_cart("u-ava", "SKU-KETTLE")
    assert c.cart("u-ava") == {"SKU-KETTLE": 1} and isinstance(c.orders, dict)


def test_redis_map_behaves_like_dict():
    m = RedisJSONMap(fakeredis.FakeRedis(), "t:m")
    m["a"] = {"x": 1}
    assert "a" in m and len(m) == 1 and list(m) == ["a"] and m.get("b") is None
    del m["a"]
    assert m == {}
