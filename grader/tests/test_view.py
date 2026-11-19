"""The grader SDK's views: check_view, encode_view, write_view and the panel helpers."""

import copy
import inspect
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

from commonhill_grader import (
    MAX_VIEW_BYTES,
    VIEW_ENV,
    check_view,
    encode_view,
    games_panel,
    image_panel,
    is_view_run,
    svg_panel,
    write_view,
)

DATA = Path(__file__).parent / "data"
NS = 'xmlns="http://www.w3.org/2000/svg"'


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(width: int, height: int) -> bytes:
    """A real PNG, 8-bit greyscale, all black (header only past 64 x 64: the check reads no more)."""
    header = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    if width > 64 or height > 64:
        return header + chunk(b"IEND", b"")
    rows = b"".join(b"\x00" + bytes(width) for _ in range(height))
    return header + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def riff(kind: bytes, body: bytes) -> bytes:
    payload = b"WEBP" + kind + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def webp_lossless(width: int, height: int) -> bytes:
    return riff(b"VP8L", b"\x2f" + ((width - 1) | (height - 1) << 14).to_bytes(4, "little") + bytes(8))


def webp_lossy(width: int, height: int) -> bytes:
    body = b"\x10\x02\x00\x9d\x01\x2a" + struct.pack("<HH", width, height) + bytes(8)
    return riff(b"VP8 ", body)


def webp_extended(width: int, height: int) -> bytes:
    canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return riff(b"VP8X", b"\x00\x00\x00\x00" + canvas)


