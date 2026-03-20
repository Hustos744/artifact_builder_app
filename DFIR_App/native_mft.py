import csv
import os
from datetime import datetime

TIME_FIELDS = (
    ("atime", 7),
    ("mtime", 8),
    ("ctime", 9),
    ("crtime", 10),
)

NOISE_SUBSTRINGS = [
    "($file_name)",
    "/winsxs/",
    "/assembly/",
    "odlsent",
    ".odl",
    "cache_data",
    "code cache",
    "service worker",
    "/packages/",
    "/windowsapps/microsoft/",
    "/program files/windowsapps/",
    "systemapps",
    "/microsoft office/",
    "/acrobat reader dc/",
]


def is_noise(file_path):
    """
    Filters out noisy lines using simple string matching.
    Returns True if the line should be SKIPPED.
    """
    path_lower = file_path.lower()
    path_normalized = path_lower.replace("\\", "/")

    for substring in NOISE_SUBSTRINGS:
        if substring in path_normalized:
            return True
    return False


def parse_timestamp(raw_value):
    raw_value = raw_value.strip()
    if not raw_value or not raw_value.isdigit():
        return None

    unix_timestamp = int(raw_value)
    if unix_timestamp <= 0:
        return None

    try:
        date_str = datetime.utcfromtimestamp(unix_timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return None
    return unix_timestamp, date_str


def parse_body_file(body_file_path, output_csv_path, update_ui_func=None):
    """
    Parses a .body file produced by MFTECmd.exe and writes a sorted timeline CSV.
    Replaces the former WSL dependency on mactime / grep / sed entirely.

    Body file format (SleuthKit / MFTECmd):
      MD5|name|inode|mode_as_string|UID|GID|size|atime|mtime|ctime|crtime
    """
    if not os.path.exists(body_file_path):
        msg = "Error: body file not found at " + body_file_path
        if update_ui_func:
            update_ui_func(msg)
        return False, msg

    headers = ["Date (UTC)", "Timestamp Type", "Size", "UID", "GID", "Inode", "Mode", "File Name"]
    rows = []

    try:
        if update_ui_func:
            update_ui_func("[*] Native mactime parsing started...")

        with open(body_file_path, "r", encoding="utf-8", errors="replace") as infile:
            for line in infile:
                parts = line.rstrip("\n").split("|")
                if len(parts) < 11:
                    continue

                name = os.path.normpath(parts[1].replace("/", os.sep))
                if is_noise(name):
                    continue

                size = parts[6]
                uid = parts[4]
                gid = parts[5]
                inode = parts[2]
                mode = parts[3]

                for timestamp_type, index in TIME_FIELDS:
                    parsed_timestamp = parse_timestamp(parts[index])
                    if not parsed_timestamp:
                        continue
                    unix_timestamp, date_str = parsed_timestamp
                    rows.append((unix_timestamp, date_str, timestamp_type, size, uid, gid, inode, mode, name))

        rows.sort(key=lambda row: (row[0], row[2], row[8]))

        with open(output_csv_path, "w", encoding="utf-8", newline="") as outfile:
            writer = csv.writer(outfile)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row[1:])

        result_msg = "Wrote " + str(len(rows)) + " rows to " + output_csv_path
        if update_ui_func:
            update_ui_func("[+] " + result_msg)
        return True, result_msg

    except Exception as exc:
        err_msg = "Exception during MFT parsing: " + str(exc)
        if update_ui_func:
            update_ui_func("[!] " + err_msg)
        return False, err_msg
