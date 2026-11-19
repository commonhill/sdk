"""`/result/view.json`, schema 1: what a project shows of a check besides its numbers (since the
SDK's release 7). The web draws it itself; no code of the project's runs in a browser.

A project with views gets a *view run* after a check's measured runs: the candidate as the worker
(for head-to-head, the incumbent as its opponent), the public sample at `/eval`, `AR_SEED` empty
(so `seed()` is `PUBLIC_SEED`) and `AR_VIEW=1` (`is_view_run()`). Only its view.json is read; its
result never counts. So a view shows the public sample only, never the held-out set: anyone who
can see the attempt sees its view, and a measured run's instances are the held-out set. It is the
same run as an agent's local one, so a deterministic project's view is the same there.

    {"schema": 1, "panels": [                                1 to 64 panels
      {"kind": "svg", "svg": "<svg xmlns=...>...</svg>", "caption": "n = 26"},
      {"kind": "image", "media": "image/png", "data": "<base64>"},       PNG or WebP
      {"kind": "games", "games": [Game, ...]}]}                         board replays

A caption (optional, every kind) is 1 to 200 characters. An SVG is checked by refusal, never
rewritten: expat parses it, with no DOCTYPE, entity or processing instruction, at most
`MAX_SVG_ELEMENTS` elements `MAX_SVG_DEPTH` deep, only `SVG_ELEMENTS` and `SVG_ATTRIBUTES`
(drawing, no style, scripts, event handlers or embedded content), and `href` and `url(...)` only
to an `#id` inside it. An image's width and height (from its header) are 1 to `MAX_IMAGE_SIDE`.

A game is a board replay (ultimate tic-tac-toe, Battlesnake):

    {"title": "Game 3: the candidate as X", "width": 9, "height": 9,       1 to 32; y down
     "players": [{"name": "candidate"}, {"name": "incumbent"}],            1 to 4
     "pieces": {"X": {"shape": "cross", "player": 0}, "n": {"shape": "text", "label": "1"}},
     "regions": [{"x": 0, "y": 0, "w": 3, "h": 3}, ...],                   optional, at most 81
     "frames": [{}, {"set": [[4, 4, "X"]], "focus": [4], "note": "X plays e5"}, ...],
     "result": {"winner": 0, "note": "X wins on the big board"}}           optional; winner null: none

A frame's `clear` ([[x, y], ...]), then `set` ([[x, y, piece], ...]) and `own` ([[region,
player or null], ...]) change the board from the frame before; its `paths` ([{"player": 0,
"cells": [[x, y], ...]}], head first, each step to a neighbour or the same cell), `focus`
([region, ...]) and `note` are its own. Frame 0 is the starting position; at most `MAX_FRAMES`.
The work a replay takes to draw is bounded (SolReviewer380's R2): a frame draws at most one path
per player and each region's focus at most once, and a game's frames change cells and regions at
most `MAX_GAME_CHANGES` times in all (a frame's board is the fold of the changes before it).

`check_view` is the one definition of a valid view, which `write_view` and the platform share:
the whole document, canonically encoded (`encode_view`), is at most `MAX_VIEW_BYTES`. Numbers
are whole numbers (no booleans, no floats); text has no control characters.
"""

import base64
import binascii
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from xml.parsers import expat

from commonhill_grader.result import (
    RESULT_PATH,
    _write_atomically,  # pyright: ignore[reportPrivateUsage]
)