def game(**changes: Any) -> dict[str, Any]:
    """A small valid game: 9 x 9, 9 regions, X and O, a text piece, three frames."""
    base: dict[str, Any] = {
        "title": "Game 1: the candidate as X",
        "width": 9,
        "height": 9,
        "players": [{"name": "candidate"}, {"name": "incumbent"}],
        "pieces": {
            "X": {"shape": "cross", "player": 0},
            "O": {"shape": "ring", "player": 1},
            "n1": {"shape": "text", "label": "1"},
        },
        "regions": [{"x": 3 * (r % 3), "y": 3 * (r // 3), "w": 3, "h": 3} for r in range(9)],
        "frames": [
            {"focus": list(range(9)), "note": "X to move"},
            {"set": [[4, 4, "X"]], "focus": [4], "note": "X plays e5"},
            {"clear": [[4, 4]], "set": [[4, 4, "O"], [0, 0, "n1"]], "own": [[4, 1], [0, None]]},
        ],
        "result": {"winner": None, "note": ""},
    }
    return base | changes


def view(*panels: dict[str, Any]) -> dict[str, Any]:
    return {"schema": 1, "panels": list(panels)}


def frames(*extra: dict[str, Any]) -> dict[str, Any]:
    return view(games_panel([game(frames=[{}, *extra])]))


def refused(document: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        check_view(document)


# --- valid views ----------------------------------------------------------------------------------


def test_each_panel_kind_round_trips_through_write_view(tmp_path: Path) -> None:
    svg = (
        f'<?xml version="1.0" encoding="UTF-8"?><svg {NS} xmlns:xlink="http://www.w3.org/1999/xlink"'
        ' viewBox="0 0 10 10"><defs><linearGradient id="g"><stop offset="0" stop-color="red"/>'
        '</linearGradient><circle id="c" r="1"/></defs><!-- a comment -->'
        '<rect width="10" height="10" fill="url(#g)"/><use href="#c" x="5"/><use xlink:href="#c"/>'
        '<text x="1" y="9" font-size="2">n = 26 &amp; 1.5 \u00d7 1</text></svg>'
    )
    panels = [
        svg_panel(svg, caption="n = 26, 1.5 \u00d7 1"),
        image_panel(png(3, 2), "image/png"),
        image_panel(webp_lossless(4096, 1), "image/webp", caption="lossless"),
        image_panel(webp_lossy(640, 480), "image/webp"),
        image_panel(webp_extended(1, 4096), "image/webp"),
        games_panel([game(), game(regions=[], frames=[{}], result={"winner": 1, "note": "O wins"})], "games"),
    ]
    path = tmp_path / "view.json"
    written = write_view(panels, path=path)
    data = path.read_bytes()
    assert written == {"schema": 1, "panels": panels}
    assert data == encode_view(written)
    assert data == json.dumps(written, ensure_ascii=False, separators=(",", ":")).encode()
    assert "\u00d7".encode() in data, "UTF-8, not \\u escapes"
    parsed = json.loads(data)
    assert check_view(parsed) is parsed
    assert parsed == written
    assert [p.name for p in tmp_path.iterdir()] == ["view.json"]


def test_the_helpers_make_the_panels_of_the_format() -> None:
    assert svg_panel("<svg/>") == {"kind": "svg", "svg": "<svg/>"}
    assert image_panel(b"\x89PNG", "image/png", caption="c") == {
        "kind": "image",
        "media": "image/png",
        "data": "iVBORw==",
        "caption": "c",
    }
    assert games_panel(iter([{"title": "t"}])) == {"kind": "games", "games": [{"title": "t"}]}
    assert inspect.signature(write_view).parameters["path"].default == Path("/result/view.json")


def test_is_view_run_is_ar_view_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VIEW_ENV, raising=False)
    assert not is_view_run()
    monkeypatch.setenv(VIEW_ENV, "0")
    assert not is_view_run()
    monkeypatch.setenv(VIEW_ENV, "1")
    assert is_view_run()


def test_the_example_replays_are_valid_views() -> None:
    """The web's fixtures: an ultimate tic-tac-toe game and a Battlesnake game."""
    uttt: Any = check_view(json.loads((DATA / "view-uttt.json").read_text()))
    [ttt] = uttt["panels"][0]["games"]
    assert (ttt["width"], ttt["height"], len(ttt["regions"])) == (9, 9, 9)
    assert {f["set"][0][2] for f in ttt["frames"][1:]} == {"X", "O"}
    assert any("own" in f for f in ttt["frames"])
    assert all("focus" in f for f in ttt["frames"][:-1])
    assert 15 <= len(ttt["frames"]) - 1 <= 20
    assert ttt["result"]["winner"] == 0

    battlesnake: Any = check_view(json.loads((DATA / "view-battlesnake.json").read_text()))
    [snakes] = battlesnake["panels"][0]["games"]
    assert (snakes["width"], snakes["height"]) == (11, 11)
    assert snakes["pieces"] == {"food": {"shape": "dot"}}
    assert all(len(f["paths"]) == 2 for f in snakes["frames"][:-1])
    assert any("clear" in f for f in snakes["frames"])
    assert any("set" in f for f in snakes["frames"][1:])
    assert len(snakes["frames"]) >= 20
    assert snakes["result"]["winner"] == 0


# --- SVG ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("svg", "match"),
    [
        (f"<svg {NS}><script>alert(1)</script></svg>", "<script> element"),
        (f'<svg {NS} onload="alert(1)"/>', "'onload'.*no event handlers"),
        (f'<svg {NS}><rect onclick="x()"/></svg>', "'onclick'"),
        (f'<svg {NS}><rect style="fill:red"/></svg>', "'style'.*no inline CSS"),
        (f"<svg {NS}><style>rect {{fill: red}}</style></svg>", "<style> element"),
        (f'<svg {NS}><use href="https://example.com/a.svg#x"/></svg>', "only point inside the SVG"),
        (f'<svg {NS}><use href="data:image/svg+xml,x"/></svg>', "only point inside the SVG"),
        (
            f'<svg {NS} xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="javascript:x()"/></svg>',
            "inside",
        ),
        (f'<svg {NS}><rect fill="url(http://example.com/x)"/></svg>', r"url\(\) only as url\(#id\)"),
        (f'<svg {NS}><rect fill="URL(//example.com/x)"/></svg>', r"url\(\) only"),
        (f'<svg {NS}><rect fill="url(#a) url(#b)"/></svg>', r"url\(\) only"),
        (f'<svg {NS}><rect fill="u&#x72;l(http://x)"/></svg>', r"url\(\) only"),
        (f'<svg {NS}><rect fill="u\\72l(http://x)"/></svg>', "backslash"),
        (
            '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;">]>'
            f"<svg {NS}><text>&b;&b;&b;</text></svg>",
            "a DOCTYPE",
        ),
        (f'<!DOCTYPE svg SYSTEM "http://example.com/svg.dtd"><svg {NS}/>', "a DOCTYPE"),
        (f"<svg {NS}><text>&xxe;</text></svg>", "well-formed XML: undefined entity"),
        (f'<?xml-stylesheet href="https://example.com/x.css"?><svg {NS}/>', "a processing instruction"),
        (f"<svg {NS}><?php x ?></svg>", "a processing instruction"),
        (f'<?xml version="1.0" encoding="ISO-8859-1"?><svg {NS}/>', "encoding 'ISO-8859-1'"),
        (f"<svg {NS}><foreignObject><div/></foreignObject></svg>", "<foreignObject> element"),
        (f'<svg {NS}><image href="#a"/></svg>', "<image> element"),
        (f'<svg {NS}><a href="#a"><rect/></a></svg>', "<a> element"),
        (f'<svg {NS}><rect><animate attributeName="x"/></rect></svg>', "<animate> element"),
        (f'<svg {NS}><rect><set attributeName="x" to="1"/></rect></svg>', "<set> element"),
        (f'<svg {NS}><rect xml:space="preserve"/></svg>', "'xml:space'"),
        (f"<svg {NS}><g></svg>", "well-formed XML: mismatched tag"),
        (f"<svg {NS}/><svg {NS}/>", "well-formed XML: junk after document element"),
        ("", "an SVG document as text"),
        (f"<g {NS}/>", "must be an <svg> element, not <g>"),
        ("<svg/>", 'needs xmlns="http://www.w3.org/2000/svg"'),
        ('<svg xmlns="http://example.com/"/>', "xmlns must be"),
        (f"<svg {NS}>" + "<g>" * 64 + "</g>" * 64 + "</svg>", "more than 64 deep"),
        (f"<svg {NS}>" + "<rect/>" * 20_000 + "</svg>", "more than 20000 elements"),
        (f"<svg {NS}><text>\ud800</text></svg>", "no lone surrogates"),
    ],
)
def test_check_view_refuses_svg_outside_the_allowlist(svg: str, match: str) -> None:
    refused(view(svg_panel(svg)), match)


def test_svg_at_the_limits_is_allowed() -> None:
    check_view(view(svg_panel(f"<svg {NS}>" + "<g>" * 63 + "</g>" * 63 + "</svg>")))
    check_view(view(svg_panel(f"<svg {NS}>" + "<rect/>" * 19_999 + "</svg>")))
    check_view(
        view(svg_panel(f'<?xml version="1.0" encoding="utf-8"?>\n<svg {NS}>\n  <title>t</title>\n</svg>'))
    )


# --- images ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("panel", "match"),
    [
        (image_panel(webp_lossless(2, 2), "image/png"), "isn't a PNG image"),
        (image_panel(png(2, 2), "image/webp"), "isn't a WebP image"),
        (image_panel(b"GIF89a" + bytes(30), "image/gif"), 'media must be "image/png" or "image/webp"'),
        (image_panel(f"<svg {NS}/>".encode(), "image/png"), "isn't a PNG image"),
        (image_panel(b"", "image/png"), "isn't a PNG image"),
        (image_panel(png(4097, 1), "image/png"), "4097 x 1 pixels; each side must be 1 to 4096"),
        (image_panel(png(1, 5000), "image/png"), "1 x 5000 pixels"),
        (image_panel(png(0, 1), "image/png"), "0 x 1 pixels"),
        (image_panel(webp_lossless(4097, 7), "image/webp"), "4097 x 7 pixels"),
        (image_panel(webp_lossy(16383, 1), "image/webp"), "16383 x 1 pixels"),
        (image_panel(webp_extended(1, 1 << 24), "image/webp"), "1 x 16777216 pixels"),
        (image_panel(riff(b"VP8 ", bytes(20)), "image/webp"), "isn't a WebP image"),
        (image_panel(riff(b"ALPH", bytes(20)), "image/webp"), "isn't a WebP image"),
        ({"kind": "image", "media": "image/png", "data": "iVBORw"}, "isn't standard base64"),
        ({"kind": "image", "media": "image/png", "data": "iVBO\nRw=="}, "isn't standard base64"),
        ({"kind": "image", "media": "image/png", "data": "iVBO-w=="}, "isn't standard base64"),
        ({"kind": "image", "media": "image/png", "data": "\u00e9"}, "standard base64"),
        ({"kind": "image", "media": "image/png", "data": b"iVBORw=="}, "standard base64"),
    ],
)
def test_check_view_refuses_images_it_cant_vouch_for(panel: dict[str, Any], match: str) -> None:
    refused(view(panel), match)


# --- games ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "match"),
    [
        (frames({"set": [[9, 0, "X"]]}), r"set\[0\]: the cell \[9, 0\] is outside the 9 x 9 board"),
        (frames({"set": [[0, -1, "X"]]}), "outside the 9 x 9 board"),
        (frames({"clear": [[0, 9]]}), r"clear\[0\]: the cell \[0, 9\] is outside"),
        (frames({"clear": [[0, 0, "X"]]}), r"must be \[x, y\]"),
        (frames({"set": [[0, 0]]}), r"must be \[x, y, piece\]"),
        (frames({"set": [[0, 0, "Z"]]}), "the piece 'Z' isn't one of the game's pieces"),
        (view(games_panel([game(pieces={"t": {"shape": "text"}})])), "needs a label"),
        (view(games_panel([game(pieces={"t": {"shape": "text", "label": "abc"}})])), "1 to 2 characters"),
        (view(games_panel([game(pieces={"X": {"shape": "cross", "label": "x"}})])), 'only a "text" piece'),
        (view(games_panel([game(pieces={"X": {"shape": "star"}})])), "shape must be one of"),
        (view(games_panel([game(pieces={"X": {"shape": "dot", "player": 2}})])), "has 2 players"),
        (view(games_panel([game(pieces={"a b": {"shape": "dot"}})])), "the id 'a b'"),
        (view(games_panel([game(pieces={f"p{i}": {"shape": "dot"} for i in range(33)})])), "at most 32"),
        (frames({"paths": [{"player": 0, "cells": [[0, 0], [1, 1]]}]}), r"\[1, 1\] doesn't follow \[0, 0\]"),
        (frames({"paths": [{"player": 0, "cells": [[0, 0], [0, 2]]}]}), "neighbouring cell"),
        (frames({"paths": [{"player": 0, "cells": []}]}), "1 to 81 entries"),
        (frames({"paths": [{"player": 0, "cells": [[0, 0]] * 82}]}), "1 to 81 entries"),
        (frames({"paths": [{"player": 2, "cells": [[0, 0]]}]}), "has 2 players"),
        (frames({"paths": [{"cells": [[0, 0]]}]}), "needs 'player'"),
        (view(games_panel([game(regions=[{"x": 7, "y": 0, "w": 3, "h": 3}])])), "runs off the 9 x 9 board"),
        (view(games_panel([game(regions=[{"x": 0, "y": 8, "w": 1, "h": 2}])])), "runs off"),
        (view(games_panel([game(regions=[{"x": 0, "y": 0, "w": 0, "h": 1}])])), "w must be from 1"),
        (view(games_panel([game(regions=[{"x": 0, "y": 0, "w": 1, "h": 1}] * 82)])), "0 to 81 entries"),
        (frames({"own": [[9, 0]]}), "has 9 regions"),
        (frames({"own": [[0, 2]]}), "has 2 players"),
        (frames({"own": [[0]]}), r"must be \[region, player or null\]"),
        (frames({"focus": [9]}), "has 9 regions"),
        (view(games_panel([game(regions=[], frames=[{"focus": [0]}])])), "has no regions"),
        (frames({"focus": [4, 4]}), r"focus\[1\]: region 4 is in focus already"),
        (frames({"focus": [0] * 10}), r"focus\[1\]: region 0 is in focus already"),
        (
            frames({"paths": [{"player": 0, "cells": [[0, 0]]}, {"player": 0, "cells": [[1, 1]]}]}),
            r"paths\[1\]: player 0 has a path already",
        ),
        (frames({"paths": [{"player": 0, "cells": [[0, 0]]}] * 3}), "0 to 2 entries"),
        (
            view(games_panel([game(frames=[{"set": [[0, 0, "X"]] * 1000}] * 51)])),
            r"frames\[50\]: the game's frames make over 50000 changes",
        ),
        (frames({"note": "a\nb"}), "control character"),
        (frames({"note": "x" * 201}), "0 to 200 characters"),
        (frames({"next": 1}), "a key it can't have, 'next'"),
        (view(games_panel([game(frames=[{}] * 2001)])), "1 to 2000 entries"),
        (view(games_panel([game(frames=[])])), "1 to 2000 entries"),
        (view(games_panel([game(width=33)])), "width must be from 1 to 32"),
        (view(games_panel([game(height=0)])), "height must be from 1 to 32"),
        (view(games_panel([game(width=9.0)])), "width must be a whole number, got 9.0"),
        (view(games_panel([game(players=[])])), "1 to 4 entries"),
        (view(games_panel([game(players=[{"name": "x"}] * 5)])), "1 to 4 entries"),
        (view(games_panel([game(players=[{"name": "x" * 41}])])), "1 to 40 characters"),
        (view(games_panel([game(players=[{"name": "x", "colour": "red"}])])), "'colour'"),
        (view(games_panel([game(title="")])), "title must be 1 to 200"),
        (view(games_panel([game(result={"winner": 2, "note": ""})])), "has 2 players"),
        (view(games_panel([game(result={"winner": 0})])), "needs 'note'"),
        (view(games_panel([])), "1 to 64 entries"),
        (view(games_panel([game()] * 65)), "1 to 64 entries"),
        (view(games_panel([game(rules="x")])), "'rules'"),
    ],
)
def test_check_view_refuses_games_outside_the_limits(document: dict[str, Any], match: str) -> None:
    refused(document, match)


