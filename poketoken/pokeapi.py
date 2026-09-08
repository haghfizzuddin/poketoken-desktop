"""PokéAPI client with a disk cache — port of `PokeAPIClient.swift` + `SpriteLoader.swift`.

Species and evolution-chain data are static, so they are cached forever. The base-species
index (hatch candidates: every Gen 1-5 species that does not evolve from anything) comes
from the GraphQL endpoint with a 30-day TTL and a REST rejection-sampling fallback.
"""
from __future__ import annotations

import http.client
import json
import os
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REST = "https://pokeapi.co/api/v2"
GRAPHQL = "https://graphql.pokeapi.co/v1beta2"
SPRITE_BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon"
ITEM_BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/items"
ANIMATED_IDS = range(1, 650)          # Gen-V animated assets exist for #1..#649 only
DITTO_ID = 132                        # reserved for the (unported) disguise mechanic
LANG_CODES = ("ko", "en", "ja-Hrkt", "ja", "es", "fr", "pt", "de")
TYPES = ("normal", "fire", "water", "electric", "grass", "ice", "fighting", "poison", "ground", "flying",
         "psychic", "bug", "rock", "ghost", "dragon", "dark", "steel", "fairy")
LANG_FALLBACK = {"ko": ["ko"], "en": ["en"], "ja": ["ja-Hrkt", "ja"], "es": ["es"],
                 "fr": ["fr"], "pt": ["pt"], "de": ["de"]}
BASE_INDEX_TTL = 30 * 86400
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) poketoken/0.1 (PokeTokenBar port)"


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """Connect over IPv4 only. On WSL2 the IPv6 route is often black-holed, and urllib
    would burn one full timeout per AAAA record before falling back to A."""

    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
        if not infos:
            raise OSError(f"no IPv4 address for {self.host}")
        err: OSError | None = None
        sock = None
        for family, kind, proto, _, addr in infos:
            sock = socket.socket(family, kind, proto)
            sock.settimeout(self.timeout)
            try:
                sock.connect(addr)
                err = None
                break
            except OSError as e:
                err = e
                sock.close()
                sock = None
        if sock is None:
            raise err or OSError("connect failed")
        self.sock = sock
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_IPv4HTTPSConnection, req, context=self._context)


def _build_opener() -> urllib.request.OpenerDirector:
    if os.environ.get("POKETOKEN_ALLOW_IPV6", "").strip():
        return urllib.request.build_opener()
    return urllib.request.build_opener(_IPv4HTTPSHandler())

RARITIES = ("common", "uncommon", "rare", "legendary")
CAPTURE_CEILING = {"rare": 45, "uncommon": 120, "common": 255}


def rarity_rank(r: str) -> int:
    return RARITIES.index(r)


def rarity_includes(tier: str, capture_rate: int) -> bool:
    """capture_rate <= ceiling means the species is *at least* this tier (legendary: never)."""
    c = CAPTURE_CEILING.get(tier)
    return c is not None and capture_rate <= c


def rarity_from(capture_rate: int, is_legendary: bool, is_mythical: bool) -> str:
    if is_legendary or is_mythical:
        return "legendary"
    if rarity_includes("rare", capture_rate):
        return "rare"
    if rarity_includes("uncommon", capture_rate):
        return "uncommon"
    return "common"


class PokeAPIError(Exception):
    pass


@dataclass
class EvoNode:
    species_id: int
    children: list["EvoNode"] = field(default_factory=list)

    @property
    def depth(self) -> int:
        return 1 + (max((c.depth for c in self.children), default=0))

    def find(self, sid: int) -> "EvoNode | None":
        if self.species_id == sid:
            return self
        for c in self.children:
            f = c.find(sid)
            if f is not None:
                return f
        return None

    @property
    def final_ids(self) -> list[int]:
        return [self.species_id] if not self.children else [f for c in self.children for f in c.final_ids]

    def all_ids(self) -> list[int]:
        return [self.species_id] + [i for c in self.children for i in c.all_ids()]

    def path_through(self, sid: int, prefer=lambda _sid: False) -> list[int]:
        """Root-to-leaf path passing through `sid`: every form above it, then one branch below —
        the child whose subtree `prefer` says yes to (an owned form), else the first. Empty when
        `sid` is not in this tree."""
        if self.find(sid) is None:
            return []
        above: list[int] = []
        cur = self
        while cur.species_id != sid:
            above.append(cur.species_id)
            cur = next(c for c in cur.children if c.find(sid) is not None)
        below: list[int] = []
        while cur.children:
            cur = next((c for c in cur.children if any(prefer(i) for i in c.all_ids())), cur.children[0])
            below.append(cur.species_id)
        return above + [sid] + below

    def keeping_animated(self) -> "EvoNode | None":
        if self.species_id not in ANIMATED_IDS:
            return None
        kept = [k for k in (c.keeping_animated() for c in self.children) if k is not None]
        return EvoNode(self.species_id, kept)

    def to_dict(self) -> dict:
        return {"speciesID": self.species_id, "children": [c.to_dict() for c in self.children]}

    @staticmethod
    def from_dict(d: dict) -> "EvoNode":
        return EvoNode(int(d["speciesID"]), [EvoNode.from_dict(c) for c in d.get("children", [])])


