import argparse
import csv
from pathlib import Path


def split_csv(input_path: Path) -> tuple[Path, Path, Path]:
    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    output_dir = input_path.parent
    yes_path = output_dir / f"{input_path.stem}_yes.csv"
    no_path = output_dir / f"{input_path.stem}_no.csv"
    failed_path = output_dir / f"{input_path.stem}_failed.csv"

    with input_path.open("r", encoding="utf-8-sig", newline="") as infile:
        reader = csv.DictReader(infile)
        fieldnames = reader.fieldnames

        if not fieldnames or "is_answerable" not in fieldnames:
            raise ValueError("The input CSV must contain an 'is_answerable' column.")

        rows_yes = []
        rows_no = []
        rows_failed = []
        invalid_values = []
        total_rows = 0

        for row_number, row in enumerate(reader, start=2):
            total_rows += 1
            clean_row = {field: row.get(field, "") for field in fieldnames}
            value = (clean_row.get("is_answerable") or "").strip().lower()

            if value == "yes":
                rows_yes.append(clean_row)
            elif value == "no":
                rows_no.append(clean_row)
            elif value == "failed":
                rows_failed.append(clean_row)
            else:
                invalid_values.append((row_number, clean_row.get("is_answerable")))

    if invalid_values:
        details = ", ".join(
            f"line {line}: {value!r}" for line, value in invalid_values[:10]
        )
        raise ValueError(
            "Found rows with 'is_answerable' values other than 'yes', 'no', or 'failed': "
            f"{details}"
        )

    if len(rows_yes) + len(rows_no) + len(rows_failed) != total_rows:
        raise ValueError(
            "Split validation failed: yes rows + no rows + failed rows does not match input row count."
        )

    for path, rows in (
        (yes_path, rows_yes),
        (no_path, rows_no),
        (failed_path, rows_failed),
    ):
        with path.open("w", encoding="utf-8", newline="") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return yes_path, no_path, failed_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split a CSV into two files based on the 'is_answerable' column."
    )
    parser.add_argument(
        "input_csv",
        nargs="?",
        default="outputs/full/3_is_answerable.csv",
        help="Path to the source CSV. Defaults to outputs/full/3_is_answerable.csv",
    )
    args = parser.parse_args()

    yes_path, no_path, failed_path = split_csv(Path(args.input_csv))
    print(f"Created: {yes_path}")
    print(f"Created: {no_path}")
    print(f"Created: {failed_path}")


if __name__ == "__main__":
    main()