def test_a_path_may_stack_on_one_cell_and_fill_the_board() -> None:
    snake = [[0, 0], [0, 0], [1, 0], [1, 1], [1, 1]]
    check_view(frames({"paths": [{"player": 1, "cells": snake}]}))
    board = [[x if y % 2 == 0 else 8 - x, y] for y in range(9) for x in range(9)]
    check_view(frames({"paths": [{"player": 0, "cells": board}]}))


def test_sols_twelve_thousand_paths_in_one_frame_are_refused() -> None:
    """SolReviewer380's R2: one 1 x 1 game, one frame, 12,000 paths of player 0, inside the size cap
    (348,154 bytes) and accepted before; its board drew 36,013 DOM nodes. A frame draws at most one
    path per player now."""
    sol = view(
        games_panel(
            [
                {
                    "title": "dense",
                    "width": 1,
                    "height": 1,
                    "players": [{"name": "a"}],
                    "pieces": {},
                    "frames": [{"paths": [{"player": 0, "cells": [[0, 0]]}] * 12_000}],
                }
            ]
        )
    )
    assert len(json.dumps(sol, separators=(",", ":"))) < MAX_VIEW_BYTES
    refused(sol, r"paths must have 0 to 1 entries, got 12000")


def test_a_game_at_the_change_limit_is_allowed() -> None:
    check_view(view(games_panel([game(frames=[{"set": [[0, 0, "X"]] * 1000}] * 50)])))