VIEW_SCHEMA = 1
VIEW_FILE = "view.json"  # beside result.json
VIEW_ENV = "AR_VIEW"
# The runner reads at most this much of view.json; the platform keeps it whole.
MAX_VIEW_BYTES = 512 * 1024
MAX_PANELS = 64
MAX_CAPTION = 200
MAX_IMAGE_SIDE = 4096
MAX_GAMES = 64
MAX_SIDE = 32
MAX_PLAYERS = 4
MAX_PLAYER_NAME = 40
MAX_PIECES = 32
MAX_REGIONS = 81
MAX_FRAMES = 2000
MAX_GAME_CHANGES = 50_000  # a game's `clear`, `set` and `own` entries, all frames together
MAX_TITLE = 200
MAX_NOTE = 200
MAX_SVG_ELEMENTS = 20_000
MAX_SVG_DEPTH = 64

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"
SVG_ELEMENTS = frozenset(
    [
        "svg",
        "g",
        "defs",
        "title",
        "desc",
        "path",
        "circle",
        "ellipse",
        "rect",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "linearGradient",
        "radialGradient",
        "stop",
        "clipPath",
        "use",
    ]
)
SVG_ATTRIBUTES = frozenset(
    [
        "id",
        "class",
        "transform",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "width",
        "height",
        "d",
        "points",
        "viewBox",
        "preserveAspectRatio",
        "fill",
        "fill-opacity",
        "fill-rule",
        "stroke",
        "stroke-width",
        "stroke-opacity",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-dasharray",
        "stroke-dashoffset",
        "opacity",
        "font-size",
        "font-family",
        "font-weight",
        "text-anchor",
        "dominant-baseline",
        "offset",
        "stop-color",
        "stop-opacity",
        "gradientUnits",
        "gradientTransform",
        "clip-path",
        "clip-rule",
        "clipPathUnits",
        "xmlns",
        "href",
        "xmlns:xlink",
        "xlink:href",
    ]
)
_LOCAL_HREF = re.compile(r"#[A-Za-z_][\w.-]*", re.ASCII)
_LOCAL_URL = re.compile(r"url\(#[A-Za-z_][\w.-]*\)", re.ASCII)

SHAPES = ("cross", "ring", "dot", "square", "shade", "text")
PIECE_ID = re.compile(r"[A-Za-z0-9_-]{1,16}", re.ASCII)
_FRAME_KEYS = ("clear", "set", "own", "paths", "focus", "note")

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_PNG = b"\x89PNG\r\n\x1a\n"


def is_view_run() -> bool:
    """Whether this run is the platform's view run (`AR_VIEW=1`): write the view, on the sample."""
    return os.environ.get(VIEW_ENV) == "1"


