"""The store's one tool: check the mods' manifests, build the site and catalog from them, guard routine pull requests.

    python tools/store.py check   [--site DIR] [--offline] [--allow-draft] [--assets DIR] [--baseline FILE]
                                  [--event FILE] [--privacy-file FILE] [--magician DIR] [--previous URL]
    python tools/store.py build   --out DIR [--allow-draft] [--assets DIR] [--magician DIR]
    python tools/store.py guard   --base REV [--head REV] --event FILE
    python tools/store.py gate    --event FILE
    python tools/store.py smoke   [--base-url URL] [--tries N]
    python tools/store.py record  --mod ID --zip FILE --baseline FILE [--write]
    python tools/store.py proofs  --mod ID --ledger proofs.tsv [--write]
    python tools/store.py magician

Python 3.11 or later, standard library only; `build` and `check` (which builds) also need Pillow, for the pinned
Magician's `source` tool. The contract it enforces is docs/manifest.md; the schema is schema/mod.schema.json.
Exit 0 when everything holds, 1 when something does not (each failure on its own line), 2 on a usage error.

CI on GitHub runs `check` on every pull request and on main, then deploys exactly the tree `check` built. Understudy
runs the same `check` on a candidate checkout before it pushes a branch.
"""
import hashlib
import io
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_ID = "deadlock-skins.mod/1"
CACHE = os.path.join(ROOT, ".cache")
UA = "deadlock-skins-store-check"
# The fields a routine pull request may change: proofs and the compatibility each release claims from them.
ROUTINE_RELEASE_FIELDS = {"proven_on", "pending_on", "valve_check"}
# "proven on build 6868", "tested on Deadlock builds 6865 and 6866", "works on build 6871": a claim that bytes were
# seen working on a build. "Built for build N" is not one (it says what the release was made against).
CLAIM = re.compile(r"\b(?:proven|tested|works|working|verified)\b[^.\n]{0,60}?\bbuilds?\s+"
                   r"(\d{4,5}(?:(?:\s*,\s*|\s*,?\s+and\s+)\d{4,5})*)", re.I)


class Problems:
    def __init__(self):
        self.items = []

    def add(self, where, msg):
        self.items.append(f"{where}: {msg}")

    def __bool__(self):
        return bool(self.items)