@dataclass
class EvoLine:
    base_id: int
    tree: EvoNode
    rarity: str
    names: dict[int, dict[str, str]]

    def name(self, sid: int, lang: str = "en") -> str:
        by = self.names.get(sid, {})
        for code in LANG_FALLBACK.get(lang, ["en"]):
            if code in by:
                return by[code]
        return by.get("en") or f"#{sid}"

    def to_dict(self) -> dict:
        return {"baseID": self.base_id, "tree": self.tree.to_dict(), "rarity": self.rarity,
                "names": {str(k): v for k, v in self.names.items()}}

    @staticmethod
    def from_dict(d: dict) -> "EvoLine":
        return EvoLine(int(d["baseID"]), EvoNode.from_dict(d["tree"]), d["rarity"],
                       {int(k): dict(v) for k, v in d.get("names", {}).items()})


def _species_id_from_url(url: str) -> int:
    parts = [p for p in url.split("/") if p]
    try:
        return int(parts[-1])
    except (IndexError, ValueError):
        return 0


class PokeAPI:
    def __init__(self, cache_dir: Path, sprite_dir: Path, timeout: float = 15.0):
        self.cache_dir = Path(cache_dir)
        self.sprite_dir = Path(sprite_dir)
        self.timeout = timeout
        for sub in ("species", "lines", "pokemon"):
            (self.cache_dir / sub).mkdir(parents=True, exist_ok=True)
        self.sprite_dir.mkdir(parents=True, exist_ok=True)
        self._species: dict[int, dict] = {}
        self._pokemon: dict[int, dict] = {}
        self._type_chart: dict[str, dict[str, float]] | None = None
        self._lines: dict[int, EvoLine] = {}
        self._base_index: list[tuple[int, int]] | None = None
        self._wild_index: list[tuple[int, int]] | None = None
        self._opener = _build_opener()

    # ------------------------------------------------------------------ http
    def _http(self, url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    raise PokeAPIError(f"HTTP {resp.status} for {url}")
                return resp.read()
        except urllib.error.HTTPError as e:
            raise PokeAPIError(f"HTTP {e.code} for {url}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PokeAPIError(f"network error for {url}: {e}") from e

    def _get_json(self, url: str) -> dict:
        try:
            return json.loads(self._http(url))
        except ValueError as e:
            raise PokeAPIError(f"bad JSON from {url}") from e

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_json(path: Path, obj) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), "utf-8")
        tmp.replace(path)

    # --------------------------------------------------------------- species
    def species(self, sid: int) -> dict:
        """Slimmed `pokemon-species/{id}`: id, name, capture_rate, is_legendary, is_mythical,
        evolves_from (id|None), chain_url, names {lang: name}."""
        if sid in self._species:
            return self._species[sid]
        path = self.cache_dir / "species" / f"{sid}.json"
        cached = self._read_json(path)
        if cached:
            self._species[sid] = cached
            return cached
        raw = self._get_json(f"{REST}/pokemon-species/{sid}/")
        ef = raw.get("evolves_from_species")
        slim = {
            "id": int(raw["id"]),
            "name": raw.get("name", str(sid)),
            "capture_rate": int(raw.get("capture_rate", 255)),
            "is_legendary": bool(raw.get("is_legendary")),
            "is_mythical": bool(raw.get("is_mythical")),
            "evolves_from": _species_id_from_url(ef["url"]) if isinstance(ef, dict) else None,
            "chain_url": (raw.get("evolution_chain") or {}).get("url", ""),
            "names": {n["language"]["name"]: n["name"] for n in raw.get("names", [])
                      if n.get("language", {}).get("name") in LANG_CODES},
        }
        self._write_json(path, slim)
        self._species[sid] = slim
        return slim

    def pokemon(self, sid: int) -> dict:
        """Slimmed `pokemon/{id}`: base stats, types, abilities, height (dm), weight (hg)."""
        if sid in self._pokemon:
            return self._pokemon[sid]
        path = self.cache_dir / "pokemon" / f"{sid}.json"
        cached = self._read_json(path)
        if cached:
            self._pokemon[sid] = cached
            return cached
        raw = self._get_json(f"{REST}/pokemon/{sid}/")
        slim = {
            "id": int(raw["id"]),
            "name": raw.get("name", str(sid)),
            "stats": {st["stat"]["name"]: int(st["base_stat"]) for st in raw.get("stats", [])},
            "types": [t["type"]["name"] for t in sorted(raw.get("types", []), key=lambda t: t.get("slot", 0))],
            "abilities": [{"name": a["ability"]["name"], "hidden": bool(a.get("is_hidden"))}
                          for a in sorted(raw.get("abilities", []), key=lambda a: a.get("slot", 0))],
            "height": int(raw.get("height", 0)),
            "weight": int(raw.get("weight", 0)),
        }
        self._write_json(path, slim)
        self._pokemon[sid] = slim
        return slim

    def type_chart(self) -> dict[str, dict[str, float]]:
        """attacking type -> {defending type: multiplier} for all 18 types (cached forever)."""
        if self._type_chart:
            return self._type_chart
        path = self.cache_dir / "types.json"
        cached = self._read_json(path)
        if cached and len(cached) == len(TYPES):
            self._type_chart = cached
            return cached
        chart: dict[str, dict[str, float]] = {}
        for t in TYPES:
            rel = self._get_json(f"{REST}/type/{t}/").get("damage_relations", {})
            row = {d: 1.0 for d in TYPES}
            for x in rel.get("no_damage_to", []):
                row[x["name"]] = 0.0
            for x in rel.get("half_damage_to", []):
                row[x["name"]] = 0.5
            for x in rel.get("double_damage_to", []):
                row[x["name"]] = 2.0
            chart[t] = row
        self._write_json(path, chart)
        self._type_chart = chart
        return chart

    def line(self, base_id: int) -> EvoLine:
        if base_id in self._lines:
            return self._lines[base_id]
        path = self.cache_dir / "lines" / f"{base_id}.json"
        cached = self._read_json(path)
        if cached:
            line = EvoLine.from_dict(cached)
            self._lines[base_id] = line
            return line
        sp = self.species(base_id)
        if not sp["chain_url"].startswith("https://pokeapi.co/"):
            raise PokeAPIError(f"bad chain url for species {base_id}")
        chain = self._get_json(sp["chain_url"])

        def node(link: dict) -> EvoNode:
            return EvoNode(_species_id_from_url((link.get("species") or {}).get("url", "")),
                           [node(c) for c in link.get("evolves_to", [])])

        tree = node(chain["chain"]).keeping_animated() or EvoNode(base_id)
        names = {sid: dict(self.species(sid)["names"]) for sid in tree.all_ids()}
        line = EvoLine(base_id, tree, rarity_from(sp["capture_rate"], sp["is_legendary"], sp["is_mythical"]), names)
        self._write_json(path, line.to_dict())
        self._lines[base_id] = line
        return line

    def line_for(self, sid: int) -> EvoLine:
        """The line any member belongs to — line() wants the base, this walks `evolves_from` up to
        it first, so a caught Quagsire still knows about Wooper. Same cache, keyed by the base."""
        for line in self._lines.values():
            if sid in line.tree.all_ids():
                return line
        sp = self.species(sid)
        while sp.get("evolves_from") is not None:
            sp = self.species(int(sp["evolves_from"]))
        return self.line(int(sp["id"]))

    # ------------------------------------------------------------ base index
    def base_index(self) -> list[tuple[int, int]]:
        """[(species_id, capture_rate)] for every Gen 1-5 base species except Ditto."""
        if self._base_index:
            return self._base_index
        path = self.cache_dir / "base-index.json"
        disk = self._read_json(path)
        if disk and time.time() - float(disk.get("fetchedAt", 0)) < BASE_INDEX_TTL and disk.get("entries"):
            self._base_index = [(int(i), int(c)) for i, c in disk["entries"]]
            return self._base_index
        try:
            q = ("{ pokemonspecies(where: {evolves_from_species_id: {_is_null: true}, "
                 f"id: {{_lte: {ANIMATED_IDS[-1]}, _neq: {DITTO_ID}}}}}, order_by: {{id: asc}}) {{ id capture_rate }} }}")
            body = json.dumps({"query": q}).encode()
            resp = json.loads(self._http(GRAPHQL, data=body, headers={"Content-Type": "application/json"}))
            rows = resp.get("data", {}).get("pokemonspecies") or []
            entries = [(int(r["id"]), int(r["capture_rate"])) for r in rows]
            if not entries:
                raise PokeAPIError("empty base index")
            self._write_json(path, {"fetchedAt": time.time(), "entries": entries})
            self._base_index = entries
            return entries
        except (PokeAPIError, ValueError, KeyError, TypeError) as e:
            if disk and disk.get("entries"):            # stale but usable
                self._base_index = [(int(i), int(c)) for i, c in disk["entries"]]
                return self._base_index
            raise PokeAPIError(f"base index unavailable: {e}") from e

    def wild_index(self) -> list[tuple[int, int]]:
        """[(species_id, capture_rate)] for every Gen 1-5 species, evolved forms included. Eggs
        hatch base forms only; the wild is where an already-evolved Pokémon can turn up, so the
        two draws come from different pools."""
        if self._wild_index:
            return self._wild_index
        path = self.cache_dir / "wild-index.json"
        disk = self._read_json(path)
        if disk and time.time() - float(disk.get("fetchedAt", 0)) < BASE_INDEX_TTL and disk.get("entries"):
            self._wild_index = [(int(i), int(c)) for i, c in disk["entries"]]
            return self._wild_index
        try:
            q = ("{ pokemonspecies(where: {id: {_lte: %d, _neq: %d}}, order_by: {id: asc}) "
                 "{ id capture_rate } }" % (ANIMATED_IDS[-1], DITTO_ID))
            body = json.dumps({"query": q}).encode()
            resp = json.loads(self._http(GRAPHQL, data=body, headers={"Content-Type": "application/json"}))
            rows = resp.get("data", {}).get("pokemonspecies") or []
            entries = [(int(r["id"]), int(r["capture_rate"])) for r in rows]
            if not entries:
                raise PokeAPIError("empty wild index")
            self._write_json(path, {"fetchedAt": time.time(), "entries": entries})
            self._wild_index = entries
            return entries
        except (PokeAPIError, ValueError, KeyError, TypeError) as e:
            if disk and disk.get("entries"):
                self._wild_index = [(int(i), int(c)) for i, c in disk["entries"]]
                return self._wild_index
            self._wild_index = self.base_index()          # fall back to the egg pool
            return self._wild_index

    def random_base_via_rest(self, rng: random.Random, tier: str | None = None, tries: int = 16) -> int | None:
        """Fallback when GraphQL is down: sample ids until one is a base species (and meets the tier)."""
        for _ in range(tries):
            sid = rng.randrange(ANIMATED_IDS.start, ANIMATED_IDS.stop)
            if sid == DITTO_ID:
                continue
            sp = self.species(sid)                      # raises PokeAPIError on network failure
            if sp["evolves_from"] is not None:
                continue
            if tier is not None and not rarity_includes(tier, sp["capture_rate"]):
                continue
            return sid
        return None

    # --------------------------------------------------------------- sprites
    def _download(self, url: str, dest: Path) -> Path | None:
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        try:
            data = self._http(url)
        except PokeAPIError:
            return None
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return dest

    def sprite(self, sid: int, animated: bool = True, shiny: bool = False) -> Path | None:
        animated = animated and sid in ANIMATED_IDS
        ext = "gif" if animated else "png"
        dest = self.sprite_dir / f"{sid}{'-shiny' if shiny else ''}.{ext}"
        if animated:
            url = f"{SPRITE_BASE}/versions/generation-v/black-white/animated/{'shiny/' if shiny else ''}{sid}.gif"
        else:
            url = f"{SPRITE_BASE}/{'shiny/' if shiny else ''}{sid}.png"
        got = self._download(url, dest)
        if got is None and animated:                    # fall back to the static sprite
            return self.sprite(sid, animated=False, shiny=shiny)
        return got

    def egg_sprite(self) -> Path | None:
        return self._download(f"{SPRITE_BASE}/egg.png", self.sprite_dir / "egg.png")

    def item_sprite(self, name: str) -> Path | None:
        return self._download(f"{ITEM_BASE}/{name}.png", self.sprite_dir / f"item-{name}.png")
