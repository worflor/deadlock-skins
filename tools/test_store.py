"""Tests for tools/store.py: python -m unittest discover -s tools -p "test_*.py" -v (no network, no game, no Magician)."""
import copy
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

SPEC = {"repo": "someone/deadlock-skins", "base_url": "https://example.dev/skins/"}


def vpk(files):
    """A single-file VPK v2 whose files are all preload bytes: {path: bytes}."""
    by_ext = {}
    for path, data in files.items():
        folder, _, name = path.rpartition("/")
        stem, _, ext = name.rpartition(".")
        by_ext.setdefault(ext, {}).setdefault(folder or " ", []).append((stem, data))
    tree = b""
    for ext, folders in sorted(by_ext.items()):
        tree += ext.encode() + b"\0"
        for folder, names in sorted(folders.items()):
            tree += folder.encode() + b"\0"
            for stem, data in sorted(names):
                crc = __import__("zlib").crc32(data)
                tree += stem.encode() + b"\0" + struct.pack("<IHHIIH", crc, len(data), 0x7FFF, 0, 0, 0xFFFF) + data
            tree += b"\0"
        tree += b"\0"
    tree += b"\0"
    return struct.pack("<IIIIIII", 0x55AA1234, 2, len(tree), 0, 0, 0, 0) + tree


def bundle_zip(path, mod="demo", version="1.0.0", files=None, slot=10, changelog=None):
    pak = vpk(files or {"models/demo/demo.vmdl_c": b"ours", "materials/demo/a.vmat_c": b"also ours"})
    top = f"Demo_{version}"
    rel = f"files/0000-{mod}/pak{slot:02d}_dir.vpk"
    inter = {"createdAt": "2026-11-20T12:00:00Z", "mods": [{
        "name": "Demo", "order": 0, "description": "A demo skin.",
        "files": [{"name": f"pak{slot:02d}_dir.vpk", "path": rel, "sha256": hashlib.sha256(pak).hexdigest(),
                   "size": len(pak)}],
        "extensions": {"magician": {"version": "b1", "release": version, "game_build": 6868, "scenes": []}}}]}
    config = {"screenshots": ["previews/cover.jpg"], "layers": [{"name": mod, "required": True}]}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{top}/{rel}", pak)
        z.writestr(f"{top}/mod-interchange.json", json.dumps(inter))
        z.writestr(f"{top}/mod.config.json", json.dumps(config))
        z.writestr(f"{top}/CHANGELOG.md", changelog or f"# Changelog\n\n## {version} (2026-11-20)\n\n- Tested on Deadlock build 6868.\n")
        z.writestr(f"{top}/previews/cover.jpg", b"\xff\xd8 not really a jpeg")
    return pak


def manifest_for(zip_path, mod="demo", proofs=()):
    info = store.read_zip(zip_path)
    r = store.release_from_zip(info)
    r["asset"] = {"tag": f"{mod}-{r['version']}", "name": os.path.basename(zip_path), "url": "",
                  "size": os.path.getsize(zip_path), "sha256": store.sha256_file(zip_path)}
    r["asset"]["url"] = store.asset_url(SPEC, None, r)
    r["served"] = store.served_url(SPEC, r)
    r["valve_check"] = {"baseline_build": 6871, "files": len(store.listing_lines(r, info)),
                        "listing_sha256": store.listing_sha256(r, info), "identical": 0}
    r["proven_on"] = store.proven_builds(list(proofs), [c["sha256"] for c in r["components"]])
    r["pending_on"] = []
    m = {"schema": store.SCHEMA_ID, "id": mod, "status": "released", "rights": "cleared",
         "source": {"repo": f"someone/deadlock-{mod}", "mod_toml": "mod.toml"},
         "listing": {"name": "Demo", "author": "someone", "hero": "Lash", "hero_id": "lash", "content_type": "skins",
                     "nsfw": False, "description": "A demo.", "tags": ["demo"], "link": "https://example.dev/skins/",
                     "tagline": "", "blurb": "A demo skin.", "links": [], "page": "page.html", "sections": [],
                     "preselect": []},
         "releases": [r], "proofs": list(proofs), "_rel": f"skins/{mod}/manifest.json"}
    return m, info


