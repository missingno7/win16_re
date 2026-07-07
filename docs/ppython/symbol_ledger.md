# Symbol ledger — addresses → evidence (Paulie Python)

Addresses are NE-relative (`segN:offset`); the loader maps seg1→para 0x0100,
seg2 (DGROUP)→0x09CA under the default layout (do not hardcode the para values
in code — they come from `Win16Machine.seg_bases`).

| Address | Symbol | Evidence |
|---|---|---|
| seg1:61EA | `__astart` (MSC Win16 C startup) | NE entry point; `xor bp,bp; push bp; call far InitTask` prologue observed in boot trace 2026-07-07 |
| seg1:5EB0 | `WinMain` | near-called from the seg1:0033 thunk after argv setup; first body call is LoadCursor at 5EF9 (window-class setup pattern) |
| seg1:8310 | FP error handler (app side) | registered via `__fpMath` BX=3 with DX:AX=seg1:8310 during crt0 |
| seg1:629E | crt0 FP/emulator init helper | wraps DOS3Call AH=35h/25h vector saves + `__fpMath` BX=0/BX=3 calls |
| seg2:0078 | crt0: saved PSP segment | `mov ds:[0078],es` after InitTask |
| seg2:0042..004C | crt0: stack limit / hPrev / hInst / cmdline off / PSP / nCmdShow | stores of CX,SI,DI,BX,ES,DX after InitTask |
| seg2:007A | crt0: Windows version (GetVersion AX) | `mov [007A],ax` |
| seg2:007C | crt0: DOS version (DOS3Call AH=30h AX) | `mov [007C],ax` |
| seg2:0324 | import slot: `__fpMath` far pointer | `call far ds:[0324]` |
| seg2:0000 | DGROUP instance data (16 reserved bytes) | Win16 convention; stack words at 0x0A/0x0C/0x0E written by our InitTask |
