"""The tripwire tier: APIs added because a real call site proved them.

Each of these had an import slot but no handler, so calling it raised
Win16ApiGap.  This pins what the callers actually depend on — the argument
SHAPE (a pascal contract read wrong corrupts the guest stack), the RETURN a
caller branches on, and the fail-loud edges.

Synthetic and game-free: the real ApiRegistry over a mock CPU, handlers reached
through their registry entries, and a stub system object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from win16.api.core import ARG_SIZES, CallContext
from win16.api.core import Win16ApiGap
from win16.api.dialogs import (Dialog, DialogControlState, LB_GETCURSEL,
                               _decode_dir_entry, _split_spec)
from win16.api.gdi import SYS_COLORS, sys_colors
from win16.api.kernel import FatalAppExitError
from win16.api.keyboard import _US_LAYOUT
from win16.api.objects import DC, Menu, Region, Surface, Window, WndClass
from win16.api.surface import build_registry
from win16.api.user import AtomTable


class _Mem:
    def __init__(self) -> None:
        self.b = bytearray(0x40000)

    def _lin(self, seg, off):
        return (seg * 16 + (off & 0xFFFF)) % len(self.b)

    def rb(self, seg, off):
        return self.b[self._lin(seg, off)]

    def wb(self, seg, off, val):
        self.b[self._lin(seg, off)] = val & 0xFF

    def rw(self, seg, off):
        return self.rb(seg, off) | (self.rb(seg, off + 1) << 8)

    def ww(self, seg, off, val):
        self.wb(seg, off, val)
        self.wb(seg, off + 1, val >> 8)

    def load(self, seg, off, data):
        for i, byte in enumerate(data):
            self.wb(seg, off + i, byte)

    def block(self, seg, off, n):
        return bytes(self.rb(seg, off + i) for i in range(n))


@dataclass
class _Cpu:
    mem: _Mem = field(default_factory=_Mem)
    s: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(ax=0, dx=0))


def _rig():
    """A registry + a stub system, wired the way the handlers reach each other."""
    api = build_registry()
    cpu = _Cpu()
    machine = SimpleNamespace(api=api, mem=cpu.mem, exe=None)
    from win16.api.objects import HandleTable
    sysobj = SimpleNamespace(machine=machine, handles=HandleTable(), windows=[],
                             file_root=None, system_palette=None, clock_ms=0)
    api.services["system"] = sysobj
    return api, cpu, sysobj


def _call(api, cpu, module, ordinal, args=()):
    entry = api.entries[(module, ordinal)]
    ctx = CallContext(cpu, api, module, ordinal, entry.name, tuple(args))
    return entry.handler(ctx)


def _contract(api, module, ordinal, args_spec, ret="word"):
    """The DECLARED pascal contract: arg byte sizes (in order) and the return."""
    entry = api.entries[(module, ordinal)]
    assert entry.arg_sizes == [ARG_SIZES[a] for a in args_spec.split()], \
        f"{module}.{ordinal} arg shape"
    assert entry.ret == ret, f"{module}.{ordinal} return"


def _put_str(cpu, seg, off, text):
    cpu.mem.load(seg, off, text.encode("latin-1") + b"\x00")
    return (seg << 16) | off


# --------------------------------------------------------------------------
# USER.473 AnsiPrev — the Load/Save dialogs' path walk
# --------------------------------------------------------------------------

def test_ansiprev_steps_back_one_and_clamps_at_the_start():
    api, cpu, _ = _rig()
    _contract(api, "USER", 473, "str segptr", ret="long")
    start = _put_str(cpu, 0x5294, 0x93A0, "A:\\SUB\\F.ANT")
    # From anywhere past the start, the previous character is one byte back.
    assert _call(api, cpu, "USER", 473, (start, 0x529493A5)) == 0x529493A4
    assert _call(api, cpu, "USER", 473, (start, 0x529493A1)) == 0x529493A0
    # AT the start it must NOT walk off the front — the caller's loop relies on
    # this to terminate even when the string holds no separator at all.
    assert _call(api, cpu, "USER", 473, (start, start)) == start
    # The segment is preserved: the caller reads the result as ES:BX.
    assert (_call(api, cpu, "USER", 473, (start, 0x529493A5)) >> 16) == 0x5294


def test_ansiprev_walks_a_path_back_to_its_last_separator():
    # The whole point of the call site: start at the NUL and step back until a
    # ':' or '\\' — the loop SimAnt's OPENDLG/_SeparateFile/_ChangeDirectory
    # all share.  Reproduced here in full.
    api, cpu, _ = _rig()
    path = "C:\\ANT\\SAVE.ANT"
    start = _put_str(cpu, 0x5294, 0x9000, path)
    cur = start + len(path)
    steps = 0
    while True:
        ch = chr(cpu.mem.rb(0x5294, cur & 0xFFFF))
        if ch in (":", "\\") or (cur & 0xFFFF) <= (start & 0xFFFF):
            break
        cur = _call(api, cpu, "USER", 473, (start, cur))
        steps += 1
    assert (cur & 0xFFFF) - (start & 0xFFFF) == path.rindex("\\")
    assert steps == len(path) - path.rindex("\\")


# --------------------------------------------------------------------------
# USER.99 DlgDirSelect — is the selection a directory?
# --------------------------------------------------------------------------

def test_decode_dir_entry_tells_files_from_directories_and_drives():
    assert _decode_dir_entry("SAVE.ANT") == ("SAVE.ANT", False)
    assert _decode_dir_entry("[SUBDIR]") == ("SUBDIR\\", True)     # ready to paste
    assert _decode_dir_entry("[-c-]") == ("c:", True)


def _dialog_with_list(sysobj, items, sel, ctrl_id=0x194, cls="ListBox"):
    ctrl = DialogControlState(ctrl_id, cls, 0, "", None)
    ctrl.items, ctrl.sel = items, sel
    dlg = Dialog("OpenDlg", None, (0, 0), 0)
    dlg.controls = [ctrl]
    dlg.by_id = {ctrl_id: ctrl}
    sysobj.handles.add(ctrl)
    sysobj.handles.add(dlg)
    return dlg


def test_dlgdirselect_copies_the_name_and_reports_file_vs_directory():
    api, cpu, sysobj = _rig()
    _contract(api, "USER", 99, "word ptr word")
    dlg = _dialog_with_list(sysobj, ["SAVE.ANT", "[SUBDIR]", "[-c-]"], 0)
    buf = (0x6000 << 16) | 0x10

    # A FILE -> 0, name verbatim.  The caller must NOT paste its spec onto it.
    assert _call(api, cpu, "USER", 99, (dlg.handle, buf, 0x194)) == 0
    assert cpu.mem.block(0x6000, 0x10, 9).split(b"\x00")[0] == b"SAVE.ANT"

    # A DIRECTORY -> non-zero, brackets stripped, trailing '\' so that the
    # caller's lstrcat of "*.ANT" builds "SUBDIR\*.ANT".
    dlg.controls[0].sel = 1
    assert _call(api, cpu, "USER", 99, (dlg.handle, buf, 0x194)) == 1
    assert cpu.mem.block(0x6000, 0x10, 9).split(b"\x00")[0] == b"SUBDIR\\"

    # A DRIVE -> non-zero, "[-c-]" -> "c:".
    dlg.controls[0].sel = 2
    assert _call(api, cpu, "USER", 99, (dlg.handle, buf, 0x194)) == 1
    assert cpu.mem.block(0x6000, 0x10, 4).split(b"\x00")[0] == b"c:"


# --------------------------------------------------------------------------
# USER.100 DlgDirList — the DDL_* filetype decides what a listing IS
# --------------------------------------------------------------------------

def _dir_rig(tmp_path):
    api, cpu, sysobj = _rig()
    (tmp_path / "SAVE.ANT").write_bytes(b"x")
    (tmp_path / "OTHER.ANT").write_bytes(b"x")
    (tmp_path / "GAME.EXE").write_bytes(b"x")
    (tmp_path / "SOUND").mkdir()
    sysobj.file_root = tmp_path
    dlg = _dialog_with_list(sysobj, [], -1)
    dlg.by_id[0x193] = DialogControlState(0x193, "Static", 0, "", None)
    dlg.controls.append(dlg.by_id[0x193])
    return api, cpu, sysobj, dlg


def _list(api, cpu, dlg, spec, filetype, static_id=0x193):
    ptr = _put_str(cpu, 0x5000, 0, spec)
    rv = _call(api, cpu, "USER", 100,
               (dlg.handle, ptr, 0x194, static_id, filetype))
    return rv, dlg.by_id[0x194].items


DDL_DIRECTORY, DDL_DRIVES, DDL_EXCLUSIVE = 0x0010, 0x4000, 0x8000


def test_dlgdirlist_exclusive_dirs_and_drives_lists_no_files(tmp_path):
    # SimAnt's SAVEASDLG asks for exactly this (0xC010) to show "where am I":
    # folders and drives ONLY.  Listing files here inverts the answer — and its
    # spec is deliberately EMPTY, which must not become "*.*" and match all.
    api, cpu, _sysobj, dlg = _dir_rig(tmp_path)
    rv, items = _list(api, cpu, dlg, "",
                      DDL_EXCLUSIVE | DDL_DRIVES | DDL_DIRECTORY)
    assert items == ["[-c-]", "[SOUND]"]
    assert not any(i.endswith(".ANT") or i.endswith(".EXE") for i in items)
    assert rv == 1


def test_dlgdirlist_non_exclusive_lists_matching_files_plus_dirs_and_drives(tmp_path):
    # OPENDLG's 0x4010: the spec's files AND the folders AND the drives.
    api, cpu, _sysobj, dlg = _dir_rig(tmp_path)
    rv, items = _list(api, cpu, dlg, "*.ANT", DDL_DRIVES | DDL_DIRECTORY)
    assert items == ["OTHER.ANT", "SAVE.ANT", "[-c-]", "[SOUND]"]
    assert rv == 1


def test_dlgdirlist_plain_files_only_when_no_class_bits_are_set(tmp_path):
    api, cpu, _sysobj, dlg = _dir_rig(tmp_path)
    _rv, items = _list(api, cpu, dlg, "*.ANT", 0)
    assert items == ["OTHER.ANT", "SAVE.ANT"]           # no dirs, no drives
    _rv, items = _list(api, cpu, dlg, "*.*", 0)
    assert items == ["GAME.EXE", "OTHER.ANT", "SAVE.ANT"]


def test_dlgdirlist_reports_no_match_and_updates_the_current_path(tmp_path):
    api, cpu, _sysobj, dlg = _dir_rig(tmp_path)
    rv, items = _list(api, cpu, dlg, "*.SAV", 0)
    assert (rv, items) == (0, [])                       # nothing matched
    assert dlg.by_id[0x193].text == "C:\\"
    # static_id 0 means "no current-path static" — must not raise.
    rv, _items = _list(api, cpu, dlg, "*.ANT", 0, static_id=0)
    assert rv == 1


def test_dlgdirlist_entries_round_trip_through_dlgdirselect(tmp_path):
    # The producer and the consumer must agree on the bracket encoding, or the
    # dialog pastes a malformed path.  This is the pair, end to end.
    api, cpu, _sysobj, dlg = _dir_rig(tmp_path)
    _rv, items = _list(api, cpu, dlg, "*.ANT", DDL_DRIVES | DDL_DIRECTORY)
    buf = (0x6000 << 16) | 0x10
    expected = {"[SOUND]": ("SOUND\\", 1), "[-c-]": ("c:", 1),
                "SAVE.ANT": ("SAVE.ANT", 0)}
    for entry, (fragment, is_dir) in expected.items():
        dlg.by_id[0x194].sel = items.index(entry)
        assert _call(api, cpu, "USER", 99, (dlg.handle, buf, 0x194)) == is_dir
        got = cpu.mem.block(0x6000, 0x10, 16).split(b"\x00")[0]
        assert got == fragment.encode("latin-1")


def test_dlgdirlist_leaves_the_box_unselected(tmp_path):
    # LB_GETCURSEL must answer LB_ERR until a user clicks: SimAnt's SAVEASDLG
    # reads it on OK to ask "did they pick a directory?"
    api, cpu, _sysobj, dlg = _dir_rig(tmp_path)
    _list(api, cpu, dlg, "*.ANT", 0)
    assert dlg.by_id[0x194].sel == -1
    assert _call(api, cpu, "USER", 101,
                 (dlg.handle, 0x194, LB_GETCURSEL, 0, 0)) == 0xFFFFFFFF


def test_dlgdirlist_fails_loud_on_a_path_it_cannot_serve(tmp_path):
    # The file model is flat (system._canonical keeps only the last component),
    # so descending is impossible.  An empty list would say "that folder is
    # empty" — indistinguishable from the truth, and the worst answer.
    api, cpu, _sysobj, dlg = _dir_rig(tmp_path)
    for spec in ("SOUND\\*.ANT", "C:\\ANT\\*.ANT", "D:*.ANT"):
        with pytest.raises(Win16ApiGap):
            _list(api, cpu, dlg, spec, 0)
    # ...but the root, however it is spelled, IS servable.
    for spec in ("*.ANT", "C:*.ANT", "C:\\*.ANT", "\\*.ANT"):
        rv, items = _list(api, cpu, dlg, spec, 0)
        assert (rv, items) == (1, ["OTHER.ANT", "SAVE.ANT"]), spec


def test_split_spec_separates_the_directory_from_the_file_part():
    assert _split_spec("*.SAV") == ("", "*.SAV")
    assert _split_spec("C:\\ANT\\*.SAV") == ("C:\\ANT", "*.SAV")
    assert _split_spec("SUB\\*.SAV") == ("SUB", "*.SAV")
    assert _split_spec("C:*.SAV") == ("C:", "*.SAV")
    assert _split_spec("\\*.SAV") == ("", "*.SAV")
    assert _split_spec("SAVE.ANT") == ("", "SAVE.ANT")


def test_dlgdirselect_falls_back_to_the_control_text_with_no_selection():
    api, cpu, sysobj = _rig()
    dlg = _dialog_with_list(sysobj, [], -1)
    dlg.controls[0].text = "TYPED.ANT"
    buf = (0x6000 << 16) | 0x10
    assert _call(api, cpu, "USER", 99, (dlg.handle, buf, 0x194)) == 0
    assert cpu.mem.block(0x6000, 0x10, 10).split(b"\x00")[0] == b"TYPED.ANT"


# --------------------------------------------------------------------------
# USER.76 PtInRect / USER.36 GetWindowText
# --------------------------------------------------------------------------

def test_ptinrect_is_half_open_on_right_and_bottom():
    api, cpu, _ = _rig()
    _contract(api, "USER", 76, "ptr long")
    for i, v in enumerate((10, 20, 30, 40)):            # l, t, r, b
        cpu.mem.ww(0x7000, 0x10 + 2 * i, v)
    rc = (0x7000 << 16) | 0x10
    pt = lambda x, y: (y & 0xFFFF) << 16 | (x & 0xFFFF)
    assert _call(api, cpu, "USER", 76, (rc, pt(15, 25))) == 1     # inside
    assert _call(api, cpu, "USER", 76, (rc, pt(10, 20))) == 1     # top-left IS in
    assert _call(api, cpu, "USER", 76, (rc, pt(30, 25))) == 0     # right edge OUT
    assert _call(api, cpu, "USER", 76, (rc, pt(15, 40))) == 0     # bottom edge OUT
    assert _call(api, cpu, "USER", 76, (rc, pt(9, 25))) == 0


def test_ptinrect_handles_negative_coordinates():
    api, cpu, _ = _rig()
    for i, v in enumerate((-10 & 0xFFFF, -10 & 0xFFFF, 10, 10)):
        cpu.mem.ww(0x7000, 0x10 + 2 * i, v)
    rc = (0x7000 << 16) | 0x10
    neg = (-5 & 0xFFFF)
    assert _call(api, cpu, "USER", 76, (rc, (neg << 16) | neg)) == 1
    assert _call(api, cpu, "USER", 76, (rc, (neg << 16) | (-20 & 0xFFFF))) == 0


def _window(sysobj, title):
    cls = WndClass(name="c", style=0, wndproc=(0, 0), cls_extra=0, wnd_extra=0,
                   h_instance=0, h_icon=0, h_cursor=0, h_background=0,
                   menu_name=None)
    win = Window(wndclass=cls, title=title, style=0, x=0, y=0, w=10, h=10,
                 parent=0, menu=0)
    sysobj.handles.add(win)
    return win


def test_getwindowtext_truncates_to_cch_and_always_terminates():
    api, cpu, sysobj = _rig()
    _contract(api, "USER", 36, "word segptr word")
    win = _window(sysobj, "SimAnt - Quick Game")
    buf = (0x8000 << 16) | 0x20
    assert _call(api, cpu, "USER", 36, (win.handle, buf, 128)) == 19
    assert cpu.mem.block(0x8000, 0x20, 20) == b"SimAnt - Quick Game\x00"
    # cch COUNTS the NUL: 7 bytes of buffer hold 6 characters.
    assert _call(api, cpu, "USER", 36, (win.handle, buf, 7)) == 6
    assert cpu.mem.block(0x8000, 0x20, 7) == b"SimAnt\x00"


def test_getwindowtext_on_a_non_window_handle_is_the_empty_string():
    api, cpu, _ = _rig()
    buf = (0x8000 << 16) | 0x20
    cpu.mem.load(0x8000, 0x20, b"stale")
    assert _call(api, cpu, "USER", 36, (0x1234, buf, 128)) == 0
    assert cpu.mem.rb(0x8000, 0x20) == 0


# --------------------------------------------------------------------------
# USER.70 SetCursorPos
# --------------------------------------------------------------------------

def test_setcursorpos_moves_the_guest_visible_cursor_immediately():
    api, cpu, _ = _rig()
    _contract(api, "USER", 70, "word word")
    _call(api, cpu, "USER", 70, (100, 200))
    assert api.services["cursor_pos"] == (100, 200)     # a GetCursorPos poll sees it


def test_setcursorpos_warps_the_host_pointer_when_the_host_offers_one():
    api, cpu, _ = _rig()
    warps = []
    api.services["cursor_warp"] = lambda x, y: warps.append((x, y))
    _call(api, cpu, "USER", 70, (8, 16))
    assert warps == [(8, 16)]
    # Screen coordinates are signed: off the left edge must not become 65528.
    _call(api, cpu, "USER", 70, (-8 & 0xFFFF, 16))
    assert warps[-1] == (-8, 16)


# --------------------------------------------------------------------------
# USER.268/269/118 — the atom table
# --------------------------------------------------------------------------

def test_atom_table_is_unique_case_insensitive_and_refcounted():
    t = AtomTable()
    a = t.add("SimAntTopic")
    assert t.add("SIMANTTOPIC") == a                    # case-insensitive
    assert t.add("Other") != a                          # distinct string
    assert 0xC000 <= a <= 0xFFFF                        # the string-atom space
    assert t.delete(a) == 0                             # one ref dropped...
    assert t.find("SimAntTopic") == a                   # ...still alive (2 adds)
    assert t.delete(a) == 0
    assert t.find("SimAntTopic") == 0                   # gone on the last delete


def test_globaldeleteatom_reports_an_atom_it_does_not_own():
    t = AtomTable()
    assert t.delete(0xC123) == 0xC123                   # USER's failure answer


def test_integer_atoms_fail_loud_rather_than_colliding():
    with pytest.raises(NotImplementedError):
        AtomTable().add("#1234")


def test_globaladdatom_and_delete_round_trip_through_the_registry():
    api, cpu, _ = _rig()
    _contract(api, "USER", 268, "str")
    _contract(api, "USER", 269, "word")
    app = _put_str(cpu, 0x2000, 0x00, "SimAnt")
    topic = _put_str(cpu, 0x2000, 0x20, "System")
    a_app = _call(api, cpu, "USER", 268, (app,))
    a_topic = _call(api, cpu, "USER", 268, (topic,))
    assert a_app and a_topic and a_app != a_topic
    assert _call(api, cpu, "USER", 269, (a_app,)) == 0
    assert _call(api, cpu, "USER", 269, (a_topic,)) == 0


def test_registerwindowmessage_is_stable_unique_and_above_wm_user():
    api, cpu, _ = _rig()
    _contract(api, "USER", 118, "str")
    name = _put_str(cpu, 0x2000, 0x40, "mmsystem_snd_msg")
    other = _put_str(cpu, 0x2000, 0x60, "something_else")
    m1 = _call(api, cpu, "USER", 118, (name,))
    m2 = _call(api, cpu, "USER", 118, (name,))
    assert m1 == m2                                     # stable for the session
    assert _call(api, cpu, "USER", 118, (other,)) != m1
    # Above every WM_USER-relative control message — the property a wndproc
    # comparing message ids depends on.
    assert m1 > 0x0400 and 0xC000 <= m1 <= 0xFFFF


# --------------------------------------------------------------------------
# USER.415 CreatePopupMenu
# --------------------------------------------------------------------------

def test_createpopupmenu_returns_a_distinct_empty_menu():
    api, cpu, sysobj = _rig()
    h1 = _call(api, cpu, "USER", 415)
    h2 = _call(api, cpu, "USER", 415)
    assert h1 and h2 and h1 != h2
    menu = sysobj.handles.get(h1)
    assert isinstance(menu, Menu) and menu.items == []
    # It must be AppendMenu-able: the submenu path that proved the API.
    bar = _call(api, cpu, "USER", 151)
    text = _put_str(cpu, 0x3000, 0, "&File")
    assert _call(api, cpu, "USER", 411, (bar, 0x10, h1, text)) == 1   # MF_POPUP
    assert len(sysobj.handles.get(bar).items) == 1


# --------------------------------------------------------------------------
# USER.180/181 — system colours
# --------------------------------------------------------------------------

def test_getsyscolor_returns_a_colorref_in_bgr_order():
    api, cpu, _ = _rig()
    _contract(api, "USER", 180, "word", ret="long")
    # COLOR_BACKGROUND (1) is Win3.1 teal (0,128,128) -> 0x00808000 as BGR.
    assert _call(api, cpu, "USER", 180, (1,)) == 0x00808000
    assert _call(api, cpu, "USER", 180, (5,)) == 0x00FFFFFF     # WINDOW = white
    assert _call(api, cpu, "USER", 180, (999,)) == 0            # out of range


def test_setsyscolors_updates_only_the_named_indices_and_getsyscolor_sees_it():
    api, cpu, sysobj = _rig()
    _contract(api, "USER", 181, "word ptr ptr")
    idx, col = 0x4000, 0x4100
    for i, v in enumerate((5, 8)):                       # WINDOW, WINDOWTEXT
        cpu.mem.ww(0x9000, idx + 2 * i, v)
    for i, v in enumerate((0x00000000, 0x00C0C0C0)):    # black, light grey
        cpu.mem.ww(0x9000, col + 4 * i, v & 0xFFFF)
        cpu.mem.ww(0x9000, col + 4 * i + 2, v >> 16)
    n = 2
    assert _call(api, cpu, "USER", 181,
                 (n, (0x9000 << 16) | idx, (0x9000 << 16) | col)) == 1
    assert _call(api, cpu, "USER", 180, (5,)) == 0x00000000
    assert _call(api, cpu, "USER", 180, (8,)) == 0x00C0C0C0
    assert _call(api, cpu, "USER", 180, (1,)) == 0x00808000     # untouched
    # The class-background resolver reads the LIVE table, so the new scheme
    # lands on the next repaint with nothing to broadcast.
    from win16.api.gdi import class_background_rgb
    assert class_background_rgb(sysobj, 5 + 1) == (0, 0, 0)


def test_the_syscolor_table_is_per_machine_not_the_module_constant():
    api_a, cpu_a, sys_a = _rig()
    api_b, _cpu_b, sys_b = _rig()
    sys_colors(sys_a)[5] = (1, 2, 3)
    assert sys_colors(sys_b)[5] == SYS_COLORS[5]        # untouched
    assert SYS_COLORS[5] == (0xFF, 0xFF, 0xFF)          # the constant is intact


def test_save_and_restore_all_19_system_colours_round_trips():
    # SimAnt's _SetUpPalette: GetSysColor all 19 on the way in, SetSysColors its
    # own scheme, then SetSysColors(saved) on the way out.  End state == start.
    api, cpu, _ = _rig()
    before = [_call(api, cpu, "USER", 180, (i,)) for i in range(19)]
    idx, mine, saved = 0x0, 0x100, 0x200
    for i in range(19):
        cpu.mem.ww(0xA000, idx + 2 * i, i)
        cpu.mem.ww(0xA000, mine + 4 * i, 0x0F0F)
        cpu.mem.ww(0xA000, mine + 4 * i + 2, 0x000F)
        cpu.mem.ww(0xA000, saved + 4 * i, before[i] & 0xFFFF)
        cpu.mem.ww(0xA000, saved + 4 * i + 2, before[i] >> 16)
    p = lambda o: (0xA000 << 16) | o
    _call(api, cpu, "USER", 181, (19, p(idx), p(mine)))
    assert [_call(api, cpu, "USER", 180, (i,)) for i in range(19)] == [0x000F0F0F] * 19
    _call(api, cpu, "USER", 181, (19, p(idx), p(saved)))
    assert [_call(api, cpu, "USER", 180, (i,)) for i in range(19)] == before


# --------------------------------------------------------------------------
# GDI.44 SelectClipRgn
# --------------------------------------------------------------------------

def _dc(sysobj):
    dc = DC(window=None, bitmap=None)
    sysobj.handles.add(dc)
    return dc


def test_selectcliprgn_replaces_the_clip_with_a_copy_of_the_region():
    api, cpu, sysobj = _rig()
    _contract(api, "GDI", 44, "word word")
    dc = _dc(sysobj)
    hrgn = _call(api, cpu, "GDI", 64, (32, 60, 285, 169))      # CreateRectRgn
    assert _call(api, cpu, "GDI", 44, (dc.handle, hrgn)) == 2   # SIMPLEREGION
    assert dc.clip_rect == (32, 60, 285, 169)
    # A COPY: deleting the region right after (what the caller does) must not
    # disturb the clip.
    assert _call(api, cpu, "GDI", 69, (hrgn,)) == 1             # DeleteObject
    assert dc.clip_rect == (32, 60, 285, 169)


def test_selectcliprgn_replaces_rather_than_intersects():
    api, cpu, sysobj = _rig()
    dc = _dc(sysobj)
    _call(api, cpu, "GDI", 22, (dc.handle, 0, 0, 50, 50))       # IntersectClipRect
    assert dc.clip_rect == (0, 0, 50, 50)
    hrgn = _call(api, cpu, "GDI", 64, (100, 100, 200, 200))
    _call(api, cpu, "GDI", 44, (dc.handle, hrgn))
    assert dc.clip_rect == (100, 100, 200, 200)                 # not an intersection


def test_selectcliprgn_null_removes_the_clip_and_an_empty_region_is_nullregion():
    api, cpu, sysobj = _rig()
    dc = _dc(sysobj)
    hrgn = _call(api, cpu, "GDI", 64, (10, 10, 20, 20))
    _call(api, cpu, "GDI", 44, (dc.handle, hrgn))
    assert _call(api, cpu, "GDI", 44, (dc.handle, 0)) == 2      # SIMPLEREGION
    assert dc.clip_rect is None                                 # unclipped again
    empty = _call(api, cpu, "GDI", 64, (5, 5, 5, 5))
    assert _call(api, cpu, "GDI", 44, (dc.handle, empty)) == 1  # NULLREGION
    # A handle that is not a region is ERROR, not a silent success.
    assert _call(api, cpu, "GDI", 44, (dc.handle, 0x4321)) == 0xFFFF


def test_savedc_restoredc_round_trips_the_clip_region():
    api, cpu, sysobj = _rig()
    dc = _dc(sysobj)
    hrgn = _call(api, cpu, "GDI", 64, (1, 2, 3, 4))
    _call(api, cpu, "GDI", 44, (dc.handle, hrgn))
    _call(api, cpu, "GDI", 30, (dc.handle,))                    # SaveDC
    _call(api, cpu, "GDI", 44, (dc.handle, 0))
    assert dc.clip_rect is None
    _call(api, cpu, "GDI", 39, (dc.handle, 0xFFFF))             # RestoreDC(-1)
    assert dc.clip_rect == (1, 2, 3, 4)


# --------------------------------------------------------------------------
# GDI.19/20 MoveTo + LineTo
# --------------------------------------------------------------------------

def _dc_on_surface(sysobj, w, h):
    from win16.api.objects import Bitmap
    surf = Surface(w, h, bytearray(w * h * 3))
    dc = DC(window=None, bitmap=Bitmap(surf), is_memory=True)
    sysobj.handles.add(dc)
    return dc, surf


def _px(s, x, y):
    o = (y * s.w + x) * 3
    return tuple(s.pixels[o:o + 3])


def test_moveto_returns_the_previous_position_packed_as_makelong():
    api, cpu, sysobj = _rig()
    _contract(api, "GDI", 20, "word s_word s_word", ret="long")
    dc = _dc(sysobj)
    assert _call(api, cpu, "GDI", 20, (dc.handle, 10, 20)) == 0          # was (0,0)
    assert _call(api, cpu, "GDI", 20, (dc.handle, 30, 40)) == (20 << 16) | 10
    assert dc.cur_pos == (30, 40)


def test_lineto_draws_from_the_current_position_with_the_selected_pen():
    api, cpu, sysobj = _rig()
    _contract(api, "GDI", 19, "word s_word s_word")
    dc, surf = _dc_on_surface(sysobj, 20, 10)
    pen = _call(api, cpu, "GDI", 61, (0, 1, 0x000000FF))     # CreatePen solid red
    _call(api, cpu, "GDI", 45, (dc.handle, pen))             # SelectObject
    _call(api, cpu, "GDI", 20, (dc.handle, 2, 5))            # MoveTo
    assert _call(api, cpu, "GDI", 19, (dc.handle, 15, 5)) == 1
    assert _px(surf, 2, 5) == (255, 0, 0)                    # start drawn
    assert _px(surf, 15, 5) == (255, 0, 0)                   # end drawn
    assert _px(surf, 8, 5) == (255, 0, 0)                    # between drawn
    assert _px(surf, 8, 6) == (0, 0, 0)                      # nothing else
    assert dc.cur_pos == (15, 5)                             # CP moved to the end


def test_lineto_chains_and_a_null_pen_still_moves_the_current_position():
    api, cpu, sysobj = _rig()
    dc, surf = _dc_on_surface(sysobj, 20, 20)
    null_pen = _call(api, cpu, "GDI", 61, (5, 1, 0x00FFFFFF))   # PS_NULL
    _call(api, cpu, "GDI", 45, (dc.handle, null_pen))
    _call(api, cpu, "GDI", 20, (dc.handle, 1, 1))
    _call(api, cpu, "GDI", 19, (dc.handle, 10, 10))
    assert dc.cur_pos == (10, 10)                            # moved...
    assert _px(surf, 5, 5) == (0, 0, 0)                      # ...but drew nothing
    # A second LineTo starts where the first ended — the polyline contract.
    pen = _call(api, cpu, "GDI", 61, (0, 1, 0x0000FF00))     # green
    _call(api, cpu, "GDI", 45, (dc.handle, pen))
    _call(api, cpu, "GDI", 19, (dc.handle, 10, 15))
    assert _px(surf, 10, 12) == (0, 255, 0)


def test_lineto_clips_to_the_surface_rather_than_overrunning_it():
    api, cpu, sysobj = _rig()
    dc, surf = _dc_on_surface(sysobj, 8, 8)
    pen = _call(api, cpu, "GDI", 61, (0, 1, 0x00FFFFFF))
    _call(api, cpu, "GDI", 45, (dc.handle, pen))
    # A line running well outside the surface on both ends: only the covered
    # span may be painted, and nothing may write past the pixel buffer.
    _call(api, cpu, "GDI", 20, (dc.handle, -20 & 0xFFFF, 4))
    assert _call(api, cpu, "GDI", 19, (dc.handle, 40, 4)) == 1
    assert _px(surf, 0, 4) == (255, 255, 255)
    assert _px(surf, 7, 4) == (255, 255, 255)
    assert _px(surf, 4, 3) == (0, 0, 0)
    assert len(surf.pixels) == 8 * 8 * 3
    assert dc.cur_pos == (40, 4)                                 # CP is the endpoint


# --------------------------------------------------------------------------
# GDI.373 SetSystemPaletteUse
# --------------------------------------------------------------------------

def test_setsystempaletteuse_returns_the_previous_use_and_is_readable_back():
    api, cpu, sysobj = _rig()
    _contract(api, "GDI", 373, "word word")
    dc = _dc(sysobj)
    SYSPAL_STATIC, SYSPAL_NOSTATIC = 1, 2
    assert _call(api, cpu, "GDI", 374, (dc.handle,)) == SYSPAL_STATIC   # default
    assert _call(api, cpu, "GDI", 373, (dc.handle, SYSPAL_NOSTATIC)) == SYSPAL_STATIC
    assert _call(api, cpu, "GDI", 374, (dc.handle,)) == SYSPAL_NOSTATIC
    assert _call(api, cpu, "GDI", 373, (dc.handle, SYSPAL_STATIC)) == SYSPAL_NOSTATIC


def test_setsystempaletteuse_fails_loud_on_a_bad_dc_or_unknown_usage():
    api, cpu, sysobj = _rig()
    assert _call(api, cpu, "GDI", 373, (0x9999, 2)) == 0        # SYSPAL_ERROR
    dc = _dc(sysobj)
    with pytest.raises(NotImplementedError):
        _call(api, cpu, "GDI", 373, (dc.handle, 7))


# --------------------------------------------------------------------------
# KERNEL.115 OutputDebugString + the PANIC path (KERNEL.1 / KERNEL.137)
# --------------------------------------------------------------------------

def test_outputdebugstring_goes_to_the_hosts_debug_sink_verbatim():
    api, cpu, _ = _rig()
    _contract(api, "KERNEL", 115, "str", ret="void")
    out = []
    api.services["debug_out"] = out.append
    _call(api, cpu, "KERNEL", 115, (_put_str(cpu, 0x1000, 0, "\r"),))
    _call(api, cpu, "KERNEL", 115, (_put_str(cpu, 0x1000, 4, "trace: tick 42\n"),))
    assert out == ["\r", "trace: tick 42\n"]        # no newline of our own


def test_fatalappexit_stops_loudly_and_carries_the_runtimes_message():
    api, cpu, _ = _rig()
    _contract(api, "KERNEL", 137, "word str", ret="void")
    msg = _put_str(cpu, 0x1000, 0x20, "run-time error R6002 - floating point not loaded")
    with pytest.raises(FatalAppExitError) as exc:
        _call(api, cpu, "KERNEL", 137, (0, msg))
    assert "R6002" in str(exc.value)


def test_fatalexit_stops_loudly_and_never_returns_success():
    api, cpu, _ = _rig()
    _contract(api, "KERNEL", 1, "word", ret="void")
    with pytest.raises(FatalAppExitError) as exc:
        _call(api, cpu, "KERNEL", 1, (0xFF,))
    assert "0xff" in str(exc.value).lower()


# --------------------------------------------------------------------------
# KEYBOARD.131 MapVirtualKey
# --------------------------------------------------------------------------

def test_mapvirtualkey_vk_to_char_is_the_unshifted_character():
    api, cpu, _ = _rig()
    _contract(api, "KEYBOARD", 131, "word word")
    MAPVK_VK_TO_VSC, MAPVK_VK_TO_CHAR = 0, 2
    assert _call(api, cpu, "KEYBOARD", 131, (ord("A"), MAPVK_VK_TO_CHAR)) == ord("A")
    assert _call(api, cpu, "KEYBOARD", 131, (ord("Z"), MAPVK_VK_TO_CHAR)) == ord("Z")
    assert _call(api, cpu, "KEYBOARD", 131, (0x20, MAPVK_VK_TO_CHAR)) == 0x20  # SPACE
    # Keys with NO character answer 0 — VK_INSERT is one of the two SimAnt asks
    # about, and the answer is fed straight into a 256-entry ctype table.
    assert _call(api, cpu, "KEYBOARD", 131, (0x2D, MAPVK_VK_TO_CHAR)) == 0
    assert _call(api, cpu, "KEYBOARD", 131, (0x70, MAPVK_VK_TO_CHAR)) == 0     # F1


def test_mapvirtualkey_every_character_fits_one_byte():
    # The call site indexes the C runtime's 256-entry ctype table with the raw
    # result; anything above 0xFF would read out of that table.
    for vk in _US_LAYOUT:
        _scan, ch = _US_LAYOUT[vk]
        assert 0 <= ch <= 0xFF


def test_mapvirtualkey_vk_to_scancode_is_the_us_layout():
    api, cpu, _ = _rig()
    MAPVK_VK_TO_VSC, MAPVK_VSC_TO_VK = 0, 1
    assert _call(api, cpu, "KEYBOARD", 131, (0x20, MAPVK_VK_TO_VSC)) == 0x39   # SPACE
    assert _call(api, cpu, "KEYBOARD", 131, (0x2D, MAPVK_VK_TO_VSC)) == 0x52   # INS
    assert _call(api, cpu, "KEYBOARD", 131, (ord("A"), MAPVK_VK_TO_VSC)) == 0x1E
    assert _call(api, cpu, "KEYBOARD", 131, (ord("Q"), MAPVK_VK_TO_VSC)) == 0x10
    # The inverse maps back.
    assert _call(api, cpu, "KEYBOARD", 131, (0x1E, MAPVK_VSC_TO_VK)) == ord("A")


def test_mapvirtualkey_refuses_to_invent_a_mapping():
    api, cpu, _ = _rig()
    with pytest.raises(NotImplementedError):
        _call(api, cpu, "KEYBOARD", 131, (0xFE, 0))         # not on the layout
    with pytest.raises(NotImplementedError):
        _call(api, cpu, "KEYBOARD", 131, (ord("A"), 9))     # unknown map type


# --------------------------------------------------------------------------
# the tier as a whole
# --------------------------------------------------------------------------

def test_every_api_this_slice_added_is_named_from_the_ordinal_table():
    # A handler whose name disagrees with the Wine ordinal table cannot even be
    # registered (ApiRegistry.register raises), so building the surface at all
    # is the proof — this pins that the entries EXIST and are named.
    from win16.api.ordinals import ORDINAL_NAMES
    api = build_registry()
    expected = {
        ("USER", 36): "GetWindowText", ("USER", 70): "SetCursorPos",
        ("USER", 76): "PtInRect", ("USER", 99): "DlgDirSelect",
        ("USER", 118): "RegisterWindowMessage", ("USER", 180): "GetSysColor",
        ("USER", 181): "SetSysColors", ("USER", 268): "GlobalAddAtom",
        ("USER", 269): "GlobalDeleteAtom", ("USER", 415): "CreatePopupMenu",
        ("USER", 473): "AnsiPrev", ("GDI", 19): "LineTo", ("GDI", 20): "MoveTo",
        ("GDI", 44): "SelectClipRgn", ("GDI", 373): "SetSystemPaletteUse",
        ("KERNEL", 1): "FatalExit", ("KERNEL", 115): "OutputDebugString",
        ("KERNEL", 137): "FatalAppExit", ("KEYBOARD", 131): "MapVirtualKey",
    }
    for (module, ordinal), name in expected.items():
        assert api.entries[(module, ordinal)].name == name
        assert ORDINAL_NAMES[module][ordinal] == name