def proof_rows(build, sha, owner="demo"):
    pid = store.proof_id(build, [(owner, sha)])
    return [{"build": build, "date": "2026-11-21", "proof": pid, "owner": owner, "file": "pak10_dir.vpk",
             "crc32": "00000000", "sha256": sha, "evidence_sha256": "0" * 64}]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.zip = os.path.join(self.dir, "Demo_1.0.0.zip")
        self.pak = bundle_zip(self.zip)
        self.sha = hashlib.sha256(self.pak).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()


class TheZip(Fixture):
    def test_vpk_listing_reads_paths_crcs_and_sizes(self):
        listing, _ = store.vpk_listing(io.BytesIO(vpk({"a/b/c.txt": b"xyz", "root.vmat_c": b"1234"})))
        self.assertEqual(sorted((p, s) for p, _, s in listing), [("a/b/c.txt", 3), ("root.vmat_c", 4)])

    def test_a_consistent_release_has_no_problems(self):
        m, info = manifest_for(self.zip, proofs=proof_rows(6868, self.sha))
        p = store.Problems()
        store.check_release_zip(m, 0, info, p, None)
        store.check_proofs(m, p)
        self.assertEqual(p.items, [])
        self.assertEqual(m["releases"][0]["proven_on"], [6868])

    def test_a_pak_that_is_not_the_manifests_is_caught(self):
        m, info = manifest_for(self.zip)
        m["releases"][0]["components"][0]["sha256"] = "1" * 64
        p = store.Problems()
        store.check_release_zip(m, 0, info, p, None)
        self.assertTrue(any("sha256" in x for x in p.items), p.items)

    def test_a_valve_check_recorded_for_other_bytes_is_caught(self):
        m, info = manifest_for(self.zip)
        m["releases"][0]["valve_check"]["listing_sha256"] = "2" * 64
        p = store.Problems()
        store.check_release_zip(m, 0, info, p, None)
        self.assertTrue(any("valve_check was recorded for other paks" in x for x in p.items), p.items)

    def test_a_file_identical_to_valves_fails_against_the_baseline(self):
        m, info = manifest_for(self.zip)
        crc = __import__("zlib").crc32(b"ours")
        baseline = (6871, {("models/demo/demo.vmdl_c", crc, 4)})
        p = store.Problems()
        store.check_release_zip(m, 0, info, p, baseline)
        self.assertTrue(any("byte-identical to Valve's on build 6871" in x for x in p.items), p.items)
        m["releases"][0]["valve_check"]["identical"] = 1
        p = store.Problems()
        store.check_release_zip(m, 0, info, p, None)
        self.assertTrue(any("cannot ship" in x for x in p.items), p.items)

    def test_the_baseline_format_is_magicians(self):
        b = os.path.join(self.dir, "baseline.tsv")
        open(b, "w").write("# magician baseline build=6871 files=1\nmodels/x.vmdl_c\t0000beef\t12\n")
        self.assertEqual(store.read_baseline(b), (6871, {("models/x.vmdl_c", 0xBEEF, 12)}))


class TheSchema(Fixture):
    def setUp(self):
        super().setUp()
        self.schema = json.load(open(os.path.join(store.ROOT, "schema", "mod.schema.json"), encoding="utf-8"))
        m, _ = manifest_for(self.zip)
        del m["_rel"]
        self.m = m

    def test_a_manifest_from_a_zip_is_valid(self):
        self.assertEqual(store.validate(self.m, self.schema, self.schema), [])

    def test_unknown_fields_and_a_wrong_schema_are_refused(self):
        m = copy.deepcopy(self.m)
        m["schema"] = "deadlock-skins.mod/2"
        m["listing"]["colour"] = "red"
        errors = store.validate(m, self.schema, self.schema)
        self.assertTrue(any("schema" in e for e in errors) and any("unknown field colour" in e for e in errors), errors)

    def test_the_repos_own_manifests_are_valid(self):
        p = store.Problems()
        mods = store.load_manifests(p)
        self.assertEqual(p.items, [])
        self.assertTrue(mods)

    def test_the_validator_refuses_keywords_it_does_not_check(self):
        with self.assertRaises(ValueError):
            store.validate(1, {"type": "integer", "multipleOf": 2}, {})

    def test_conventions_tie_tag_url_and_served_together(self):
        m = copy.deepcopy(self.m)
        m["_rel"], m["_dir"] = "skins/demo/manifest.json", self.dir
        open(os.path.join(self.dir, "page.html"), "w").write("<h1>Demo</h1>")
        p = store.Problems()
        store.check_conventions(SPEC, m, p)
        self.assertEqual(p.items, [])
        m["releases"][0]["asset"]["tag"] = "demo-v1"
        m["releases"][0]["served"] = "https://elsewhere.dev/x.zip"
        p = store.Problems()
        store.check_conventions(SPEC, m, p)
        self.assertEqual(len(p.items), 3, p.items)


