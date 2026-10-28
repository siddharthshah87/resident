#!/usr/bin/env python3
"""resident-firstboot: seed the intent log from /usr/share/resident/intents.json, adopt the state
each intent explains, save the drift baseline, mark done. Runs once as `agent` (RESIDENT_AUTHOR=image).
Idempotent: skips if /var/lib/resident/.seeded exists (the unit also has the Condition)."""
import json
import os
import sys

sys.path.insert(0, "/usr/lib/resident")
import resident  # noqa: E402  (ships at /usr/lib/resident/agentos.py)

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
