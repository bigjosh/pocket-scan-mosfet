"""Export a downloaded .sent/.pending binary scan and its .meta sidecar to CSV."""
import argparse
import csv
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from firmware.archive import UploadSource


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--metadata", type=Path,
                        help="also write the scan settings, provenance, outcome and fault as JSON")
    args = parser.parse_args()
    source = UploadSource(str(args.scan))
    try:
        blocks = []
        while True:
            block = source.read(1024)
            if not block:
                break
            blocks.append(block)
    finally:
        source.close()
    scan = json.loads(b"".join(blocks))
    with args.output.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(scan["columns"])
        writer.writerows(scan["points"])
    print("Exported %d measured points to %s" % (scan["point_count"], args.output))
    metadata = args.metadata
    if scan["outcome"] == "partial":
        metadata = metadata or Path(str(args.output) + ".metadata.json")
        print("Partial scan: %s %s; unfinished point excluded" %
              (scan["fault"]["channel"], scan["fault"]["kind"]))
    if metadata is not None:
        del scan["points"]
        with metadata.open("w", encoding="utf-8") as output:
            json.dump(scan, output, indent=2)
            output.write("\n")
        print("Scan metadata: %s" % metadata)


if __name__ == "__main__":
    main()
