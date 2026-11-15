from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ufoLib2 import Font
from ufoLib2.objects import Glyph


def _make_font(
    path: Path,
    glyphs: dict[str, dict[str, Any]],
) -> Path:
    font = Font()
    for name, data in glyphs.items():
        glyph = Glyph(name)
        glyph.unicodes = list(data.get("unicodes", ()))
        glyph.lib = dict(data.get("lib", {}))
        font.addGlyph(glyph)
    font.save(path, overwrite=True)
    return path


@pytest.fixture
def two_supplier_ufos(tmp_path: Path) -> tuple[Path, Path]:
    """Two UFOs exercising unicode-named and internally named suppliers."""

    left = _make_font(
        tmp_path / "left.ufo",
        {
            # Unicode-named supplier; glif unicode is present for A.
            "uni0041": {"unicodes": [0x41], "lib": {"com.example.stableId": "g-0041"}},
            "uni0042": {"unicodes": [0x42], "lib": {"com.example.stableId": "g-0042"}},
            # Two candidate designs for the same character.
            "uni0043": {"unicodes": [0x43], "lib": {"com.example.stableId": "g-0043"}},
            "u1F600": {"unicodes": [0x1F600]},
        },
    )
    right = _make_font(
        tmp_path / "right.ufo",
        {
            # Internally named supplier.
            "A": {"unicodes": [0x41], "lib": {"com.example.stableId": "g-0041"}},
            "B": {"unicodes": [0x42], "lib": {"com.example.stableId": "g-0042"}},
            "C.alt1": {"unicodes": [0x43]},
            "C.alt2": {"unicodes": [0x43]},
            # Unencoded alternate: same name family, no unicode, no stable id.
            "A.ss01": {},
            "smiley": {"unicodes": [0x1F600]},
        },
    )
    return left, right


@pytest.fixture
def conflict_ufos(tmp_path: Path) -> tuple[Path, Path]:
    """A name pointing at A while the stable id points at B."""

    left = _make_font(
        tmp_path / "left.ufo",
        {
            # Name says "A"; explicit stable id says record B.
            "A": {"unicodes": [0x41], "lib": {"com.example.stableId": "id-B"}},
            "B": {"unicodes": [0x42]},
        },
    )
    right = _make_font(
        tmp_path / "right.ufo",
        {
            "A": {"unicodes": [0x41]},
            "B": {"unicodes": [0x42], "lib": {"com.example.stableId": "id-B"}},
        },
    )
    return left, right


@pytest.fixture
def write_json(tmp_path: Path):
    def write(name: str, data: Any) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    return write