def say(msg=""):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def die(msg, code=1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_json(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------------------------------- the schema

def validate(value, schema, root, where="$"):
    """The part of JSON Schema 2020-12 that schema/mod.schema.json uses; any other keyword is refused, so the schema
    can never say more than this checks."""
    known = {"$schema", "$id", "title", "description", "$defs", "$ref", "type", "properties", "required",
             "additionalProperties", "items", "enum", "const", "pattern", "minLength", "minItems", "maxItems",
             "minimum", "maximum"}
    unknown = set(schema) - known
    if unknown:
        raise ValueError(f"the schema uses {sorted(unknown)}, which tools/store.py does not check")
    out = []
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            raise ValueError(f"unsupported $ref {ref}")
        return validate(value, root["$defs"][ref[len("#/$defs/"):]], root, where)
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool}
    if t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{where}: must be an integer"]
    elif t:
        if not isinstance(value, types[t]):
            return [f"{where}: must be {'an' if t[0] in 'aeiou' else 'a'} {t}"]
    if "const" in schema and value != schema["const"]:
        out.append(f"{where}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: must be one of {schema['enum']}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{where}: {value!r} does not match {schema['pattern']}")
        if len(value) < schema.get("minLength", 0):
            out.append(f"{where}: must not be empty")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{where}: must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{where}: must be at most {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            out.append(f"{where}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{where}: at most {schema['maxItems']} item(s)")
        if "items" in schema:
            for i, v in enumerate(value):
                out += validate(v, schema["items"], root, f"{where}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                out.append(f"{where}: missing {k}")
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    out.append(f"{where}: unknown field {k}")
        for k, v in value.items():
            if k in props:
                out += validate(v, props[k], root, f"{where}.{k}")
    return out


# ---------------------------------------------------------------------------------------------------- the repo

def store_spec():
    return load_json(os.path.join(ROOT, "store.json"))


def manifest_paths(root=ROOT):
    base = os.path.join(root, "skins")
    if not os.path.isdir(base):
        return []
    return sorted(os.path.join(base, d, "manifest.json") for d in os.listdir(base)
                  if os.path.isfile(os.path.join(base, d, "manifest.json")))


def load_manifests(problems, root=ROOT):
    schema = load_json(os.path.join(root, "schema", "mod.schema.json"))
    mods = []
    base = os.path.join(root, "skins")
    for d in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if not os.path.isdir(os.path.join(base, d)):
            continue
        p = os.path.join(base, d, "manifest.json")
        rel = f"skins/{d}/manifest.json"
        if not os.path.isfile(p):
            problems.add(f"skins/{d}", "a mod folder needs a manifest.json")
            continue
        try:
            m = load_json(p)
        except ValueError as e:
            problems.add(rel, f"does not parse: {e}")
            continue
        errors = validate(m, schema, schema)
        for e in errors:
            problems.add(rel, e)
        if errors:
            continue
        if m["id"] != d:
            problems.add(rel, f"id {m['id']!r} must be the folder's name, {d!r}")
            continue
        m["_dir"] = os.path.join(base, d)
        m["_rel"] = rel
        mods.append(m)
    # catalog order: oldest mod first, so a new mod joins at the end and the front page does not reshuffle
    mods.sort(key=lambda m: (m["releases"][0]["date"], m["id"]))
    return mods


def asset_url(spec, m, r):
    return f"https://github.com/{spec['repo']}/releases/download/{r['asset']['tag']}/{urllib.parse.quote(r['asset']['name'])}"


def served_url(spec, r):
    return spec["base_url"] + "files/" + urllib.parse.quote(r["asset"]["name"])


def check_conventions(spec, m, problems):
    """What the schema cannot say: names that must agree with each other."""
    where = m["_rel"]
    versions = [r["version"] for r in m["releases"]]
    if len(set(versions)) != len(versions):
        problems.add(where, "two releases share a version")
    key = lambda v: tuple(int(x) for x in v.split("."))
    if [key(v) for v in versions] != sorted(key(v) for v in versions):
        problems.add(where, "releases must be oldest first")
    if [r["date"] for r in m["releases"]] != sorted(r["date"] for r in m["releases"]):
        problems.add(where, "release dates must not go backwards")
    if not os.path.isfile(os.path.join(m["_dir"], m["listing"]["page"])):
        problems.add(where, f"listing.page {m['listing']['page']} is not in the folder")
    for i, r in enumerate(m["releases"]):
        w = f"{where} releases[{i}] ({r['version']})"
        if r["asset"]["tag"] != f"{m['id']}-{r['version']}":
            problems.add(w, f"asset.tag must be {m['id']}-{r['version']}")
        if r["asset"]["url"] != asset_url(spec, m, r):
            problems.add(w, f"asset.url must be {asset_url(spec, m, r)}")
        if r["served"] != served_url(spec, r):
            problems.add(w, f"served must be {served_url(spec, r)}")
        if r["asset"]["sha256"] in {x["asset"]["sha256"] for x in m["releases"][:i]}:
            problems.add(w, "the same zip as an earlier release")
        ids = [c["id"] for c in r["components"]]
        if len(set(ids)) != len(ids) or [c["order"] for c in r["components"]] != list(range(len(ids))):
            problems.add(w, "components need distinct ids and orders 0, 1, 2 ... in list order")
        if all(c["optional"] for c in r["components"]):
            problems.add(w, "one component at least must always be installed")
        for c in r["components"]:
            if c["pak"] != f"files/{c['order']:04d}-{c['id']}/pak{c['slot']:02d}_dir.vpk":
                problems.add(w, f"component {c['id']}: pak must be files/{c['order']:04d}-{c['id']}/pak{c['slot']:02d}_dir.vpk")
        if set(r["pending_on"]) & set(r["proven_on"]):
            problems.add(w, "a build cannot be both proven_on and pending_on")
    unknown = set(m["listing"]["preselect"]) - {c["id"] for c in m["releases"][-1]["components"] if c["optional"]}
    if unknown:
        problems.add(where, f"listing.preselect names {sorted(unknown)}, which are not optional components")
    if m["status"] == "early" and not m["releases"][-1]["proven_on"]:
        problems.add(where, "an early release still needs a proof on one build: the store says which")


# ---------------------------------------------------------------------------------------------------- GitHub

def github_token():
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    if shutil.which("gh"):
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


class StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub answers an asset download with a redirect to a signed storage URL; the token must not follow it there."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.headers = {k: v for k, v in new.headers.items() if k.lower() != "authorization"}
            new.unredirected_hdrs = {}
        return new


def http(url, token=None, accept=None, timeout=60):
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(StripAuthOnRedirect())
    return opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)


def api(path, token):
    with http("https://api.github.com/" + path.lstrip("/"), token, "application/vnd.github+json") as r:
        return json.load(r)


def release_state(repo, tag, token, cache):
    """The release a manifest names, as GitHub has it: published (anyone can download it), draft (only someone with
    push access sees it) or missing."""
    if tag in cache:
        return cache[tag]
    try:
        rel = api(f"repos/{repo}/releases/tags/{urllib.parse.quote(tag)}", token)
        state = "published"
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        rel, state = None, "missing"
        if token:
            page = 1
            while rel is None:
                items = api(f"repos/{repo}/releases?per_page=100&page={page}", token)
                rel = next((x for x in items if x.get("tag_name") == tag and x.get("draft")), None)
                if len(items) < 100:
                    break
                page += 1
            if rel is not None:
                state = "draft"
    cache[tag] = (state, rel)
    return cache[tag]


# ---------------------------------------------------------------------------------------------------- assets

def cached_asset(r):
    return os.path.join(CACHE, "assets", r["asset"]["sha256"], r["asset"]["name"])


def fetch_asset(spec, r, token, states, assets_dir=None, allow_draft=False):
    """The release zip, from the cache, a local folder or GitHub, refused unless its size and sha256 are the
    manifest's. Returns (path, state)."""
    a = r["asset"]
    path = cached_asset(r)
    state, rel = release_state(spec["repo"], a["tag"], token, states) if token is not False else ("unknown", None)
    if os.path.isfile(path) and os.path.getsize(path) == a["size"] and sha256_file(path) == a["sha256"]:
        return path, state
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if assets_dir and os.path.isfile(os.path.join(assets_dir, a["name"])):
        shutil.copyfile(os.path.join(assets_dir, a["name"]), path + ".part")
    else:
        if state == "missing":
            raise RuntimeError(f"no release {a['tag']} on {spec['repo']}")
        if state == "draft" and not allow_draft:
            raise RuntimeError(f"release {a['tag']} is a draft: publish it before the store can serve it")
        if state == "draft":
            match = [x for x in rel["assets"] if x["name"] == a["name"]]
            if not match:
                raise RuntimeError(f"draft release {a['tag']} has no asset {a['name']}")
            src = http(match[0]["url"], token, "application/octet-stream", timeout=600)
        else:
            src = http(a["url"], None, timeout=600)
        with src, open(path + ".part", "wb") as f:
            shutil.copyfileobj(src, f, 1 << 20)
    if os.path.getsize(path + ".part") != a["size"] or sha256_file(path + ".part") != a["sha256"]:
        os.remove(path + ".part")
        raise RuntimeError(f"{a['name']} does not have the manifest's size and sha256")
    os.replace(path + ".part", path)
    return path, state


def check_asset_record(r, state, rel, problems, where, allow_draft):
    """GitHub's own record of the asset (its size and, for newer uploads, the sha256 it computed) against the
    manifest, so a re-uploaded asset is caught even when the cache holds the old bytes."""
    if state == "missing":
        problems.add(where, f"no release {r['asset']['tag']}")
        return
    if state == "draft" and not allow_draft:
        problems.add(where, f"release {r['asset']['tag']} is a draft; publish it first")
    match = [x for x in (rel or {}).get("assets", []) if x["name"] == r["asset"]["name"]]
    if not match:
        problems.add(where, f"release {r['asset']['tag']} has no asset {r['asset']['name']}")
        return
    x = match[0]
    if x.get("size") != r["asset"]["size"]:
        problems.add(where, f"GitHub says {x['name']} is {x.get('size')} bytes, the manifest {r['asset']['size']}")
    digest = x.get("digest")
    if digest and digest != "sha256:" + r["asset"]["sha256"]:
        problems.add(where, f"GitHub's sha256 for {x['name']} is not the manifest's")
    extra = sorted(y["name"] for y in rel["assets"] if y["name"] != r["asset"]["name"])
    if extra:
        problems.add(where, f"release {r['asset']['tag']} carries other assets too: {extra}")


# ---------------------------------------------------------------------------------------------------- the zip

def vpk_listing(stream):
    """A single-file VPK v2 directory: [(path, crc32, size)], read from a stream positioned at the pak's start;
    returns the listing and the bytes read so far (the caller hashes the rest)."""
    head = stream.read(28)
    sig, version, tree = struct.unpack_from("<III", head)
    if sig != 0x55AA1234 or version != 2:
        raise ValueError("not a VPK v2")
    data = stream.read(tree)
    pos, out = 0, []

    def cstr():
        nonlocal pos
        end = data.index(b"\0", pos)
        s = data[pos:end].decode("utf-8")
        pos = end + 1
        return s

    while True:
        ext = cstr()
        if not ext:
            break
        while True:
            folder = cstr()
            if not folder:
                break
            while True:
                name = cstr()
                if not name:
                    break
                crc, preload, _archive, _offset, length, term = struct.unpack_from("<IHHIIH", data, pos)
                pos += 18
                if term != 0xFFFF:
                    raise ValueError("bad VPK directory entry")
                pos += preload
                path = (name if folder == " " else f"{folder}/{name}") + ("" if ext == " " else f".{ext}")
                out.append((path, crc, preload + length))
    return out, head + data


def read_zip(path):
    """What the store needs from a release zip: its single top folder, the paks (sha256, size, listing), the
    mod-manager metadata, the changelog, previews and scenes."""
    z = zipfile.ZipFile(path)
    names = z.namelist()
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) != 1:
        raise ValueError("a release zip has one top-level folder")
    top = tops.pop()
    rel = lambda n: n[len(top) + 1:]
    info = {"top": top, "names": [rel(n) for n in names if not n.endswith("/")], "paks": {}}
    for n in names:
        if re.fullmatch(r"files/[^/]+/pak\d{2}_dir\.vpk", rel(n)):
            h = hashlib.sha256()
            with z.open(n) as f:
                listing, first = vpk_listing(f)
                h.update(first)
                for b in iter(lambda: f.read(1 << 20), b""):
                    h.update(b)
            info["paks"][rel(n)] = {"sha256": h.hexdigest(), "size": z.getinfo(n).file_size, "listing": listing}
    read = lambda r: z.read(f"{top}/{r}").decode("utf-8") if f"{top}/{r}" in names else None
    info["interchange"] = json.loads(read("mod-interchange.json") or "null")
    info["config"] = json.loads(read("mod.config.json") or "null")
    info["changelog"] = read("CHANGELOG.md") or ""
    info["text"] = {r: read(r) for r in info["names"] if r.endswith((".md", ".txt", ".json", ".sh", ".cmd"))}
    info["scenes"] = sorted(m.group(1) for r in info["names"] if (m := re.fullmatch(r"scene/([a-z0-9_]+)/scene-patch\.txt", r)))
    return info


def release_from_zip(info):
    """The release entry a zip implies, everything but the store's own fields (asset, served, valve_check, proofs)."""
    inter, config = info["interchange"], info["config"]
    if not inter or not config:
        raise ValueError("not a Magician bundle zip (no mod-interchange.json or mod.config.json)")
    ext = inter["mods"][0].get("extensions", {}).get("magician", {})
    required = {l["name"]: l.get("required", True) for l in config.get("layers", [])}
    comps = []
    for mod in sorted(inter["mods"], key=lambda x: x["order"]):
        vpks = [f for f in mod["files"] if f["path"].endswith(".vpk")]
        if len(vpks) != 1:
            raise ValueError(f"mod {mod['name']!r} needs exactly one pak")
        f = vpks[0]
        m = re.fullmatch(r"files/(\d{4})-([a-z0-9-]+)/pak(\d{2})_dir\.vpk", f["path"])
        if not m:
            raise ValueError(f"pak path {f['path']!r} is not files/NNNN-<id>/pakNN_dir.vpk")
        comps.append({"id": m.group(2), "name": mod["name"], "description": mod["description"], "order": mod["order"],
                      "optional": not required.get(m.group(2), True), "pak": f["path"], "slot": int(m.group(3)),
                      "sha256": f["sha256"], "size": f["size"]})
    return {"version": ext.get("release"), "date": (inter.get("createdAt") or "")[:10], "build": ext.get("version"),
            "built_for": ext.get("game_build"), "components": comps, "scenes": sorted(ext.get("scenes", [])),
            "previews": [s.split("/", 1)[1] for s in config.get("screenshots", [])]}


def listing_lines(r, info):
    lines = []
    for c in r["components"]:
        for path, crc, size in info["paks"][c["pak"]]["listing"]:
            lines.append(f"{c['id']}\t{path}\t{crc:08x}\t{size}\n")
    return sorted(lines)


def listing_sha256(r, info):
    return hashlib.sha256("".join(listing_lines(r, info)).encode()).hexdigest()


def read_baseline(path):
    """Magician's baseline of a build (`magician baseline`): the path, crc32 and size of every file Valve ships."""
    build, files = None, set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                m = re.search(r"build=(\d+)", line)
                build = int(m.group(1)) if m else build
                continue
            c = line.rstrip("\n").split("\t")
            if len(c) == 3:
                files.add((c[0].lower(), int(c[1], 16), int(c[2])))
    if not build or not files:
        raise ValueError(f"{path} is not a Magician baseline")
    return build, files


def valve_identical(r, info, baseline_files):
    return sorted(path for c in r["components"] for path, crc, size in info["paks"][c["pak"]]["listing"]
                  if (path.lower(), crc, size) in baseline_files)


def check_release_zip(m, i, info, problems, baseline):
    r = m["releases"][i]
    where = f"{m['_rel']} releases[{i}] ({r['version']})"
    try:
        implied = release_from_zip(info)
    except (ValueError, KeyError, TypeError) as e:
        problems.add(where, f"{r['asset']['name']}: {e}")
        return
    for k, v in implied.items():
        if r[k] != v:
            problems.add(where, f"{k} is {r[k]!r} in the manifest and {v!r} in the zip")
    for c in r["components"]:
        pak = info["paks"].get(c["pak"])
        if pak is None:
            problems.add(where, f"the zip has no {c['pak']}")
        elif (pak["sha256"], pak["size"]) != (c["sha256"], c["size"]):
            problems.add(where, f"{c['pak']} in the zip is not the sha256 and size the manifest gives")
    if set(info["paks"]) != {c["pak"] for c in r["components"]}:
        problems.add(where, "the zip carries paks the manifest does not list")
    for p in r["previews"]:
        if f"previews/{p}" not in info["names"]:
            problems.add(where, f"no previews/{p} in the zip")
    if any(c["pak"] not in info["paks"] for c in r["components"]):
        return
    v = r["valve_check"]
    lines = listing_lines(r, info)
    if v["listing_sha256"] != listing_sha256(r, info) or v["files"] != len(lines):
        problems.add(where, "valve_check was recorded for other paks than these (listing_sha256 or files differ): "
                            "run `store.py record` where the game's baseline is")
    if v["identical"]:
        problems.add(where, f"valve_check found {v['identical']} file(s) byte-identical to Valve's: they cannot ship")
    if baseline:
        build, files = baseline
        same = valve_identical(r, info, files)
        if same:
            problems.add(where, f"{len(same)} file(s) byte-identical to Valve's on build {build}, e.g. {same[0]}")


# ---------------------------------------------------------------------------------------------------- proofs

def proof_id(build, pairs):
    """Magician's proof id (crates/magician/src/proofs.rs): the build and the proof's sorted (owner, sha256) pairs."""
    h = hashlib.sha256(str(build).encode())
    for owner, sha in sorted(set(pairs)):
        h.update(f"\n{owner}\t{sha}".encode())
    return h.hexdigest()[:16]


def proofs_by_id(rows):
    out = {}
    for p in rows:
        out.setdefault((p["build"], p["proof"]), set()).add((p["owner"], p["sha256"]))
    return out


def proven_builds(rows, shas):
    return sorted({b for (b, pid), pairs in proofs_by_id(rows).items()
                   if proof_id(b, pairs) == pid and set(shas) <= {s for _, s in pairs}})


def check_proofs(m, problems):
    where = m["_rel"]
    rows = m["proofs"]
    keys = [tuple(sorted(p.items())) for p in rows]
    if len(set(keys)) != len(keys):
        problems.add(where, "a proof row is listed twice")
    if rows != sorted(rows, key=lambda p: (p["build"], p["proof"], p["owner"], p["file"])):
        problems.add(where, "proofs must be sorted by build, proof, owner and file (store.py proofs writes them so)")
    shipped = {c["sha256"] for r in m["releases"] for c in r["components"]}
    for (b, pid), pairs in proofs_by_id(rows).items():
        if proof_id(b, pairs) != pid:
            problems.add(where, f"proof {pid} on build {b} does not add up to its id: a row is missing or changed")
        if not {s for _, s in pairs} & shipped:
            problems.add(where, f"proof {pid} on build {b} holds none of these releases' paks")
    for i, r in enumerate(m["releases"]):
        got = proven_builds(rows, [c["sha256"] for c in r["components"]])
        if r["proven_on"] != got:
            problems.add(f"{where} releases[{i}] ({r['version']})",
                         f"proven_on says {r['proven_on']}, the proofs prove these paks on {got}")


def html_text(t):
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return t.replace("&nbsp;", " ").replace("&amp;", "&")


def claims(text):
    out = set()
    for m in CLAIM.finditer(" ".join(text.split())):
        out.update(int(x) for x in re.findall(r"\d{4,5}", m.group(1)))
    return out


def check_claims(m, infos, problems):
    """Every 'works on build X' the store shows must be a build a complete proof holds these bytes on."""
    cur = m["releases"][-1]
    # the page copy is checked as the store shows it, after the build (check_site): a block the build leaves out
    # (the Install block) is not a claim anyone reads
    for what, text in (("listing.tagline", m["listing"]["tagline"]), ("listing.blurb", m["listing"]["blurb"])):
        bad = sorted(claims(text) - set(cur["proven_on"]))
        if bad:
            problems.add(m["_rel"], f"{what} claims build(s) {bad} that {cur['version']} has no proof on")
    for i, r in enumerate(m["releases"]):
        info = infos.get((m["id"], i))
        if not info:
            continue
        sec = re.search(rf"^##\s+v?{re.escape(r['version'])}\b.*?(?=^##\s|\Z)", info["changelog"], flags=re.M | re.S)
        bad = sorted(claims(sec.group(0) if sec else "") - set(r["proven_on"]))
        if bad:
            problems.add(f"{m['_rel']} releases[{i}] ({r['version']})",
                         f"the zip's changelog claims build(s) {bad} that these paks have no proof on")


def read_ledger(path):
    """A source repository's proofs.tsv (magician proofs/1), as public rows: the evidence path stays private."""
    rows = []
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines:
        c = line.rstrip("\r\n").split("\t")
        if line.startswith("#") or len(c) != 9 or not c[0].isdigit():
            continue
        rows.append({"build": int(c[0]), "date": c[1], "proof": c[2], "owner": c[3], "file": c[4].replace("\\", "/"),
                     "crc32": c[5], "sha256": c[6], "evidence_sha256": c[8]})
    return rows


def public_proofs(m, ledger_rows):
    """Every row of every proof that holds one of the manifest's paks, merged with what the manifest has (a row
    stands alone, so a union is the merge), sorted."""
    shipped = {c["sha256"] for r in m["releases"] for c in r["components"]}
    groups = proofs_by_id(ledger_rows)
    keep = {k for k, pairs in groups.items() if {s for _, s in pairs} & shipped}
    rows = {tuple(sorted(p.items())): p for p in m["proofs"]}
    for p in ledger_rows:
        if (p["build"], p["proof"]) in keep:
            rows.setdefault(tuple(sorted(p.items())), p)
    return sorted(rows.values(), key=lambda p: (p["build"], p["proof"], p["owner"], p["file"]))


# ---------------------------------------------------------------------------------------------------- collisions

def check_collisions(mods, infos, problems):
    """No two mods may claim one pak number or ship one path: a player who has both would lose one of them."""
    slots, paths = {}, {}
    for m in mods:
        i = len(m["releases"]) - 1
        r = m["releases"][i]
        for c in r["components"]:
            slots.setdefault(c["slot"], []).append(f"{m['id']}/{c['id']}")
        info = infos.get((m["id"], i))
        claimed = {f"maps/ui/hero_prefabs/{s}.vpk" for s in r["scenes"]}
        if info:
            claimed |= {p.lower() for c in r["components"] if c["pak"] in info["paks"]
                        for p, _, _ in info["paks"][c["pak"]]["listing"]}
        for p in claimed:
            paths.setdefault(p, set()).add(m["id"])
    for slot, who in sorted(slots.items()):
        if len(who) > 1:
            problems.add("store", f"pak{slot:02d} is claimed by {', '.join(who)}")
    shared = sorted(p for p, who in paths.items() if len(who) > 1)
    for p in shared[:20]:
        problems.add("store", f"{p} is shipped by {', '.join(sorted(paths[p]))}")
    if len(shared) > 20:
        problems.add("store", f"... and {len(shared) - 20} more shared paths")


# ---------------------------------------------------------------------------------------------------- privacy

def withheld_terms(privacy_file=None):
    """The names of unreleased work that may never appear in this repository. They are not in it: CI reads them
    from the PRIVACY_WALL secret, a maintainer's machine from a file. Without them the wall fails closed."""
    raw = os.environ.get("PRIVACY_WALL", "")
    if privacy_file:
        raw += "\n" + open(privacy_file, encoding="utf-8").read()
    terms = sorted({fold(t) for t in re.split(r"[\n,]", raw) if fold(t)})
    return terms


def fold(t):
    t = unicodedata.normalize("NFKD", t.strip().lower())
    return "".join(ch for ch in t if ch.isalnum())


def leaks(text, terms):
    """True when the text holds a term, case and accents and separators aside ("Some_Name", "some name")."""
    if not terms:
        return False
    flat = fold(text)
    return any(t in flat for t in terms)


def scan_privacy(sources, terms, problems):
    """Each source is (where, text). Only where a term is is said, never which, so a log cannot leak it."""
    for where, text in sources:
        if not leaks(text, terms):
            continue
        lines = [n + 1 for n, line in enumerate(text.splitlines()) if leaks(line, terms)]
        at = f" (line {', '.join(map(str, lines[:5]))})" if lines else ""
        problems.add(where, f"holds a withheld name{at}")


def readable(data):
    """Text as text; a binary file (an image) as its runs of printable ASCII, one per line, so letters scattered
    through compressed bytes are never read as a word."""
    if b"\0" not in data:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return "\n".join(m.group().decode("ascii") for m in re.finditer(rb"[ -~]{4,}", data))


def repo_sources(root=ROOT):
    r = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard", "-z"], capture_output=True)
    if r.returncode:
        die("git ls-files failed: " + r.stderr.decode(errors="replace"))
    for rel in sorted(x for x in r.stdout.decode().split("\0") if x):
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            yield rel, rel + "\n" + readable(open(p, "rb").read())


def tree_sources(out):
    for dirpath, _, files in os.walk(out):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, out).replace(os.sep, "/")
            if f.endswith(".zip"):
                continue  # the zips are scanned as assets
            yield f"site/{rel}", rel + "\n" + readable(open(p, "rb").read())


