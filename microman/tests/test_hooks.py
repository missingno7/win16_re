"""The microman lifted islands (microman/hooks.py) — the A/B oracle gate.

Two machines run the same deterministic headless boot side by side: one pure
ASM, one with the WAP fill/copy islands hooked.  At every checkpoint the
window pixels must be IDENTICAL — the hook's value is byte-exact speed, and
this gate is what makes it a recovery instead of an approximation.
"""
import hashlib

import pytest

from microman import hooks, runtime

pytestmark = pytest.mark.skipif(not runtime.assets_present(),
                                reason="microman assets not present")

# 20 batches reaches the first WAP page-transition animation (~batch 13+),
# where the islands fire tens of thousands of times — the boot window alone
# never enters these loops.
BATCHES = 20
BATCH_STEPS = 500


def _drive(hooked: bool):
    machine = runtime.create_machine()
    machine.cpu.trace_enabled = False
    fires = {}
    if hooked:
        assert runtime.install_hooks(machine) == 19
        # Count each island FAMILY's firings so the test proves every shape is
        # actually exercised (not just installed).
        cs = machine.seg_bases[__import__("microman").hooks.CODE_SEG_INDEX]
        for (hcs, ip), name in list(machine.cpu.hook_names.items()):
            if hcs != cs:
                continue
            family = name.split("@")[0]
            orig = machine.cpu.replacement_hooks[(hcs, ip)]

            def wrap(cpu, _orig=orig, _fam=family):
                fires[_fam] = fires.get(_fam, 0) + 1
                return _orig(cpu)

            machine.cpu.replacement_hooks[(hcs, ip)] = wrap
    sysobj = machine.api.services["system"]
    hashes = []
    for _ in range(BATCHES):
        machine.cpu.run(BATCH_STEPS)
        win = next((w for w in sysobj.windows
                    if w.wndclass.name == "MicroManClass"), None)
        pixels = bytes(win.surface.pixels) if win is not None else b""
        hashes.append(hashlib.sha256(pixels).hexdigest())
    return machine, hashes, fires


def test_islands_are_pixel_exact_and_engaged():
    plain, plain_hashes, _ = _drive(hooked=False)
    hooked, hooked_hashes, fires = _drive(hooked=True)

    # Byte-exact rendering at every checkpoint.
    assert hooked_hashes == plain_hashes

    # The islands actually engaged: a hook consumes ONE instruction where the
    # ASM loops consumed dozens per byte, so the hooked run must reach the
    # same checkpoints in materially fewer instructions.
    assert hooked.cpu.instruction_count < plain.cpu.instruction_count * 0.9, (
        f"hooks never engaged: {hooked.cpu.instruction_count} vs "
        f"{plain.cpu.instruction_count}")

    # Every island FAMILY fired at least once in the title window (so the
    # byte-exact comparison above actually covers each shape).
    for family in ("wap_fill_asc", "wap_fill_desc", "wap_huge_copy",
                   "wap_byte_copy", "wap_byte_fill"):
        assert fires.get(family, 0) > 0, f"{family} never fired"


def test_install_refuses_wrong_code():
    """A binary whose code segment lacks the WAP loop bodies must be refused
    (an island landing on different code corrupts silently)."""
    machine = runtime.create_machine()
    cs = machine.seg_bases[hooks.CODE_SEG_INDEX]
    machine.mem.data[cs << 4:(cs << 4) + 0x10000] = bytes(0x10000)
    with pytest.raises(AssertionError):
        hooks.install(machine)
