# Symbol ledger — addresses → evidence (Paulie Python)

Addresses are NE-relative (`segN:offset`); the loader maps seg1→para 0x0100,
seg2 (DGROUP)→0x09CA under the default layout (do not hardcode the para values
in code — they come from `Win16Machine.seg_bases`).

| Address | Symbol | Evidence |
|---|---|---|
| seg1:61EA | `__astart` (MSC Win16 C startup) | NE entry point; `xor bp,bp; push bp; call far InitTask` prologue observed in boot trace 2026-07-07 |
| seg2:0000 | DGROUP instance data (16 reserved bytes) | Win16 convention; not yet observed |