def event_sources(event_path, root=ROOT):
    """A pull request's own words: title, body, branch, labels and commit messages."""
    if not event_path or not os.path.isfile(event_path):
        return
    ev = load_json(event_path)
    pr = ev.get("pull_request")
    if pr:
        yield "pull request title", pr.get("title") or ""
        yield "pull request body", pr.get("body") or ""
        yield "pull request branch", pr.get("head", {}).get("ref") or ""
        yield "pull request labels", " ".join(l["name"] for l in pr.get("labels", []))
        base, head = pr["base"]["sha"], pr["head"]["sha"]
        r = subprocess.run(["git", "-C", root, "log", "--format=%H%n%B", f"{base}..{head}"], capture_output=True)
        if r.returncode == 0:
            yield "commit messages", r.stdout.decode("utf-8", "replace")
        else:
            yield "commit messages", "(unreadable)"
    for c in ev.get("commits", []):
        yield f"commit {c.get('id', '')[:12]}", c.get("message") or ""


# ---------------------------------------------------------------------------------------------------- Magician

def platform_key():
    if sys.platform.startswith("linux") and platform.machine() in ("x86_64", "AMD64"):
        return "linux-x64"
    if sys.platform == "win32":
        return "windows-x64"
    die(f"no pinned Magician build for {sys.platform} {platform.machine()}; pass --magician DIR")


