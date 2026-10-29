#!/usr/bin/env python3
"""resident-firstboot: seed the intent log from /usr/share/resident/intents.json, adopt the state
each intent explains, save the drift baseline, mark done. Runs once as `agent` (RESIDENT_AUTHOR=image).
Idempotent: skips if /var/lib/resident/.seeded exists (the unit also has the Condition)."""
import json
import subprocess
import os
import sys

sys.path.insert(0, "/usr/lib/resident")
import agentos as resident  # noqa: E402  (ships at /usr/lib/resident/agentos.py)

ROOT = resident.ROOT
MARK = os.path.join(ROOT, ".seeded")
SEED = "/usr/share/resident/intents.json"


def main():
    if os.path.exists(MARK):
        return 0
    with open(SEED, "rb") as fh:
        seed = json.load(fh)
    os.environ.setdefault("RESIDENT_AUTHOR", "image")
    ids = {}
    for it in seed["intents"]:
        iid = resident.intent_add(it["text"], it.get("verify"))
        ids[it["key"]] = iid
        for ad in it.get("adopt", []):
            kinds = {ad["kind"]} if ad.get("kind") else None
            try:
                resident.intent_adopt(iid, ad["patterns"], kinds)
            except Exception as e:  # adoption is best-effort; the intent itself is what matters
                print(f"adopt for {it['key']} failed: {e}", file=sys.stderr)
        if it.get("close"):
            resident.intent_close(iid, "done")
    # Everything else present at first boot shipped in the read-only image itself, and that is its reason:
    # the base is verified and only ever replaced whole (A/B), so "part of image <version>" is literally true.
    # Adoption skips anything a specific intent above already explains, so this only fills the gaps; from here
    # on, anything new is real drift.
    ver = ""
    try:
        for line in open("/usr/lib/os-release"):
            if line.startswith("IMAGE_VERSION="):
                ver = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    base = resident.intent_add(f"Shipped in the Resident Linux image {ver}: part of the read-only base, "
                               "replaced only as a whole image (A/B update)", None)
    ids["base-image"] = base
    try:
        resident.intent_adopt(base, [".*"], None)
        # On-demand services (D-Bus or socket activated) are asleep at first boot but shipped all the same:
        # explain every service unit file in the image, so one waking up later is not mistaken for drift.
        out = subprocess.run(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--plain"],
                             capture_output=True, text=True).stdout
        c = resident.db()
        known = {r[0] for r in c.execute("SELECT key FROM changes WHERE kind='service'")}
        shipped = [(u, "adopted", "shipped unit file") for u in (l.split()[0] for l in out.splitlines() if l.split())
                   if u not in known]  # templates too: their instances are explained through them
        resident.record_changes(c, base, None, [("service", u, a, d) for u, a, d in shipped], resident.now())
        c.commit()
    except Exception as e:
        print(f"adopt for base-image failed: {e}", file=sys.stderr)
    resident.save_last_fp(resident.fingerprint())
    with open(os.path.join(ROOT, "seed-ids.json"), "w") as fh:
        json.dump({"image_version": seed.get("image_version"), "ids": ids}, fh, indent=1)
    with open(MARK, "w") as fh:
        fh.write(resident.now() + "\n")
    resident.emit_event("system", "first boot: intent log seeded", f"{len(ids)} intents from {SEED}")
    print(f"seeded {len(ids)} intents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
