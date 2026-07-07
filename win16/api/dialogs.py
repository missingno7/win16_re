"""The Win16 dialog engine: DialogBox, dialog procs, and the dialog APIs.

Model: `Dialog` + `DialogControlState` live here (game-agnostic); the game's
own dialog procedure runs in the VM via callbacks.  DialogBox blocks the CPU
thread in a modal event loop, exactly like real USER:

    build from the DIALOG resource -> WM_INITDIALOG -> loop { control event
    -> WM_COMMAND to the dialog proc } until EndDialog -> return its result.

Presentation is pluggable through `services["dialog_ui"]` (an object with
show/update/close); the interactive player renders real widgets.  Without a
host (headless replay) the modal loop auto-answers IDOK/IDCANCEL — the same
policy as headless MessageBox.
"""
from __future__ import annotations

import queue
from dataclasses import dataclass, field

from win16.dialog import DialogTemplate, parse_dialog

from .core import ApiRegistry, CallContext, Win16ApiGap
from .system import Win16System

WM_INITDIALOG = 0x0110
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_USER = 0x0400

IDOK, IDCANCEL = 1, 2

BS_AUTORADIOBUTTON = 0x9

# Win16 control messages are WM_USER-relative, per control class.
BM_GETCHECK, BM_SETCHECK = WM_USER + 0, WM_USER + 1
CB_GETEDITSEL = WM_USER + 0
CB_LIMITTEXT = WM_USER + 1
CB_SETEDITSEL = WM_USER + 2
CB_ADDSTRING = WM_USER + 3
CB_DIR = WM_USER + 5
CB_GETCOUNT = WM_USER + 6
CB_GETCURSEL = WM_USER + 7
CB_GETLBTEXT = WM_USER + 8
CB_GETLBTEXTLEN = WM_USER + 9
CB_INSERTSTRING = WM_USER + 10
CB_RESETCONTENT = WM_USER + 11
CB_FINDSTRING = WM_USER + 12
CB_SELECTSTRING = WM_USER + 13
CB_SETCURSEL = WM_USER + 14

# Edit-control messages (WM_USER-relative in Win16).
EM_GETSEL = WM_USER + 0
EM_SETSEL = WM_USER + 1
EM_LIMITTEXT = WM_USER + 21


@dataclass
class DialogControlState:
    ctrl_id: int
    cls: str                    # "Button", "Edit", "Static", "ComboBox", ...
    style: int
    text: str                   # live text mirror (host keeps it current)
    template: object            # win16.dialog.DialogControl
    checked: int = 0
    items: list[str] = field(default_factory=list)
    sel: int = -1
    limit: int = 0
    handle: int = 0

    @property
    def is_auto_radio(self) -> bool:
        return self.cls == "Button" and (self.style & 0xF) == BS_AUTORADIOBUTTON

    def geom_px(self) -> tuple[int, int, int, int]:
        from win16.dialog import du_to_px
        tc = self.template
        x, y = du_to_px(tc.x, tc.y)
        w, h = du_to_px(tc.cx, tc.cy)
        return x, y, w, h        # relative to the dialog client


@dataclass
class Dialog:
    name: str
    template: DialogTemplate
    proc: tuple[int, int]       # far pointer to the game's dialog procedure
    parent: int
    controls: list[DialogControlState] = field(default_factory=list)
    by_id: dict[int, DialogControlState] = field(default_factory=dict)
    events: "queue.Queue" = field(default_factory=queue.Queue)
    ended: bool = False
    result: int = 0
    x: int = 0
    y: int = 0
    handle: int = 0

    def size_px(self) -> tuple[int, int]:
        from win16.dialog import du_to_px
        return du_to_px(self.template.cx, self.template.cy)

    def geom_px(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, *self.size_px())

    def control(self, ctrl_id: int) -> DialogControlState:
        ctrl = self.by_id.get(ctrl_id)
        if ctrl is None:
            raise Win16ApiGap(f"dialog {self.name}: no control id {ctrl_id}")
        return ctrl

    def set_radio(self, ctrl: DialogControlState) -> None:
        """AUTORADIOBUTTON semantics: checking one clears its siblings."""
        for other in self.controls:
            if other.is_auto_radio:
                other.checked = 1 if other is ctrl else 0


def _sys(ctx: CallContext) -> Win16System:
    return ctx.registry.services["system"]