def pinned_magician():
    """The Magician release magician.lock.json pins, downloaded once, checked against its sha256 and unpacked under
    .cache. Never main, never a newer release: changing the pin is a reviewed change to magician.lock.json."""
    lock = load_json(os.path.join(ROOT, "magician.lock.json"))
    key = platform_key()
    a = lock["assets"][key]
    home = os.path.join(CACHE, "magician", f"{lock['version']}-{key}")
    stamp = os.path.join(home, ".sha256")
    if os.path.isfile(stamp) and open(stamp).read().strip() == a["sha256"]:
        return home
    url = f"https://github.com/{lock['repo']}/releases/download/{lock['tag']}/{a['name']}"
    with http(url, timeout=300) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != a["sha256"]:
        die(f"{a['name']} from {url} has sha256 {got}, not the pinned {a['sha256']}")
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home)
    if a["name"].endswith(".zip"):
        zipfile.ZipFile(io.BytesIO(data)).extractall(home)
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            t.extractall(home, filter="data")
    exe = os.path.join(home, "magician.exe" if key.startswith("windows") else "magician")
    if not key.startswith("windows"):
        os.chmod(exe, 0o755)
    open(stamp, "w").write(a["sha256"] + "\n")
    return home


def magician_exe(home):
    return os.path.join(home, "magician.exe" if sys.platform == "win32" else "magician")