def _short(value: object) -> str:
    if isinstance(value, list | tuple):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() >= 64:
        return "a huge integer"
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _text(what: str, value: object, low: int, high: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text, got {_short(value)}")
    if not low <= len(value) <= high:
        raise ValueError(f"{what} must be {low} to {high} characters long, got {len(value)}")
    if _CONTROL.search(value):
        raise ValueError(f"{what} has a control character (a line break, say) in it")
    if _SURROGATE.search(value):
        raise ValueError(f"{what} must be valid Unicode text (no lone surrogates)")
    return value


def _int(what: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be a whole number, got {_short(value)}")
    if not low <= value <= high:
        raise ValueError(f"{what} must be from {low} to {high}, got {_short(value)}")
    return value


def _index(what: str, value: object, count: int, things: str) -> int:
    """`value` as an index into the game's `count` `things` (players, regions)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be the index of one of the game's {things}, got {_short(value)}")
    if not 0 <= value < count:
        has = f"no {things}" if count == 0 else f"{count} {things} (0 to {count - 1})"
        raise ValueError(f"{what} is {_short(value)}, but the game has {has}")
    return value


def _list(what: str, value: object, low: int, high: int | None = None) -> Sequence[object]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{what} must be a list, got {_short(value)}")
    items = cast(Sequence[object], value)
    if len(items) < low or (high is not None and len(items) > high):
        bound = f"at least {low}" if high is None else f"{low} to {high}"
        raise ValueError(f"{what} must have {bound} entries, got {len(items)}")
    return items


def _object(
    what: str, value: object, required: Sequence[str], optional: Sequence[str] = ()
) -> dict[str, object]:
    """`value` as an object with all the `required` keys and no others but the `optional` ones."""
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be an object, got {_short(value)}")
    fields = cast(dict[object, object], value)
    for key in fields:
        if key not in required and key not in optional:
            allowed = ", ".join(repr(k) for k in (*required, *optional)) or "none"
            raise ValueError(f"{what} has a key it can't have, {_short(key)} (it may have: {allowed})")
    for key in required:
        if key not in fields:
            raise ValueError(f"{what} needs {key!r}")
    return cast(dict[str, object], fields)


def _caption(what: str, panel: dict[str, object]) -> None:
    if "caption" in panel:
        _text(f"{what}.caption", panel["caption"], 1, MAX_CAPTION)


# --- SVG ------------------------------------------------------------------------------------------


def _svg_attribute(what: str, element: str, name: str, value: str) -> None:
    if name not in SVG_ATTRIBUTES:
        why = (
            "; no event handlers"
            if name.lower().startswith("on")
            else "; no inline CSS (use presentation attributes like fill)"
            if name == "style"
            else ""
        )
        raise ValueError(
            f"{what}: <{element}> has the attribute {name!r}, which a view's SVG can't have{why}"
        )
    if name == "xmlns" and value != SVG_NAMESPACE:
        raise ValueError(f"{what}: xmlns must be {SVG_NAMESPACE!r}, got {_short(value)}")
    if name == "xmlns:xlink" and value != XLINK_NAMESPACE:
        raise ValueError(f"{what}: xmlns:xlink must be {XLINK_NAMESPACE!r}, got {_short(value)}")
    if name in ("href", "xlink:href") and not _LOCAL_HREF.fullmatch(value):
        raise ValueError(
            f"{what}: <{element}> {name} may only point inside the SVG (#id), got {_short(value)}"
        )
    if "\\" in value:
        raise ValueError(f"{what}: <{element}> {name} has a backslash in it (a CSS escape)")
    if "url(" in value.lower() and not _LOCAL_URL.fullmatch(value):
        raise ValueError(
            f"{what}: <{element}> {name} may use url() only as url(#id), a reference inside the SVG,"
            f" got {_short(value)}"
        )


def _svg(what: str, svg: object) -> None:
    """Refuse `svg` unless it is an SVG document of only the allowed elements and attributes."""
    if not isinstance(svg, str) or not svg:
        raise ValueError(f"{what} must be an SVG document as text, got {_short(svg)}")
    if _SURROGATE.search(svg):
        raise ValueError(f"{what} must be valid Unicode text (no lone surrogates)")
    parser = expat.ParserCreate()
    elements = depth = 0

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal elements, depth
        elements += 1
        depth += 1
        if elements == 1 and name != "svg":
            raise ValueError(f"{what} must be an <svg> element, not <{name}>")
        if name not in SVG_ELEMENTS:
            raise ValueError(f"{what} has a <{name}> element, which a view's SVG can't have")
        if elements > MAX_SVG_ELEMENTS:
            raise ValueError(f"{what} has more than {MAX_SVG_ELEMENTS} elements")
        if depth > MAX_SVG_DEPTH:
            raise ValueError(f"{what} nests elements more than {MAX_SVG_DEPTH} deep")
        for attribute, value in attributes.items():
            _svg_attribute(what, name, attribute, value)
        if elements == 1 and "xmlns" not in attributes:
            raise ValueError(
                f'{what}: the <svg> needs xmlns="{SVG_NAMESPACE}" (a browser shows nothing without it)'
            )

    def end(name: str) -> None:
        nonlocal depth
        depth -= 1

    def refuse(thing: str) -> Any:
        def handler(*_: object) -> None:
            raise ValueError(f"{what} has {thing}, which a view's SVG can't have")

        return handler

    def declaration(version: str, encoding: str | None, standalone: int) -> None:
        if encoding is not None and encoding.lower() not in ("utf-8", "utf8"):
            raise ValueError(f"{what} declares the encoding {encoding!r}; a view's SVG is UTF-8")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = refuse("a DOCTYPE")
    parser.EntityDeclHandler = refuse("an entity declaration")
    parser.ProcessingInstructionHandler = refuse("a processing instruction")
    parser.XmlDeclHandler = declaration
    try:
        parser.Parse(svg, True)
    except expat.ExpatError as exc:
        reason = expat.ErrorString(exc.code)
        raise ValueError(
            f"{what} isn't well-formed XML: {reason} (line {exc.lineno}, column {exc.offset})"
        ) from None


# --- images ---------------------------------------------------------------------------------------


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != _PNG or data[8:16] != b"\x00\x00\x00\x0dIHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":  # a key frame's start code
        return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:  # 14 bits each, minus one
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(data) >= 30:  # the canvas, 24 bits each, minus one
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    return None


_IMAGES = {
    "image/png": ("a PNG image (its signature, then its IHDR chunk)", _png_size),
    "image/webp": ("a WebP image (RIFF, WEBP, then a VP8, VP8L or VP8X chunk)", _webp_size),
}


def _image(what: str, media: object, data: object) -> None:
    if not isinstance(media, str) or media not in _IMAGES:
        raise ValueError(f'{what}.media must be "image/png" or "image/webp", got {_short(media)}')
    if not isinstance(data, str) or not data.isascii():
        raise ValueError(f"{what}.data must be the image in standard base64, got {_short(data)}")
    try:
        raw = binascii.a2b_base64(data, strict_mode=True)
    except binascii.Error:
        raise ValueError(f"{what}.data isn't standard base64 (A-Z a-z 0-9 + /, padded with =)") from None
    kind, size = _IMAGES[media]
    measured = size(raw)
    if measured is None:
        raise ValueError(f"{what}.data isn't {kind}")
    width, height = measured
    if not (1 <= width <= MAX_IMAGE_SIDE and 1 <= height <= MAX_IMAGE_SIDE):
        raise ValueError(f"{what} is {width} x {height} pixels; each side must be 1 to {MAX_IMAGE_SIDE}")


# --- games ----------------------------------------------------------------------------------------


def _entry(what: str, value: object, size: int, shape: str) -> Sequence[object]:
    """`value` as a list of exactly `size` things, `shape` in words."""
    entry = _sequence(value)
    if entry is None or len(entry) != size:
        raise ValueError(f"{what} must be {shape}, got {_short(value)}")
    return entry


def _sequence(value: object) -> Sequence[object] | None:
    return cast(Sequence[object], value) if isinstance(value, list | tuple) else None


def _cell(what: str, value: object, width: int, height: int, *, piece: bool = False) -> tuple[int, int]:
    x, y = _entry(what, value, 3, "[x, y, piece]")[:2] if piece else _entry(what, value, 2, "[x, y]")
    for axis, number in (("x", x), ("y", y)):
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(f"{what}: {axis} must be a whole number, got {_short(number)}")
    x, y = cast(int, x), cast(int, y)
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError(
            f"{what}: the cell [{_short(x)}, {_short(y)}] is outside the {width} x {height} board"
            f" (x 0 to {width - 1}, y 0 to {height - 1})"
        )
    return x, y


def _piece(what: str, value: object, players: int) -> None:
    piece = _object(what, value, ("shape",), ("player", "label"))
    shape = piece["shape"]
    if shape not in SHAPES:
        raise ValueError(f"{what}.shape must be one of {', '.join(SHAPES)}, got {_short(shape)}")
    if "player" in piece:
        _index(f"{what}.player", piece["player"], players, "players")
    if shape == "text":
        if "label" not in piece:
            raise ValueError(f'{what} is a "text" piece, so it needs a label (1 or 2 characters)')
        _text(f"{what}.label", piece["label"], 1, 2)
    elif "label" in piece:
        raise ValueError(f'{what} has a label, which only a "text" piece can have')


@dataclass(frozen=True)
class _Game:
    """What a game's frames are checked against: its board, and how many players and regions."""

    width: int
    height: int
    players: int
    pieces: frozenset[str]
    regions: int


def _frame(what: str, value: object, game: _Game) -> int:
    """Check frame `value`; how many changes (`clear`, `set` and `own` entries) it makes."""
    frame = _object(what, value, (), _FRAME_KEYS)
    width, height = game.width, game.height
    clears = _list(f"{what}.clear", frame.get("clear", []), 0)
    for i, cell in enumerate(clears):
        _cell(f"{what}.clear[{i}]", cell, width, height)
    sets = _list(f"{what}.set", frame.get("set", []), 0)
    for i, entry in enumerate(sets):
        piece = _entry(f"{what}.set[{i}]", entry, 3, "[x, y, piece]")[2]
        _cell(f"{what}.set[{i}]", entry, width, height, piece=True)
        if not isinstance(piece, str) or piece not in game.pieces:
            raise ValueError(f"{what}.set[{i}]: the piece {_short(piece)} isn't one of the game's pieces")
    owns = _list(f"{what}.own", frame.get("own", []), 0)
    for i, entry in enumerate(owns):
        region, player = _entry(f"{what}.own[{i}]", entry, 2, "[region, player or null]")
        _index(f"{what}.own[{i}]'s region", region, game.regions, "regions")
        if player is not None:
            _index(f"{what}.own[{i}]'s player", player, game.players, "players")
    drawn: set[object] = set()
    for i, entry in enumerate(_list(f"{what}.paths", frame.get("paths", []), 0, game.players)):
        path = _object(f"{what}.paths[{i}]", entry, ("player", "cells"))
        _index(f"{what}.paths[{i}].player", path["player"], game.players, "players")
        if path["player"] in drawn:
            raise ValueError(f"{what}.paths[{i}]: player {path['player']} has a path already; one each")
        drawn.add(path["player"])
        cells = _list(f"{what}.paths[{i}].cells", path["cells"], 1, width * height)
        previous: tuple[int, int] | None = None
        for j, cell in enumerate(cells):
            x, y = _cell(f"{what}.paths[{i}].cells[{j}]", cell, width, height)
            if previous is not None and abs(x - previous[0]) + abs(y - previous[1]) > 1:
                raise ValueError(
                    f"{what}.paths[{i}].cells[{j}]: [{x}, {y}] doesn't follow [{previous[0]}, {previous[1]}];"
                    " each step is to a neighbouring cell (up, down, left, right) or the same one"
                )
            previous = x, y
    focused: set[object] = set()
    for i, region in enumerate(_list(f"{what}.focus", frame.get("focus", []), 0)):
        _index(f"{what}.focus[{i}]", region, game.regions, "regions")
        if region in focused:
            raise ValueError(f"{what}.focus[{i}]: region {region} is in focus already; once each")
        focused.add(region)
    if "note" in frame:
        _text(f"{what}.note", frame["note"], 0, MAX_NOTE)
    return len(clears) + len(sets) + len(owns)


def _game(what: str, value: object) -> None:
    game = _object(
        what, value, ("title", "width", "height", "players", "pieces", "frames"), ("regions", "result")
    )
    _text(f"{what}.title", game["title"], 1, MAX_TITLE)
    width = _int(f"{what}.width", game["width"], 1, MAX_SIDE)
    height = _int(f"{what}.height", game["height"], 1, MAX_SIDE)
    players = _list(f"{what}.players", game["players"], 1, MAX_PLAYERS)
    for i, entry in enumerate(players):
        player = _object(f"{what}.players[{i}]", entry, ("name",))
        _text(f"{what}.players[{i}].name", player["name"], 1, MAX_PLAYER_NAME)
    pieces = game["pieces"]
    if not isinstance(pieces, dict):
        raise ValueError(f"{what}.pieces must be an object of pieces by id, got {_short(pieces)}")
    by_id = cast(dict[object, object], pieces)
    if len(by_id) > MAX_PIECES:
        raise ValueError(f"{what}.pieces has {len(by_id)} pieces; at most {MAX_PIECES}")
    for piece_id, piece in by_id.items():
        if not isinstance(piece_id, str) or not PIECE_ID.fullmatch(piece_id):
            raise ValueError(f"{what}.pieces: the id {_short(piece_id)} must be 1 to 16 of A-Z a-z 0-9 _ -")
        _piece(f"{what}.pieces[{piece_id!r}]", piece, len(players))
    regions = _list(f"{what}.regions", game.get("regions", []), 0, MAX_REGIONS)
    for i, entry in enumerate(regions):
        region = _object(f"{what}.regions[{i}]", entry, ("x", "y", "w", "h"))
        x = _int(f"{what}.regions[{i}].x", region["x"], 0, MAX_SIDE)
        y = _int(f"{what}.regions[{i}].y", region["y"], 0, MAX_SIDE)
        w = _int(f"{what}.regions[{i}].w", region["w"], 1, MAX_SIDE)
        h = _int(f"{what}.regions[{i}].h", region["h"], 1, MAX_SIDE)
        if x + w > width or y + h > height:
            raise ValueError(
                f"{what}.regions[{i}] ({w} x {h} at [{x}, {y}]) runs off the {width} x {height} board"
            )
    board = _Game(width, height, len(players), frozenset(cast(dict[str, object], by_id)), len(regions))
    changes = 0
    for i, frame in enumerate(_list(f"{what}.frames", game["frames"], 1, MAX_FRAMES)):
        changes += _frame(f"{what}.frames[{i}]", frame, board)
        if changes > MAX_GAME_CHANGES:
            raise ValueError(
                f"{what}.frames[{i}]: the game's frames make over {MAX_GAME_CHANGES} changes (clear, set"
                " and own entries) in all"
            )
    if "result" in game:
        result = _object(f"{what}.result", game["result"], ("winner", "note"))
        if result["winner"] is not None:
            _index(f"{what}.result.winner", result["winner"], len(players), "players")
        _text(f"{what}.result.note", result["note"], 0, MAX_NOTE)


# --- the document ---------------------------------------------------------------------------------


def _kind(panel: object) -> object:
    return cast(dict[object, object], panel).get("kind") if isinstance(panel, dict) else None


def _panel(what: str, value: object) -> None:
    kind = _kind(value)
    if kind == "svg":
        panel = _object(what, value, ("kind", "svg"), ("caption",))
        _svg(f"{what}.svg", panel["svg"])
    elif kind == "image":
        panel = _object(what, value, ("kind", "media", "data"), ("caption",))
        _image(what, panel["media"], panel["data"])
    elif kind == "games":
        panel = _object(what, value, ("kind", "games"), ("caption",))
        for i, game in enumerate(_list(f"{what}.games", panel["games"], 1, MAX_GAMES)):
            _game(f"{what}.games[{i}]", game)
    else:
        _object(what, value, ("kind",), ("svg", "media", "data", "games", "caption"))
        raise ValueError(f'{what}.kind must be "svg", "image" or "games", got {_short(kind)}')
    _caption(what, panel)


def _encode(view: object) -> bytes:
    """`view`'s canonical bytes, after checking it whole."""
    try:
        document = _object("the view", view, ("schema", "panels"))
        schema = document["schema"]
        if isinstance(schema, bool) or schema != VIEW_SCHEMA:
            raise ValueError(f"the view's schema must be {VIEW_SCHEMA}, got {_short(schema)}")
        for i, panel in enumerate(_list("the view's panels", document["panels"], 1, MAX_PANELS)):
            _panel(f"panels[{i}]", panel)
        data = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    except RecursionError:
        raise ValueError("the view is nested too deeply") from None
    if len(data) > MAX_VIEW_BYTES:
        raise ValueError(f"the view is {len(data)} bytes, over the {MAX_VIEW_BYTES} byte limit")
    return data


def check_view(view: object) -> dict[str, object]:
    """`view` (a parsed view.json) as given, if it is a valid schema-1 view; else ValueError,
    saying what is wrong. The check `write_view` and the platform share."""
    _encode(view)
    return cast(dict[str, object], view)


def encode_view(view: object) -> bytes:
    """`view` as view.json's bytes (compact UTF-8 JSON), after `check_view`."""
    return _encode(view)


def write_view(
    panels: Iterable[dict[str, Any]], path: Path = RESULT_PATH.parent / VIEW_FILE
) -> dict[str, object]:
    """Write view.json (schema 1) of `panels` to `path` atomically; returns what was written.
    ValueError, before anything is written, if it isn't a valid view (`check_view`)."""
    view: dict[str, object] = {"schema": VIEW_SCHEMA, "panels": list(panels)}
    _write_atomically(path, encode_view(view))
    return view


def _captioned(caption: str | None) -> dict[str, object]:
    return {} if caption is None else {"caption": caption}


def svg_panel(svg: str, caption: str | None = None) -> dict[str, object]:
    """A panel showing `svg`, an SVG document (`<svg xmlns="http://www.w3.org/2000/svg" ...>`)."""
    return {"kind": "svg", "svg": svg, **_captioned(caption)}


def image_panel(data: bytes, media: str, caption: str | None = None) -> dict[str, object]:
    """A panel showing `data`, a PNG ("image/png") or WebP ("image/webp") image's bytes."""
    return {
        "kind": "image",
        "media": media,
        "data": base64.b64encode(data).decode("ascii"),
        **_captioned(caption),
    }


def games_panel(games: Iterable[dict[str, Any]], caption: str | None = None) -> dict[str, object]:
    """A panel of board replays (see the module's docstring for a game)."""
    return {"kind": "games", "games": list(games), **_captioned(caption)}