class Proofs(Fixture):
    def test_proof_ids_are_magicians(self):
        # a row of Maid Lash's ledger: build 6864, one file
        sha = "d67cb5d8ace272cad62c696783d3005836722b1223fad8604eb4979339ef5e47"
        self.assertEqual(store.proof_id(6864, [("maid_lash", sha)]), "a6f98cf84f567a6b")

    def test_proven_on_must_be_what_the_proofs_say(self):
        m, _ = manifest_for(self.zip, proofs=proof_rows(6868, self.sha))
        m["releases"][0]["proven_on"] = [6868, 6871]
        p = store.Problems()
        store.check_proofs(m, p)
        self.assertTrue(any("proven_on says [6868, 6871]" in x for x in p.items), p.items)

    def test_a_proof_missing_a_row_counts_for_nothing(self):
        rows = proof_rows(6868, self.sha) + proof_rows(6868, "3" * 64, owner="scene")
        pid = store.proof_id(6868, [("demo", self.sha), ("scene", "3" * 64)])
        for r in rows:
            r["proof"] = pid
        self.assertEqual(store.proven_builds(rows, [self.sha]), [6868])
        self.assertEqual(store.proven_builds(rows[:1], [self.sha]), [])

    def test_ledger_rows_become_public_rows_of_whole_proofs(self):
        ledger = os.path.join(self.dir, "proofs.tsv")
        pid = store.proof_id(6868, [("demo", self.sha)])
        other = store.proof_id(6868, [("elsewhere", "4" * 64)])
        open(ledger, "w").write(
            "# magician proofs/1\nbuild\tdate\tproof\towner\tfile\tcrc32\tsha256\tevidence\tevidence_sha256\n"
            f"6868\t2026-11-25\t{pid}\tdemo\tpak10_dir.vpk\tabcd0123\t{self.sha}\tD:\\private\\log.txt\t{'5' * 64}\n"
            f"6868\t2026-11-25\t{other}\telsewhere\tpak20_dir.vpk\tabcd0123\t{'4' * 64}\tD:\\x\t{'5' * 64}\n")
        m, _ = manifest_for(self.zip)
        rows = store.public_proofs(m, store.read_ledger(ledger))
        self.assertEqual([r["proof"] for r in rows], [pid])
        self.assertNotIn("evidence", rows[0])
        self.assertNotIn("private", json.dumps(rows))


class Claims(unittest.TestCase):
    def test_what_counts_as_a_claim(self):
        self.assertEqual(store.claims("Tested on Deadlock builds 6865 and 6866."), {6865, 6866})
        self.assertEqual(store.claims("Built for Deadlock build 6871 and proven in game on build 6868."), {6868})
        self.assertEqual(store.claims("Built for and proven in game on Deadlock builds 6868 and 6871"), {6868, 6871})
        self.assertEqual(store.claims("Built for Deadlock build 6871."), set())
        self.assertEqual(store.claims("Version 1.0.2, built for Deadlock build 6868. Proven in game on build 6868."), {6868})

    def test_an_unproven_claim_in_the_blurb_fails(self):
        m = {"_rel": "x", "id": "x", "listing": {"tagline": "", "blurb": "Works on build 6871."},
             "releases": [{"version": "1.0.0", "proven_on": [6868]}]}
        p = store.Problems()
        store.check_claims(m, {}, p)
        self.assertTrue(any("[6871]" in x for x in p.items), p.items)

    def test_the_early_line_says_proven_and_pending(self):
        m = {"releases": [{"proven_on": [6868], "pending_on": [6871]}]}
        self.assertEqual(store.status_line(m), "Proven in game on Deadlock build 6868. The check on build 6871 is pending.")
        self.assertEqual(store.claims(store.status_line(m)), {6868})