# ---------------------------------------------------------------------------------------------------- build

def status_line(m):
    r = m["releases"][-1]
    p = r["proven_on"]
    s = f"Proven in game on Deadlock build{'s' if len(p) > 1 else ''} {join_and(p)}."
    if r["pending_on"]:
        q = r["pending_on"]
        s += f" The check on build{'s' if len(q) > 1 else ''} {join_and(q)} is pending."
    return s


def join_and(xs):
    xs = [str(x) for x in xs]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def derived_inputs(spec, mods, step, work, assets):
    """The inputs Magician's `source` tool reads, written from the manifests for one step of the replay: each mod at
    its step-th release (or its last), so the release history is built oldest first and comes out the same every
    time from nothing but the manifests and the zips."""
    entries = []
    for m in mods:
        i = min(step, len(m["releases"]) - 1)
        r = m["releases"][i]
        L = m["listing"]
        d = os.path.join(work, m["id"], r["version"])
        os.makedirs(os.path.join(d, "package"), exist_ok=True)
        bundle = {"name": L["name"], "slug": m["id"], "author": L["author"], "hero": L["hero"], "category": "Skins",
                  "description": L["description"], "version": r["build"], "release": r["version"], "date": r["date"],
                  "game_build": r["built_for"], "nsfw": L["nsfw"], "link": L["link"], "hero_id": L["hero_id"],
                  "content_type": L["content_type"], "tags": L["tags"],
                  "mods": [{"slug": c["id"], "name": c["name"], "order": c["order"], "description": c["description"],
                            "optional": c["optional"]} for c in r["components"]],
                  "scenes": [{"hero": s} for s in r["scenes"]], "previews": [{"out": p} for p in r["previews"]]}
        with open(os.path.join(d, "package", "bundle.json"), "w", encoding="utf-8", newline="\n") as f:
            f.write(dump_json(bundle))
        with open(os.path.join(d, "mod.toml"), "w", encoding="utf-8", newline="\n") as f:
            f.write(f'[mod]\nslug = "{m["id"]}"\n')
        with open(os.path.join(d, "proofs.tsv"), "w", encoding="utf-8", newline="\n") as f:
            f.write("build\tdate\tproof\towner\tfile\tcrc32\tsha256\tevidence\tevidence_sha256\n")
            for p in m["proofs"]:
                f.write(f"{p['build']}\t{p['date']}\t{p['proof']}\t{p['owner']}\t{p['file']}\t{p['crc32']}\t"
                        f"{p['sha256']}\t-\t{p['evidence_sha256']}\n")
        page = open(os.path.join(m["_dir"], L["page"]), encoding="utf-8").read()
        blurb = L["blurb"]
        if m["status"] == "early":
            note = status_line(m)
            page = re.sub(r"(</h1>)", rf"\1\n\n<p><strong>Early release.</strong> {note}</p>", page, count=1)
            blurb = f"**Early release.** {note} {blurb}"
        with open(os.path.join(d, "page.html"), "w", encoding="utf-8", newline="\n") as f:
            f.write(page)
        e = {"bundle": os.path.join(d, "package", "bundle.json"), "zip": assets[(m["id"], i)], "id": m["id"],
             "page": os.path.join(d, "page.html"), "sections": L["sections"], "preselect": L["preselect"],
             "tagline": L["tagline"], "blurb": blurb, "links": L["links"], "mod_toml": os.path.join(d, "mod.toml")}
        if not L["tagline"]:
            del e["tagline"]
        entries.append(e)
    src = {k: spec[k] for k in ("source_id", "name", "base_url", "authors")}
    src["mods"] = entries
    src["taxonomy"] = os.path.join(ROOT, spec["taxonomy"])
    src["icon"] = os.path.join(ROOT, spec["icon"])
    src["page"] = {k: (os.path.join(ROOT, v) if k in ("css", "head", "header", "footer") else v)
                   for k, v in spec["page"].items()}
    path = os.path.join(work, f"source-{step}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_json(src))
    return path


def build_site(spec, mods, assets, out, home):
    """The whole served tree from the manifests, through the pinned Magician's `source` tool, replayed one release
    at a time so every earlier release is in the history and its zip is still served."""
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    os.makedirs(CACHE, exist_ok=True)
    work = tempfile.mkdtemp(prefix="store-build-", dir=CACHE)
    env = dict(os.environ, MAGICIAN_PYTHON=sys.executable, MAGICIAN_HOME=home, PYTHONUTF8="1")
    steps = max(len(m["releases"]) for m in mods)
    try:
        for step in range(steps):
            spec_path = derived_inputs(spec, mods, step, work, assets)
            r = subprocess.run([magician_exe(home), "source", spec_path, "--out", out], env=env,
                               capture_output=True, text=True, encoding="utf-8")
            if r.returncode:
                die(f"magician source (step {step + 1} of {steps}) failed:\n{r.stdout}{r.stderr}")
            if step == steps - 1:
                say(r.stdout.rstrip())
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check_site(spec, mods, out, problems, previous=None):
    """The built tree must serve what the manifests say, and everything the live store served before."""
    files = set(open(os.path.join(out, ".dmm-source-files"), encoding="utf-8").read().split())
    for m in mods:
        for r in m["releases"]:
            p = os.path.join(out, "files", r["asset"]["name"])
            if not os.path.isfile(p) or sha256_file(p) != r["asset"]["sha256"]:
                problems.add("site", f"files/{r['asset']['name']} is not served with the manifest's bytes")
        listing = load_json(os.path.join(out, "mods", f"{m['id']}.json"))
        if listing["release"]["version"] != m["releases"][-1]["version"]:
            problems.add("site", f"mods/{m['id']}.json offers {listing['release']['version']}, not the newest release")
        history = load_json(os.path.join(out, "mods", m["id"], "releases.json"))["items"]
        if sorted(h["version"] for h in history) != sorted(r["version"] for r in m["releases"]):
            problems.add("site", f"mods/{m['id']}/releases.json does not hold every release")
        cur = m["releases"][-1]
        bad = sorted(claims(listing["description"]) - set(cur["proven_on"]))
        if bad:
            problems.add("site", f"the {m['id']} description claims build(s) {bad} that {cur['version']} has no proof on")
        proven = {r["version"]: set(r["proven_on"]) for r in m["releases"]}
        for h in history:
            bad = sorted(claims(h.get("changelog") or "") - proven.get(h["version"], set()))
            if bad:
                problems.add("site", f"{m['id']} {h['version']}'s release notes claim build(s) {bad} without a proof")
    if previous:
        try:
            with http(previous.rstrip("/") + "/.dmm-source-files") as r:
                before = set(r.read().decode().split())
        except (urllib.error.URLError, OSError) as e:
            problems.add("site", f"could not read what the live store serves ({e})")
            return
        for rel in sorted(before - files):
            problems.add("site", f"{rel} is served now and would disappear")


# ---------------------------------------------------------------------------------------------------- commands

def parse(args, flags, switches=()):
    out = {k: None for k in flags}
    out.update({k: False for k in switches})
    i = 0
    while i < len(args):
        a = args[i]
        if a in flags and i + 1 < len(args):
            out[a] = args[i + 1]
            i += 2
        elif a in switches:
            out[a] = True
            i += 1
        else:
            die(f"unknown or incomplete option {a!r}\n" + __doc__, 2)
    return out


def gather(spec, mods, o, problems, need_zips):
    """Each release's zip, read; GitHub's record of each asset checked when online."""
    token = None if o.get("--offline") else github_token()
    states, infos, assets = {}, {}, {}
    for m in mods:
        for i, r in enumerate(m["releases"]):
            where = f"{m['_rel']} releases[{i}] ({r['version']})"
            if not o.get("--offline"):
                try:
                    state, rel = release_state(spec["repo"], r["asset"]["tag"], token, states)
                    check_asset_record(r, state, rel, problems, where, o.get("--allow-draft"))
                except urllib.error.URLError as e:
                    problems.add(where, f"could not read the release from GitHub ({e})")
            if not need_zips and not os.path.isfile(cached_asset(r)):
                continue
            try:
                path, _ = fetch_asset(spec, r, False if o.get("--offline") else token, states, o.get("--assets"),
                                      o.get("--allow-draft"))
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                problems.add(where, str(e))
                continue
            assets[(m["id"], i)] = path
            try:
                infos[(m["id"], i)] = read_zip(path)
            except (ValueError, KeyError, zipfile.BadZipFile) as e:
                problems.add(where, f"{r['asset']['name']}: {e}")
    return infos, assets


def cmd_check(args):
    o = parse(args, ["--site", "--assets", "--baseline", "--event", "--privacy-file", "--magician", "--previous"],
              ["--offline", "--allow-draft", "--no-build"])
    problems = Problems()
    spec = store_spec()
    mods = load_manifests(problems)
    ids = [m["id"] for m in mods]
    say(f"manifests: {len(mods)} ({', '.join(ids)})")
    for m in mods:
        check_conventions(spec, m, problems)
        check_proofs(m, problems)
    baseline = read_baseline(o["--baseline"]) if o["--baseline"] else None
    infos, assets = gather(spec, mods, o, problems, need_zips=True)
    for m in mods:
        for i in range(len(m["releases"])):
            if (m["id"], i) in infos:
                check_release_zip(m, i, infos[(m["id"], i)], problems, baseline)
        check_claims(m, infos, problems)
    check_collisions(mods, infos, problems)
    say(f"releases: {sum(len(m['releases']) for m in mods)}, zips read: {len(infos)}"
        + (f", checked against Valve's build {baseline[0]}" if baseline else ""))

    terms = withheld_terms(o["--privacy-file"])
    if not terms:
        problems.add("privacy wall", "no withheld names given (the PRIVACY_WALL secret or --privacy-file): "
                                     "the wall fails closed")
    sources = list(repo_sources()) + list(event_sources(o["--event"]))
    for (mid, i), info in infos.items():
        sources += [(f"asset {info['top']}.zip: {rel}", rel + "\n" + (text or ""))
                    for rel, text in info["text"].items()]
        sources.append((f"asset {info['top']}.zip: file names", info["top"] + "\n" + "\n".join(info["names"])))
    scan_privacy(sources, terms, problems)

    if not o["--no-build"] and not problems and len(assets) == sum(len(m["releases"]) for m in mods):
        out = os.path.abspath(o["--site"] or os.path.join(CACHE, "site"))
        home = o["--magician"] or pinned_magician()
        build_site(spec, mods, assets, out, home)
        check_site(spec, mods, out, problems, o["--previous"])
        scan_privacy(tree_sources(out), terms, problems)
        say(f"site: {out}")
    elif not o["--no-build"] and problems:
        say("site: not built (fix the problems first)")
    report(problems)


def report(problems):
    if problems:
        for p in problems.items:
            say("FAIL " + p)
        say(f"{len(problems.items)} problem(s)")
        sys.exit(1)
    say("ok")


def cmd_build(args):
    o = parse(args, ["--out", "--assets", "--magician"], ["--allow-draft", "--offline"])
    if not o["--out"]:
        die("build needs --out DIR", 2)
    problems = Problems()
    spec = store_spec()
    mods = load_manifests(problems)
    infos, assets = gather(spec, mods, dict(o, **{"--offline": True}), problems, need_zips=True) if o["--offline"] \
        else gather(spec, mods, o, problems, need_zips=True)
    if problems:
        report(problems)
    build_site(spec, mods, assets, os.path.abspath(o["--out"]), o["--magician"] or pinned_magician())
    say("ok")


def git_show(rev, rel, root=ROOT):
    r = subprocess.run(["git", "-C", root, "show", f"{rev}:{rel}"], capture_output=True)
    return r.stdout.decode("utf-8") if r.returncode == 0 else None


def routine_diff(rel, before, after):
    """What a routine change may do to one manifest: add proof rows and restate what each release is proven on,
    pending on and checked against. Anything else is a reviewed change."""
    bad = []
    try:
        a, b = json.loads(before), json.loads(after)
    except ValueError as e:
        return [f"{rel}: does not parse ({e})"]
    for k in sorted(set(a) | set(b)):
        if k in ("proofs", "releases"):
            continue
        if a.get(k) != b.get(k):
            bad.append(f"{rel}: changes {k}")
    old_rows = {json.dumps(p, sort_keys=True) for p in a.get("proofs", [])}
    new_rows = {json.dumps(p, sort_keys=True) for p in b.get("proofs", [])}
    if old_rows - new_rows:
        bad.append(f"{rel}: removes or edits {len(old_rows - new_rows)} proof row(s)")
    ra, rb = a.get("releases", []), b.get("releases", [])
    if len(ra) != len(rb):
        bad.append(f"{rel}: adds or removes a release")
    for i, (x, y) in enumerate(zip(ra, rb)):
        for k in sorted(set(x) | set(y)):
            if k not in ROUTINE_RELEASE_FIELDS and x.get(k) != y.get(k):
                bad.append(f"{rel}: releases[{i}] changes {k}")
    return bad


def pr_of(event_path):
    ev = load_json(event_path)
    pr = ev.get("pull_request")
    if not pr:
        die("this event has no pull request", 2)
    return ev, pr


def cmd_guard(args):
    """A pull request labelled `routine` may only add proofs and restate compatibility in existing manifests, and
    only the bot opens one. Runs from the base branch's copy of this file, never the pull request's."""
    o = parse(args, ["--base", "--head", "--event"])
    if not o["--event"] or not o["--base"]:
        die("guard needs --base REV and --event FILE", 2)
    _, pr = pr_of(o["--event"])
    labels = {l["name"] for l in pr.get("labels", [])}
    if "routine" not in labels:
        say("not a routine pull request: nothing to guard (the merge gate wants a review)")
        return
    problems = Problems()
    bot = os.environ.get("ROUTINE_BOT", "")
    author = pr.get("user", {}).get("login", "")
    if not bot:
        problems.add("routine", "the ROUTINE_BOT repository variable is not set, so no routine PR can be trusted")
    elif author != bot:
        problems.add("routine", f"opened by {author}, not the routine bot ({bot}): routine is the bot's label")
    head = o["--head"] or pr["head"]["sha"]
    r = subprocess.run(["git", "-C", ROOT, "diff", "--name-status", "--no-renames", f"{o['--base']}...{head}"],
                       capture_output=True, text=True)
    if r.returncode:
        die("git diff failed: " + r.stderr, 2)
    changed = [line.split("\t") for line in r.stdout.splitlines() if line]
    if not changed:
        problems.add("routine", "changes nothing")
    for status, rel in changed:
        if status != "M" or not re.fullmatch(r"skins/[a-z0-9-]+/manifest\.json", rel):
            problems.add(rel, f"a routine PR may only edit existing manifests ({status})")
            continue
        for b in routine_diff(rel, git_show(o["--base"], rel) or "", git_show(head, rel) or ""):
            problems.add("routine", b)
    say(f"routine pull request #{pr.get('number')} by {author}: {len(changed)} file(s)")
    report(problems)


def cmd_gate(args):
    """The merge gate: a routine PR from the bot needs no review (the guard and the check carry it); the
    maintainer's own PR is reviewed by the maintainer merging it; anything else needs the maintainer's approval
    of its newest commit."""
    o = parse(args, ["--event"])
    if not o["--event"]:
        die("gate needs --event FILE", 2)
    ev, pr = pr_of(o["--event"])
    bot, maintainer = os.environ.get("ROUTINE_BOT", ""), os.environ.get("MAINTAINER", "")
    if not maintainer:
        die("the MAINTAINER repository variable is not set")
    author = pr["user"]["login"]
    labels = {l["name"] for l in pr.get("labels", [])}
    if "routine" in labels and bot and author == bot:
        say(f"routine PR by {bot}: no review needed (routine-guard and check must pass)")
        return
    if author == maintainer:
        say(f"opened by the maintainer ({maintainer}): merging it is the review")
        return
    repo, n, head = ev["repository"]["full_name"], pr["number"], pr["head"]["sha"]
    token = github_token()
    reviews = api(f"repos/{repo}/pulls/{n}/reviews?per_page=100", token)
    latest = {}
    for rv in reviews:
        if rv["user"]["login"] == maintainer and rv["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest = rv
    if latest.get("state") == "APPROVED" and latest.get("commit_id") == head:
        say(f"approved by {maintainer} at {head[:12]}")
        return
    die(f"needs {maintainer}'s approval of {head[:12]} (a routine PR is only the bot's, and only proofs)")


def cmd_smoke(args):
    """After a deploy: every path the store serves answers 200 without a redirect, and every zip has the manifest's
    bytes. Pages can take a minute to switch over, so it tries again."""
    import time
    o = parse(args, ["--base-url", "--tries"])
    spec = store_spec()
    base = (o["--base-url"] or spec["base_url"]).rstrip("/") + "/"
    problems = Problems()
    mods = load_manifests(problems)
    tries = int(o["--tries"] or 1)
    for attempt in range(tries):
        problems = Problems()
        try:
            with http(base + ".dmm-source-files") as r:
                paths = r.read().decode().split()
        except (urllib.error.URLError, OSError) as e:
            problems.add(base, f"no .dmm-source-files ({e})")
            paths = []
        want = {r["asset"]["name"]: r["asset"]["sha256"] for m in mods for r in m["releases"]}
        for rel in paths:
            url = base + urllib.parse.quote(rel)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                opener = urllib.request.build_opener(NoRedirect())
                with opener.open(req, timeout=300) as r:
                    if rel.startswith("files/") and rel.endswith(".zip"):
                        h = hashlib.sha256()
                        for b in iter(lambda: r.read(1 << 20), b""):
                            h.update(b)
                        name = urllib.parse.unquote(rel[len("files/"):])
                        if name in want and h.hexdigest() != want[name]:
                            problems.add(url, "is not the manifest's bytes")
                        say(f"200 {rel} sha256 {h.hexdigest()[:16]}")
                    else:
                        r.read()
                        say(f"200 {rel}")
            except urllib.error.HTTPError as e:
                problems.add(url, f"answers {e.code}")
            except (urllib.error.URLError, OSError) as e:
                problems.add(url, str(e))
        missing = sorted(set(want) - {urllib.parse.unquote(p[len("files/"):]) for p in paths if p.startswith("files/")})
        for name in missing:
            problems.add(base, f"files/{name} is not served")
        if not problems or attempt == tries - 1:
            break
        say(f"{len(problems.items)} problem(s); trying again in 30 s")
        time.sleep(30)
    report(problems)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirects to {newurl}", headers, fp)


def find_manifest(mod_id):
    p = os.path.join(ROOT, "skins", mod_id, "manifest.json")
    return p if os.path.isfile(p) else None


def cmd_record(args):
    """A release entry for a zip, from the zip itself plus the valve check against a Magician baseline of the build
    (run where the baseline is). Prints it; --write appends it to the mod's manifest (or updates the entry of the
    same version). The asset is then uploaded to the release the entry names."""
    o = parse(args, ["--mod", "--zip", "--baseline"], ["--write"])
    if not (o["--mod"] and o["--zip"] and o["--baseline"]):
        die("record needs --mod ID --zip FILE --baseline FILE", 2)
    spec = store_spec()
    info = read_zip(o["--zip"])
    r = release_from_zip(info)
    build, files = read_baseline(o["--baseline"])
    name = os.path.basename(o["--zip"])
    r["asset"] = {"tag": f"{o['--mod']}-{r['version']}", "name": name, "url": "", "size": os.path.getsize(o["--zip"]),
                  "sha256": sha256_file(o["--zip"])}
    r["asset"]["url"] = asset_url(spec, None, r)
    r["served"] = served_url(spec, r)
    lines = listing_lines(r, info)
    r["valve_check"] = {"baseline_build": build, "files": len(lines), "listing_sha256": listing_sha256(r, info),
                        "identical": len(valve_identical(r, info, files))}
    path = find_manifest(o["--mod"])
    m = load_json(path) if path else None
    r["proven_on"] = proven_builds(m["proofs"], [c["sha256"] for c in r["components"]]) if m else []
    r["pending_on"] = []
    order = ["version", "date", "build", "built_for", "components", "scenes", "previews", "asset", "served",
             "valve_check", "proven_on", "pending_on"]
    r = {k: r[k] for k in order}
    if r["valve_check"]["identical"]:
        say(f"warning: {r['valve_check']['identical']} file(s) are byte-identical to Valve's on build {build}")
    if o["--write"]:
        if not m:
            die(f"no skins/{o['--mod']}/manifest.json to write into: start it from docs/manifest.md's example")
        old = {x["version"]: n for n, x in enumerate(m["releases"])}
        if r["version"] in old:
            keep = m["releases"][old[r["version"]]]
            r["pending_on"] = keep["pending_on"]
            m["releases"][old[r["version"]]] = r
        else:
            m["releases"].append(r)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(dump_json(m))
        say(f"wrote {r['version']} into skins/{o['--mod']}/manifest.json")
    else:
        sys.stdout.write(dump_json(r))


def cmd_proofs(args):
    """The public rows of a source repository's proof ledger for the mod's paks, merged into the manifest, and each
    release's proven_on restated from them (and taken out of pending_on). This is the routine change."""
    o = parse(args, ["--mod", "--ledger"], ["--write"])
    if not (o["--mod"] and o["--ledger"]):
        die("proofs needs --mod ID --ledger proofs.tsv", 2)
    path = find_manifest(o["--mod"])
    if not path:
        die(f"no skins/{o['--mod']}/manifest.json")
    m = load_json(path)
    before = dump_json(m)
    m["proofs"] = public_proofs(m, read_ledger(o["--ledger"]))
    for r in m["releases"]:
        r["proven_on"] = proven_builds(m["proofs"], [c["sha256"] for c in r["components"]])
        r["pending_on"] = [b for b in r["pending_on"] if b not in r["proven_on"]]
    after = dump_json(m)
    if after == before:
        say("no new proofs")
        return
    for r in m["releases"]:
        say(f"{r['version']}: proven on {r['proven_on'] or 'nothing yet'}" + (f", pending {r['pending_on']}" if r["pending_on"] else ""))
    if o["--write"]:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(after)
        say(f"wrote skins/{o['--mod']}/manifest.json")


def cmd_magician(args):
    say(pinned_magician())


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cmds = {"check": cmd_check, "build": cmd_build, "guard": cmd_guard, "gate": cmd_gate, "smoke": cmd_smoke,
            "record": cmd_record, "proofs": cmd_proofs, "magician": cmd_magician}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        die(__doc__, 2)
    cmds[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
