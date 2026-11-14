"""Build the aa.dbc SDO sample instance used by the column-group demo.

Each raw row is one decoded CAN frame.  The SDO frames carry a multiplexor plus
mutually exclusive payload signals, so a row contains exactly one payload per
frame.  Byte counts are the ceiling of each signal's bit length.
"""

import json
import os

import canmatrix.formats


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))


def load_fields():
    db = list(canmatrix.formats.loadp(
        os.path.join(ROOT, "tests", "files", "dbc", "aa.dbc")).values())[0]
    fields = []
    for frame in db.frames:
        if frame.name not in ("SDO_download", "SDO_upload"):
            continue
        for signal in frame.signals:
            fields.append({
                "frame": frame.name,
                "signal": signal.name,
                "bytes": (signal.size + 7) // 8,
            })
    return fields


def load_rows():
    """One row per multiplexed SDO frame variant, from aa.dbc's VAL_ table."""
    download_common = ["sdo_down_CMD", "sdo_down_IDX", "sdo_down_SUBIDX"]
    upload_common = ["sdo_state", "sdo_uo_IDX", "sdo_up_SUBIDX"]
    variants = {
        "SDO_download": [
            download_common + ["data8"],    # mux 47, 8-bit
            download_common + ["data16"],   # mux 43, 16-bit
            download_common + ["data24"],   # mux 39, 24-bit
            download_common + ["data320"],  # mux 35, 32-bit
            download_common + ["data321"],  # mux 67, 32-bit
        ],
        "SDO_upload": [
            upload_common + ["error_code"],  # mux 128
            upload_common + ["data8"],       # mux 79
            upload_common + ["data16"],      # mux 75
            upload_common + ["data24"],      # mux 71
        ],
    }
    return [{"frame": frame, "present": present}
            for frame, rows in variants.items() for present in rows]


def build():
    return {
        "header_bytes": 16,
        "width_limit": 32,
        "fields": load_fields(),
        "rows": load_rows(),
        "queries": [
            {"name": "down_header", "frequency": 5000,
             "fields": [["SDO_download", "sdo_down_CMD"],
                        ["SDO_download", "sdo_down_IDX"]]},
            {"name": "down_data8", "frequency": 2000,
             "fields": [["SDO_download", "data8"]]},
            {"name": "down_data32", "frequency": 1500,
             "fields": [["SDO_download", "data320"]]},
            {"name": "down_wide", "frequency": 300,
             "fields": [["SDO_download", "data8"],
                        ["SDO_download", "data16"]]},
            {"name": "up_header", "frequency": 4000,
             "fields": [["SDO_upload", "sdo_state"],
                        ["SDO_upload", "sdo_uo_IDX"]]},
            {"name": "up_error", "frequency": 800,
             "fields": [["SDO_upload", "error_code"]]},
            {"name": "up_data24", "frequency": 1200,
             "fields": [["SDO_upload", "data24"]]},
        ],
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