class Collisions(Fixture):
    def test_one_slot_or_one_path_for_two_mods_fails(self):
        a, ia = manifest_for(self.zip, mod="demo")
        z2 = os.path.join(self.dir, "Other_1.0.0.zip")
        bundle_zip(z2, mod="other", files={"models/demo/demo.vmdl_c": b"theirs"})
        b, ib = manifest_for(z2, mod="other")
        p = store.Problems()
        store.check_collisions([a, b], {("demo", 0): ia, ("other", 0): ib}, p)
        self.assertTrue(any("pak10 is claimed by demo/demo, other/other" in x for x in p.items), p.items)
        self.assertTrue(any("models/demo/demo.vmdl_c is shipped by demo, other" in x for x in p.items), p.items)

    def test_different_slots_and_paths_pass(self):
        a, ia = manifest_for(self.zip, mod="demo")
        z2 = os.path.join(self.dir, "Other_1.0.0.zip")
        bundle_zip(z2, mod="other", files={"models/other/other.vmdl_c": b"theirs"}, slot=20)
        b, ib = manifest_for(z2, mod="other")
        p = store.Problems()
        store.check_collisions([a, b], {("demo", 0): ia, ("other", 0): ib}, p)
        self.assertEqual(p.items, [])


class PrivacyWall(unittest.TestCase):
    TERMS = ["secretname", "hushed"]

    def test_case_accents_and_separators_do_not_hide_a_name(self):
        for text in ("Secret_Name 1.0.0", "SECRET-NAME", "secret name", "Hüshed", "pak of hushed.vpk"):
            self.assertTrue(store.leaks(text, self.TERMS), text)
        self.assertFalse(store.leaks("Maid Lash: N7, Grand Service", self.TERMS))

    def test_the_report_names_the_place_not_the_name(self):
        p = store.Problems()
        store.scan_privacy([("README.md", "fine\nall fine\nthe secret name here\n")], self.TERMS, p)
        self.assertEqual(p.items, ["README.md: holds a withheld name (line 3)"])
        self.assertNotIn("secret", " ".join(p.items))

    def test_letters_scattered_through_binary_are_not_a_word(self):
        data = b"\x00h\x01u\x02s\x03h\x04e\x05d\x06" + b"\x00real hushed string\x00"
        text = store.readable(data)
        self.assertNotIn("h u", text)
        self.assertTrue(store.leaks(text, self.TERMS))
        self.assertFalse(store.leaks(store.readable(b"\x00h\x01u\x02s\x03h\x04e\x05d\x06"), self.TERMS))

    def test_no_terms_fails_closed(self):
        old = os.environ.pop("PRIVACY_WALL", None)
        try:
            self.assertEqual(store.withheld_terms(), [])
        finally:
            if old is not None:
                os.environ["PRIVACY_WALL"] = old


class RoutineGuard(Fixture):
    def setUp(self):
        super().setUp()
        m, _ = manifest_for(self.zip)
        del m["_rel"]
        self.before = json.dumps(m, indent=2)
        self.m = m

    def after(self, change):
        m = copy.deepcopy(self.m)
        change(m)
        return store.routine_diff("skins/demo/manifest.json", self.before, json.dumps(m, indent=2))

    def test_new_proofs_and_their_compatibility_are_routine(self):
        def change(m):
            m["proofs"] += proof_rows(6871, self.sha)
            m["releases"][0]["proven_on"] = [6871]
            m["releases"][0]["pending_on"] = []
            m["releases"][0]["valve_check"]["baseline_build"] = 6872
        self.assertEqual(self.after(change), [])

    def test_anything_else_is_not(self):
        self.assertEqual(self.after(lambda m: m["listing"].update(blurb="new words")), ["skins/demo/manifest.json: changes listing"])
        self.assertTrue(self.after(lambda m: m["releases"][0]["asset"].update(sha256="6" * 64)))
        self.assertTrue(self.after(lambda m: m["releases"].append(copy.deepcopy(m["releases"][0]))))
        self.assertTrue(self.after(lambda m: m.update(status="early")))

    def test_a_proof_row_may_be_added_never_edited_or_removed(self):
        m = copy.deepcopy(self.m)
        m["proofs"] = proof_rows(6868, self.sha)
        before = json.dumps(m)
        m["proofs"][0]["date"] = "2026-12-01"
        self.assertTrue(store.routine_diff("x", before, json.dumps(m)))


if __name__ == "__main__":
    unittest.main()
