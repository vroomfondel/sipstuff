#!/usr/bin/env python3
"""Converts metadata.csv from the old 3-column format (rhasspy/piper) to the new 2-column format (piper1-gpl).

Old:  satz_0001|Raw text|Normalized text
New:  satz_0001.wav|Normalized text
"""

import argparse
import sys
from pathlib import Path


def convert(input_path: Path, output_path: Path) -> int:
    count = 0
    with input_path.open("r", encoding="utf-8") as fin, output_path.open("w", encoding="utf-8") as fout:
        for line_nr, line in enumerate(fin, 1):
            line = line.rstrip("\n\r")
            if not line:
                continue
            parts = line.split("|")
            if len(parts) == 3:
                filename, _raw, normalized = parts
            elif len(parts) == 2:
                filename, normalized = parts
            else:
                print(f"WARNING: Line {line_nr} has {len(parts)} columns, skipping: {line[:80]}", file=sys.stderr)
                continue

            if not filename.endswith(".wav"):
                filename = f"{filename}.wav"

            fout.write(f"{filename}|{normalized}\n")
            count += 1

    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="Path to the old metadata.csv (3 columns)")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output file (default: metadata_piper1.csv in the same directory)",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: {args.input} not found", file=sys.stderr)
        sys.exit(1)

    output = args.output or args.input.parent / "metadata_piper1.csv"

    if output == args.input:
        print("ERROR: Output must not be the same as input", file=sys.stderr)
        sys.exit(1)

    count = convert(args.input, output)
    print(f"{count} lines converted: {args.input} -> {output}")


if __name__ == "__main__":
    main()
