"""Win16 icon resource decoding (GROUP_ICON + ICON)."""
import pytest

from ppython import runtime
from win16.icon import decode_icon, group_icon_entries, load_named_icon

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="game assets not present")


@pytest.fixture(scope="module")
def exe():
    return runtime.load_exe()


def test_group_icon_directory(exe):
    grp = exe.lookup_resource("GROUP_ICON", "myi_python")
    assert grp is not None
    entries = group_icon_entries(grp.data)
    assert entries == [(32, 32, 4, 2)]          # one 32x32 4bpp image, icon id 2


def test_decode_named_icon(exe):
    w, h, rgba = load_named_icon(exe, "myi_python")
    assert (w, h) == (32, 32)
    assert len(rgba) == 32 * 32 * 4
    # this icon is fully opaque (its AND mask is all zero) and not all one colour
    alphas = rgba[3::4]
    assert all(a == 255 for a in alphas)
    rgb_values = {bytes(rgba[i:i + 3]) for i in range(0, len(rgba), 4)}
    assert len(rgb_values) > 1                  # real image, not a flat fill


def test_both_icons_decode(exe):
    for res in exe.find_resources("ICON"):
        w, h, rgba = decode_icon(res.data)
        assert w > 0 and h > 0 and len(rgba) == w * h * 4