@pytest.mark.parametrize(
    ("document", "match"),
    [
        ({"schema": True, "panels": [svg_panel(f"<svg {NS}/>")]}, "schema must be 1, got True"),
        (view(games_panel([game(width=True)])), "width must be a whole number, got True"),
        (frames({"set": [[True, 0, "X"]]}), "x must be a whole number, got True"),
        (frames({"clear": [[0, False]]}), "y must be a whole number, got False"),
        (frames({"own": [[False, 0]]}), "region must be the index"),
        (frames({"own": [[0, True]]}), "player must be the index"),
        (frames({"focus": [True]}), "must be the index"),
        (frames({"paths": [{"player": False, "cells": [[0, 0]]}]}), "must be the index"),
        (
            view(games_panel([game(regions=[{"x": False, "y": 0, "w": 1, "h": 1}])])),
            "x must be a whole number",
        ),
        (view(games_panel([game(pieces={"X": {"shape": "dot", "player": True}})])), "must be the index"),
        (view(games_panel([game(result={"winner": True, "note": ""})])), "must be the index"),
    ],
)
def test_check_view_refuses_booleans_as_numbers(document: dict[str, Any], match: str) -> None:
    refused(document, match)


# --- the document ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "match"),
    [
        ([], "the view must be an object"),
        ({"schema": 1}, "needs 'panels'"),
        ({"schema": 2, "panels": [svg_panel(f"<svg {NS}/>")]}, "schema must be 1"),
        ({"schema": 1, "panels": [], "extra": 1}, "a key it can't have, 'extra'"),
        (view(), "1 to 64 entries"),
        (view(*[svg_panel(f"<svg {NS}/>")] * 65), "1 to 64 entries"),
        (view({"kind": "html", "svg": "<p/>"}), 'kind must be "svg", "image" or "games"'),
        (view({"svg": "<p/>"}), "needs 'kind'"),
        (view({"kind": "svg", "svg": f"<svg {NS}/>", "media": "image/png"}), "'media'"),
        (view(svg_panel(f"<svg {NS}/>", caption="")), "caption must be 1 to 200"),
        (view(svg_panel(f"<svg {NS}/>", caption="x" * 201)), "caption must be 1 to 200"),
        (view(svg_panel(f"<svg {NS}/>", caption="a\tb")), "control character"),
        (view(svg_panel(f"<svg {NS}/>", caption="\x85")), "control character"),
        (view(svg_panel(f"<svg {NS}/>", caption="\udc00")), "no lone surrogates"),
        (view({"kind": "svg", "svg": f"<svg {NS}/>", "caption": None}), "caption must be text"),
    ],
)
def test_check_view_refuses_a_malformed_document(document: object, match: str) -> None:
    refused(document, match)


def test_a_view_over_the_size_cap_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    def padded(n: int) -> dict[str, Any]:
        return svg_panel(f"<svg {NS}><desc>{'x' * n}</desc></svg>")

    overhead = len(encode_view(view(padded(0))))
    at_cap = view(padded(MAX_VIEW_BYTES - overhead))
    assert len(encode_view(at_cap)) == MAX_VIEW_BYTES
    over = view(padded(MAX_VIEW_BYTES - overhead + 1))
    refused(over, f"{MAX_VIEW_BYTES + 1} bytes, over the {MAX_VIEW_BYTES} byte limit")
    with pytest.raises(ValueError, match="byte limit"):
        encode_view(over)
    path = tmp_path / "view.json"
    with pytest.raises(ValueError, match="byte limit"):
        write_view(over["panels"], path=path)
    assert not list(tmp_path.iterdir())


def test_check_view_doesnt_change_the_document() -> None:
    document = view(games_panel([game()]))
    before = copy.deepcopy(document)
    assert check_view(document) is document
    assert document == before