def _dialog(ctx: CallContext, hdlg: int) -> Dialog:
    dlg = _sys(ctx).handles.get(hdlg)
    if not isinstance(dlg, Dialog):
        raise Win16ApiGap(f"handle {hdlg:04X} is not a dialog")
    return dlg


def _host(ctx: CallContext):
    return ctx.registry.services.get("dialog_ui")


def _call_proc(ctx: CallContext, dlg: Dialog, msg: int, wparam: int,
               lparam: int) -> int:
    from win16.callback import call_far
    from win16.loader import THUNK_SEG
    seg, off = dlg.proc
    machine = _sys(ctx).machine
    ax, _dx = call_far(machine.cpu, THUNK_SEG, seg, off,
                       [dlg.handle, msg, wparam & 0xFFFF,
                        (lparam >> 16) & 0xFFFF, lparam & 0xFFFF])
    return ax


def _pump_timers(ctx: CallContext) -> None:
    """Real modal loops keep dispatching WM_PAINT + WM_TIMER to other windows;
    without this the game (repaints, music, animation) would freeze under an
    open dialog."""
    sysobj = _sys(ctx)
    if sysobj.message_source is None:
        return
    sysobj.pump_modal(paint=True, timers=True)


def _dispatch_dialog_event(ctx: CallContext, dlg: Dialog, event) -> None:
    kind = event[0]
    if kind == "command":
        _ctrl_id, notify = event[1], event[2]
        ctrl = dlg.by_id.get(_ctrl_id)
        if ctrl is not None and ctrl.is_auto_radio:
            dlg.set_radio(ctrl)
        lparam = ((notify & 0xFFFF) << 16) | (ctrl.handle if ctrl else 0)
        _call_proc(ctx, dlg, WM_COMMAND, _ctrl_id, lparam)
    elif kind == "settext":                             # recorded edit content
        dlg.control(event[1]).text = event[2]
    elif kind == "close":                               # window X == Cancel
        _call_proc(ctx, dlg, WM_COMMAND, IDCANCEL, 0)
        if not dlg.ended:
            dlg.ended = True
            dlg.result = IDCANCEL
    else:
        raise Win16ApiGap(f"dialog event {kind!r} not understood")


def _run_modal(ctx: CallContext, dlg: Dialog) -> int:
    sysobj = _sys(ctx)
    host = _host(ctx)
    player = ctx.registry.services.get("demo_player")
    recorder = ctx.registry.services.get("demo_recorder")
    if host is not None:
        host.show(dlg)
    _call_proc(ctx, dlg, WM_INITDIALOG, 0, 0)

    if player is not None:
        # Replay: consume the recorded dialog-event stream, byte for byte.
        while not dlg.ended:
            _dispatch_dialog_event(ctx, dlg, player.next_dialog_event(dlg.name))
    elif host is None:
        # Headless: answer like a user pressing OK (then Cancel), same policy
        # as the auto-OK MessageBox.  Anything beyond that is a gap.
        for answer in (IDOK, IDCANCEL):
            if dlg.ended:
                break
            event = ("command", answer, 0)
            if recorder is not None:
                recorder.dialog_event(dlg.name, event)
            _dispatch_dialog_event(ctx, dlg, event)
        if not dlg.ended:
            raise Win16ApiGap(f"dialog {dlg.name}: proc ended on neither OK nor Cancel")
    else:
        while not dlg.ended:
            try:
                event = dlg.events.get(timeout=0.03)
            except queue.Empty:
                sysobj.clock_ms = max(sysobj.clock_ms,
                                      getattr(host, "now_ms", lambda: sysobj.clock_ms)())
                _pump_timers(ctx)
                continue
            # Edit widgets mirror text live; capture the final content in the
            # demo just before each command so replay reproduces typed input.
            if recorder is not None:
                if event[0] == "command":
                    for ctrl in dlg.controls:
                        if ctrl.cls == "Edit" and ctrl.ctrl_id != 0xFFFF:
                            recorder.dialog_event(
                                dlg.name, ("settext", ctrl.ctrl_id, ctrl.text))
                recorder.dialog_event(dlg.name, event)
            _dispatch_dialog_event(ctx, dlg, event)
    if host is not None:
        host.close(dlg)
    return dlg.result


