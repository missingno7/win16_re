"""AddFontResource's pair: a program that installs fonts also removes them.

The gap this closes: GDI.136 was a tripwire, so a program calling it on the
way out died with a Win16ApiGap at exit — after the app had already torn its
windows down and there was nothing left to salvage.
"""
from win16.api import gdi
from win16.api.core import ApiRegistry
from win16.api.ordinals import api_name


def _registry() -> ApiRegistry:
    api = ApiRegistry()
    gdi.install(api)
    return api


def test_remove_font_resource_is_implemented():
    api = _registry()
    assert ("GDI", 136) in api.entries, "GDI.136 must not be a tripwire"


def test_remove_font_resource_reports_removed():
    api = _registry()
    entry = api.entries[("GDI", 136)]
    assert entry.handler(None) == 1        # TRUE: the caller's cleanup completes


def test_add_and_remove_font_resource_are_a_pair():
    api = _registry()
    for ordinal in (119, 136):             # Add / Remove
        assert ("GDI", ordinal) in api.entries


def test_ordinal_136_is_named_for_reports():
    # apicoverage/IR tags resolve names from this table; an unnamed ordinal
    # reads as a mystery in every report it appears in.
    assert api_name("GDI", 136) == "GDI.136:RemoveFontResource"
