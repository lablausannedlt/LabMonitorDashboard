"""
list_dll_exports.py
-------------------
Prints every function exported by a Windows DLL by parsing its PE header.
No third-party packages needed.

Usage:
    python list_dll_exports.py
"""

import struct
import sys
from pathlib import Path

DLL_PATH = r"C:\Program Files\IVI Foundation\VISA\Win64\Bin\TLTSP_64.dll"


def get_exports(path: str) -> list[str]:
    data = Path(path).read_bytes()

    if data[:2] != b"MZ":
        raise ValueError("Not a valid PE/DLL file")

    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        raise ValueError("No PE signature found")

    machine          = struct.unpack_from("<H", data, pe_offset + 4)[0]
    num_sections     = struct.unpack_from("<H", data, pe_offset + 6)[0]
    opt_header_size  = struct.unpack_from("<H", data, pe_offset + 20)[0]
    opt_offset       = pe_offset + 24
    is_64bit         = machine == 0x8664  # IMAGE_FILE_MACHINE_AMD64

    # Export Directory RVA is the first entry in the Data Directory table.
    # Its offset within the Optional Header differs between PE32 and PE32+.
    export_dir_rva = struct.unpack_from(
        "<I", data, opt_offset + (112 if is_64bit else 96)
    )[0]

    sections_offset = opt_offset + opt_header_size

    def rva_to_file_offset(rva: int) -> int | None:
        for i in range(num_sections):
            s      = sections_offset + i * 40
            vaddr  = struct.unpack_from("<I", data, s + 12)[0]
            vsize  = struct.unpack_from("<I", data, s + 16)[0]
            raw    = struct.unpack_from("<I", data, s + 20)[0]
            if vaddr <= rva < vaddr + max(vsize, 1):
                return raw + (rva - vaddr)
        return None

    if export_dir_rva == 0:
        return []

    exp_off = rva_to_file_offset(export_dir_rva)
    if exp_off is None:
        return []

    num_names  = struct.unpack_from("<I", data, exp_off + 24)[0]
    names_rva  = struct.unpack_from("<I", data, exp_off + 32)[0]
    names_off  = rva_to_file_offset(names_rva)

    names = []
    for i in range(num_names):
        name_rva = struct.unpack_from("<I", data, names_off + i * 4)[0]
        name_off = rva_to_file_offset(name_rva)
        if name_off is None:
            continue
        end = data.index(b"\x00", name_off)
        names.append(data[name_off:end].decode("ascii", errors="replace"))

    return sorted(names)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DLL_PATH
    try:
        exports = get_exports(path)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"DLL: {path}")
    print(f"Exported functions ({len(exports)} total):\n")
    for name in exports:
        print(f"  {name}")
