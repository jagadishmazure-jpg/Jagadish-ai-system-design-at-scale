import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location("cb", pathlib.Path(__file__).parent.parent / "scripts/check_brands.py")
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)


def test_blocklist_is_not_empty():
    # an empty blocklist silently checks nothing (a past failure in this portfolio)
    assert len(cb.TERM_HASHES) > 50 and len(cb.SPELLING_HASHES) > 50


def test_detects_source_talk_brands_built_at_runtime():
    for name in ("Blip" + "card", "Flip" + "kart", "Che" + "vy", "Ama" + "zon"):
        assert cb.find_hits(f"a chatbot for {name} users") == [name], name


def test_fictional_brand_passes():
    assert cb.find_hits("Kestrel Goods support agent") == []
