"""win16.bootimage — the data-only boot image layer (dos_re_2.0 §1a').

Game-free: the full build→load→replay round-trip needs a loaded NE, so its
end-to-end proof lives in the game-port project (simant_port's clean-room
test, run against the real binary + a recorded demo).  Asserted here: the
EXE-free program identity (raw stripped, resources kept, the raw-dependent
path fails loud), manifest schema gating, the audit's fail conditions
(bundled executable, undeclared nonzero code byte), the digest poison mask,
and the mask-range derivation from a manifest.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import types
from pathlib import Path

import pytest

from win16.bootimage import (BOOT_MANIFEST_SCHEMA, audit_boot_image,
                             independence_report, load_boot_manifest,
                             mask_ranges_from_manifest, strip_program)
from win16.ne import NEExecutable, NEHeader, Resource, Segment
from win16.vmsnap import digest


def _mini_exe(tmp_path) -> NEExecutable:
    raw = b"MZ" + bytes(62) + b"code&data" + bytes(16)
    p = tmp_path / "MINI.EXE"
    p.write_bytes(raw)
    hdr = NEHeader(ne_offset=0, linker_version=(5, 1), flags=0,
                   auto_data_seg=2, heap_size=0x100, stack_size=0x800,
                   entry_seg=1, entry_ip=0, initial_ss_seg=2, initial_sp=0,
                   segment_count=2, module_count=0, align_shift=4,
                   target_os=2)
    segs = (Segment(index=1, file_offset=64, file_length=9, min_alloc=16,
                    flags=0, relocations=()),
            Segment(index=2, file_offset=0, file_length=0, min_alloc=16,
                    flags=1, relocations=()))
    res = (Resource(type_id=2, type_name="BITMAP", res_id=7, res_name="#7",
                    flags=0, data=b"BMDATA"),)
    return NEExecutable(path=p, raw=raw, header=hdr, segments=segs,
                        modules=(), imported_names={}, entry_points=(),
                        resident_names=(), resources=res,
                        resource_name_map={})


def test_strip_program_keeps_identity_drops_bytes(tmp_path):
    exe = _mini_exe(tmp_path)
    prog = strip_program(exe)
    assert prog.raw == b""
    assert prog.path == Path("MINI.EXE")            # name only, no directory
    assert prog.header.stack_size == 0x800          # runtime header reads work
    assert prog.segments[1].alloc_size == 16
    assert prog.lookup_resource("BITMAP", 7).data == b"BMDATA"
    with pytest.raises(ValueError):                 # raw-dependent path: loud
        prog.segment_bytes(prog.segments[0])
    # And it round-trips through pickle (the boot image's program.pickle).
    again = pickle.loads(pickle.dumps(prog))
    assert again.lookup_resource("BITMAP", 7).data == b"BMDATA"


def test_manifest_schema_is_gated(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"schema": "nope"}))
    from dos_re.independence import VMlessViolation
    with pytest.raises(VMlessViolation, match="schema"):
        load_boot_manifest(tmp_path)


def _mini_boot_dir(tmp_path, exe, *, image: bytes, code_as_data=(),
                   extra_file: tuple[str, bytes] | None = None) -> Path:
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "memory.bin").write_bytes(image)
    (boot / "state.json").write_text("{}")
    (boot / "system.pickle").write_bytes(b"")
    (boot / "program.pickle").write_bytes(pickle.dumps(strip_program(exe)))
    if extra_file:
        (boot / extra_file[0]).write_bytes(extra_file[1])
    manifest = {
        "schema": BOOT_MANIFEST_SCHEMA,
        "source_exe": {"name": exe.path.name,
                       "sha256": hashlib.sha256(exe.raw).hexdigest(),
                       "size": len(exe.raw)},
        "regions": [{"ne_seg": 1, "para": 0x10, "start": 0x100, "end": 0x110,
                     "loaded": 9, "kind": "code"}],
        "poison": {"enabled": True, "poisoned_bytes": 4, "poisoned_runs": 1,
                   "code_bytes_present_after": 0,
                   "ranges": [[0x100, 4]],
                   "undeclared_bytes_zeroed": 2,
                   "undeclared_runs": [[0x106, 2]]},
        "code_as_data": {"ranges": [[s, n] for s, n in code_as_data]},
        "artifacts": {"memory": "memory.bin", "state": "state.json",
                      "system": "system.pickle", "program": "program.pickle"},
        "memory_sha256": hashlib.sha256(image).hexdigest(),
    }
    (boot / "manifest.json").write_text(json.dumps(manifest))
    # The IR that "decoded" bytes 0x100..0x104 of segment paragraph 0x10.
    ir = {"functions": {"0010:0000": {"blocks": [{"instructions": [
        {"ip": "0000", "bytes": "90909090"}]}]}}}
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    return boot


def test_audit_passes_on_clean_image(tmp_path):
    exe = _mini_exe(tmp_path)
    image = bytearray(0x200)                        # everything zeroed
    boot = _mini_boot_dir(tmp_path, exe, image=bytes(image))
    fails, info = audit_boot_image(boot, tmp_path / "ir.json")
    assert fails == []
    report = independence_report(load_boot_manifest(boot))
    assert report.endswith("EXE-independence wall: HOLDS")


def test_audit_fails_on_undeclared_nonzero_code_byte(tmp_path):
    exe = _mini_exe(tmp_path)
    image = bytearray(0x200)
    image[0x106] = 0xEB                             # surviving original byte
    boot = _mini_boot_dir(tmp_path, exe, image=bytes(image))
    fails, _ = audit_boot_image(boot, tmp_path / "ir.json")
    assert any("not declared code_as_data" in f for f in fails)


def test_audit_accepts_declared_code_as_data(tmp_path):
    exe = _mini_exe(tmp_path)
    image = bytearray(0x200)
    image[0x106] = 0xEB
    boot = _mini_boot_dir(tmp_path, exe, image=bytes(image),
                          code_as_data=[(0x106, 1)])
    fails, _ = audit_boot_image(boot, tmp_path / "ir.json")
    assert fails == []


def test_audit_fails_on_bundled_mz_and_on_renamed_exe(tmp_path):
    exe = _mini_exe(tmp_path)
    image = bytes(0x200)
    boot = _mini_boot_dir(tmp_path, exe, image=image,
                          extra_file=("innocent.dat", exe.raw))
    fails, _ = audit_boot_image(boot, tmp_path / "ir.json")
    assert any("MZ header" in f for f in fails)
    assert any("IS the source EXE" in f for f in fails)


def test_poisoned_code_byte_fails_audit(tmp_path):
    exe = _mini_exe(tmp_path)
    image = bytearray(0x200)
    image[0x101] = 0x90                             # decoded byte NOT zeroed
    boot = _mini_boot_dir(tmp_path, exe, image=bytes(image))
    fails, _ = audit_boot_image(boot, tmp_path / "ir.json")
    assert any("recovered code byte" in f for f in fails)


def test_mask_ranges_cover_poison_and_undeclared_runs(tmp_path):
    exe = _mini_exe(tmp_path)
    boot = _mini_boot_dir(tmp_path, exe, image=bytes(0x200))
    ranges = mask_ranges_from_manifest(load_boot_manifest(boot))
    assert (0x100, 4) in ranges and (0x106, 2) in ranges


def test_digest_mask_ranges_zero_the_masked_bytes():
    mem = types.SimpleNamespace(data=bytearray(b"\x00AB\x00"))
    cpu = types.SimpleNamespace(s=types.SimpleNamespace())
    # dataclasses.asdict needs a dataclass; use the real CPUState.
    from dos_re.cpu import CPUState
    cpu.s = CPUState()
    m = types.SimpleNamespace(mem=mem, cpu=cpu,
                              api=types.SimpleNamespace(services={}))
    masked = digest(m, mask_ranges=[(1, 2)])
    mem2 = types.SimpleNamespace(data=bytearray(4))
    m2 = types.SimpleNamespace(mem=mem2, cpu=cpu,
                               api=types.SimpleNamespace(services={}))
    assert masked == digest(m2)                     # mask == bytes zeroed
    assert digest(m) != masked                      # and differs unmasked
