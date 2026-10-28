#!/usr/bin/env python3
"""policy.py — the (tiny) broker. Reads /etc/resident/policy.toml and answers one question:
may the agent do <verb> on <resource>?   exit 0 = allowed, 1 = denied, 3 = approval required.
Used by privileged.sh before every privileged action. stdlib only (tomllib: python >= 3.11)."""
import fnmatch
import re
import sys
import tomllib

POLICY = "/etc/resident/policy.toml"


def load():
    with open(POLICY, "rb") as fh:
        return tomllib.load(fh)


def check(pol, verb, resource):
    approvals = {a["verb"] for a in pol.get("approval", {}).get("required", [])}
    if verb in approvals:
        return 3, f"'{verb}' requires human approval: {next(a['why'] for a in pol['approval']['required'] if a['verb'] == verb)}"
    if verb == "install":
        pk = pol["resources"]["packages"]
        if "install" not in pk["verbs"]:
            return 1, "install not granted"
        if not re.match(pk["name_pattern"], resource):
            return 1, f"bad package name {resource!r}"
        for pat in pk.get("deny", []):
            if fnmatch.fnmatch(resource, pat):
                return 1, f"{resource} is in the trust base (deny: {pat})"
        return 0, "ok"
    if verb in ("start", "stop", "restart", "reload", "status"):
        un = pol["resources"]["units"]
        if verb not in un["verbs"]:
            return 1, f"verb {verb} not granted on units"
        if verb in un.get("deny_verbs", []):
            return 3, f"{verb} changes what boots"
        if not re.match(r"^[A-Za-z0-9@._:\\-]+$", resource):
            return 1, f"bad unit name {resource!r}"
        if not re.search(un["manage"], resource):
            return 1, f"{resource} is not one of the agent's units ({un['manage']})"
        return 0, "ok"
    if verb in ("enable", "disable", "mask", "daemon-reload"):
        return 3, "unit-enable requires approval"
    return 1, f"unknown verb {verb!r}"


def main(argv):
    if len(argv) < 3:
        print("usage: policy.py <verb> <resource>", file=sys.stderr)
        return 2
    code, why = check(load(), argv[1], argv[2])
    print(why)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