def install(api: ApiRegistry) -> None:
    @api.register("USER", 87, args="word str word segptr")
    def DialogBox(ctx: CallContext) -> int:             # (hInst, template, parent, proc)
        from .user import _resource_name
        sysobj = _sys(ctx)
        name = _resource_name(ctx, ctx.args[1])
        res = sysobj.machine.exe.lookup_resource("DIALOG", name)
        if res is None:
            raise Win16ApiGap(f"DIALOG resource {name!r} not found")
        template = parse_dialog(res.data)
        proc = ((ctx.args[3] >> 16) & 0xFFFF, ctx.args[3] & 0xFFFF)
        dlg = Dialog(str(name), template, proc, ctx.args[2])
        for tc in template.controls:
            ctrl = DialogControlState(tc.ctrl_id, tc.cls, tc.style, tc.text, tc)
            sysobj.handles.add(ctrl)
            dlg.controls.append(ctrl)
            if tc.ctrl_id != 0xFFFF:
                dlg.by_id[tc.ctrl_id] = ctrl
        sysobj.handles.add(dlg)
        try:
            return _run_modal(ctx, dlg)
        finally:
            for ctrl in dlg.controls:
                sysobj.handles.remove(ctrl.handle)
            sysobj.handles.remove(dlg.handle)

    @api.register("USER", 88, args="word s_word")       # EndDialog(hdlg, result)
    def EndDialog(ctx: CallContext) -> int:
        dlg = _dialog(ctx, ctx.args[0])
        dlg.result = ctx.args[1]
        dlg.ended = True
        return 1

    @api.register("USER", 91, args="word word")         # GetDlgItem(hdlg, id)
    def GetDlgItem(ctx: CallContext) -> int:
        return _dialog(ctx, ctx.args[0]).control(ctx.args[1]).handle

    @api.register("USER", 92, args="word word segstr")  # SetDlgItemText
    def SetDlgItemText(ctx: CallContext) -> int:
        dlg = _dialog(ctx, ctx.args[0])
        ctrl = dlg.control(ctx.args[1])
        ctrl.text = ctx.read_string(ctx.args[2]).decode("latin-1")
        host = _host(ctx)
        if host is not None:
            host.update(dlg, ctrl)
        return 0

    @api.register("USER", 93, args="word word segptr word")
    def GetDlgItemText(ctx: CallContext) -> int:        # (hdlg, id, buf, cap)
        dlg = _dialog(ctx, ctx.args[0])
        text = dlg.control(ctx.args[1]).text.encode("latin-1")[:max(ctx.args[3] - 1, 0)]
        buf = ctx.args[2]
        ctx.mem.load((buf >> 16) & 0xFFFF, buf & 0xFFFF, text + b"\x00")
        return len(text)

    @api.register("USER", 94, args="word word word word")
    def SetDlgItemInt(ctx: CallContext) -> int:         # (hdlg, id, value, signed)
        dlg = _dialog(ctx, ctx.args[0])
        ctrl = dlg.control(ctx.args[1])
        value = ctx.args[2]
        if ctx.args[3] and value & 0x8000:
            value -= 0x10000
        ctrl.text = str(value)
        host = _host(ctx)
        if host is not None:
            host.update(dlg, ctrl)
        return 0

    @api.register("USER", 95, args="word s_word ptr word")
    def GetDlgItemInt(ctx: CallContext) -> int:         # (hdlg, id, ok_ptr, signed)
        dlg = _dialog(ctx, ctx.args[0])
        text = dlg.control(ctx.args[1]).text.strip()
        ok, value = 1, 0
        try:
            value = int(text or "0")
        except ValueError:
            ok = 0
        if ctx.args[2]:
            p = ctx.args[2]
            ctx.mem.ww((p >> 16) & 0xFFFF, p & 0xFFFF, ok)
        return value & 0xFFFF

    @api.register("USER", 101, args="word word word word long", ret="long")
    def SendDlgItemMessage(ctx: CallContext) -> int:    # (hdlg, id, msg, wp, lp)
        dlg = _dialog(ctx, ctx.args[0])
        ctrl = dlg.control(ctx.args[1])
        msg, wparam, lparam = ctx.args[2], ctx.args[3], ctx.args[4]
        host = _host(ctx)

        def refresh():
            if host is not None:
                host.update(dlg, ctrl)

        if ctrl.cls == "Button":
            if msg == BM_GETCHECK:
                return ctrl.checked
            if msg == BM_SETCHECK:
                if ctrl.is_auto_radio and wparam:
                    dlg.set_radio(ctrl)
                else:
                    ctrl.checked = wparam & 0xFFFF
                refresh()
                return 0
        elif ctrl.cls == "ComboBox":
            if msg in (CB_GETEDITSEL, CB_SETEDITSEL, CB_LIMITTEXT):
                # Edit-selection / length limit on the combo's edit box —
                # host-managed; nothing to model.
                if msg == CB_LIMITTEXT:
                    ctrl.limit = wparam
                return 0
            if msg == CB_GETLBTEXTLEN:
                return len(ctrl.items[wparam]) if 0 <= wparam < len(ctrl.items) else 0xFFFF
            if msg == CB_RESETCONTENT:
                ctrl.items.clear()
                ctrl.sel = -1
                refresh()
                return 0
            if msg in (CB_ADDSTRING, CB_INSERTSTRING):
                text = ctx.read_string(lparam).decode("latin-1")
                if msg == CB_ADDSTRING:
                    ctrl.items.append(text)
                    index = len(ctrl.items) - 1
                else:
                    index = wparam if wparam != 0xFFFF else len(ctrl.items)
                    ctrl.items.insert(index, text)
                refresh()
                return index
            if msg == CB_GETCOUNT:
                return len(ctrl.items)
            if msg == CB_GETCURSEL:
                return ctrl.sel if ctrl.sel >= 0 else 0xFFFF
            if msg == CB_SETCURSEL:
                ctrl.sel = wparam if wparam != 0xFFFF else -1
                refresh()
                return ctrl.sel & 0xFFFF
            if msg == CB_GETLBTEXT:
                if 0 <= wparam < len(ctrl.items):
                    text = ctrl.items[wparam].encode("latin-1")
                    ctx.mem.load((lparam >> 16) & 0xFFFF, lparam & 0xFFFF,
                                 text + b"\x00")
                    return len(text)
                return 0xFFFF
            if msg in (CB_FINDSTRING, CB_SELECTSTRING):
                prefix = ctx.read_string(lparam).decode("latin-1").upper()
                for i, item in enumerate(ctrl.items):
                    if item.upper().startswith(prefix):
                        if msg == CB_SELECTSTRING:
                            ctrl.sel = i
                            refresh()
                        return i
                return 0xFFFF
        elif ctrl.cls == "Edit":
            if msg == EM_LIMITTEXT:
                ctrl.limit = wparam
                return 0
            if msg == EM_SETSEL:
                return 0            # text selection is host-managed
            if msg == EM_GETSEL:
                return 0            # no selection tracked (start=end=0)
        raise Win16ApiGap(
            f"SendDlgItemMessage {ctrl.cls} msg {msg:#06x} not implemented "
            f"(dialog {dlg.name}, control {ctrl.ctrl_id})")

    @api.register("USER", 195, args="word ptr word word word")
    def DlgDirListComboBox(ctx: CallContext) -> int:
        # (hdlg, path_spec, combo_id, static_id, filetype) — fill the combo
        # with files matching the spec from the game's directory.
        dlg = _dialog(ctx, ctx.args[0])
        sysobj = _sys(ctx)
        spec_ptr = ctx.args[1]
        spec = ctx.read_string(spec_ptr).decode("latin-1") or "*.*"
        pattern = sysobj._canonical(spec)
        root = sysobj.file_root or sysobj.machine.exe.path.parent
        import fnmatch
        names = sorted(p.name.upper() for p in root.iterdir()
                       if p.is_file() and fnmatch.fnmatch(p.name.upper(), pattern))
        combo = dlg.control(ctx.args[2])
        combo.items = names
        combo.sel = 0 if names else -1
        host = _host(ctx)
        if host is not None:
            host.update(dlg, combo)
        if ctx.args[3]:                     # static shows the "current path"
            static = dlg.control(ctx.args[3])
            static.text = "C:\\"
            if host is not None:
                host.update(dlg, static)
        # Real USER rewrites the spec buffer to the filename part; the spec
        # already is one here.
        return 1 if names else 0

    @api.register("USER", 194, args="word ptr word")
    def DlgDirSelectComboBox(ctx: CallContext) -> int:  # (hdlg, buf, combo_id)
        dlg = _dialog(ctx, ctx.args[0])
        combo = dlg.control(ctx.args[2])
        text = (combo.items[combo.sel] if 0 <= combo.sel < len(combo.items)
                else combo.text)
        p = ctx.args[1]
        ctx.mem.load((p >> 16) & 0xFFFF, p & 0xFFFF,
                     text.encode("latin-1") + b"\x00")
        return 1 if text else 0