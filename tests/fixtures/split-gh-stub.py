#!/usr/bin/env python3
"""Fake `gh` for tests/test-split.sh: a tiny stateful GitHub (issues, labels, sub-issues, one
Project). State lives in $FAKE_DIR/state.json; every call is logged to $FAKE_LOG as
`R <args>` (read) or `W <args>` (write). Failure injection: state["fail"]["create:<repo>"] = k
fails the next k creates in that repo."""
import json
import os
import re
import sys

DIR = os.environ["FAKE_DIR"]
STATE = os.path.join(DIR, "state.json")
args = sys.argv[1:]
st = json.load(open(STATE, encoding="utf-8"))


def save():
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)


def log(kind):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(kind + " " + " ".join(a.replace("\n", "\\n") for a in args) + "\n")


def flag(name):
    return args[args.index(name) + 1] if name in args else None


def var(name):
    for i, a in enumerate(args):
        if a in ("-f", "-F") and i + 1 < len(args) and args[i + 1].startswith(name + "="):
            return args[i + 1][len(name) + 1:]
    return None


def key(repo, n):
    return f"{repo.lower()}#{n}"


def get(repo, n):
    k = key(repo, n)
    seen = 0
    while k in st["issues"] and st["issues"][k].get("moved_to") and seen < 5:
        k, seen = st["issues"][k]["moved_to"], seen + 1
    return st["issues"].get(k)


def by_id(node_id):
    return next((i for i in st["issues"].values() if i["id"] == node_id and not i.get("moved_to")), None)


def node(i):
    p = st["project"]
    items = [] if not i.get("item") else [{"id": i["item"]["id"], "project": {"number": p["number"], "owner": {"login": p["owner"]}},
                                            "fieldValueByName": ({"name": i["item"]["status"]} if i["item"].get("status") else None)}]
    subs = [get(*s.split("#")) for s in i.get("subIssues", [])]
    return {"id": i["id"], "number": i["number"], "title": i["title"], "body": i["body"], "state": i["state"],
            "stateReason": i.get("stateReason"), "url": f"https://github.com/{i['repo']}/issues/{i['number']}",
            "repository": {"nameWithOwner": i["repo"]},
            "labels": {"nodes": [{"name": l} for l in i["labels"]]},
            "subIssues": {"nodes": [{"id": s["id"], "number": s["number"], "title": s["title"], "state": s["state"],
                                     "stateReason": s.get("stateReason"), "url": "",
                                     "repository": {"nameWithOwner": s["repo"]}} for s in subs if s]},
            "projectItems": {"nodes": items},
            "comments": {"nodes": [{"databaseId": c["id"], "body": c["body"], "createdAt": "2026-10-19T00:00:00Z",
                                    "author": {"login": c.get("author", "bot")}} for c in i["comments"]]}}


def new_issue(repo, title, body, labels):
    st["next"][repo.lower()] = st["next"].get(repo.lower(), 100) + 1
    n = st["next"][repo.lower()]
    st["seq"] += 1
    i = {"repo": repo, "number": n, "id": f"I_{repo.replace('/', '_')}_{n}", "title": title, "body": body,
         "state": "OPEN", "labels": labels, "comments": [], "subIssues": [], "item": None}
    st["issues"][key(repo, n)] = i
    return i


def out(value):
    print(json.dumps(value))


a2 = " ".join(args[:2])
if args[:2] == ["api", "rate_limit"]:
    log("R"); out({"resources": {"graphql": {"remaining": 5000, "reset": 0, "limit": 5000}, "core": {"remaining": 5000, "reset": 0, "limit": 5000}}})
elif args[:2] == ["api", "graphql"]:
    q = var("query") or ""
    if "mutation" in q:
        log("W")
        if "addSubIssue" in q:
            parent, child = by_id(var("p")), by_id(var("c"))
            if not parent or not child:
                out({"errors": [{"message": "not found"}]}); sys.exit(1)
            ck = key(child["repo"], child["number"])
            if ck not in parent["subIssues"]:
                parent["subIssues"].append(ck)
            out({"data": {"addSubIssue": {"subIssue": {"number": child["number"]}}}})
        elif "updateProjectV2ItemFieldValue" in q:
            opts = {v: k for k, v in st["options"].items()}
            item = next(i for i in st["issues"].values() if i.get("item") and i["item"]["id"] == var("i"))
            item["item"]["status"] = opts[var("o")]
            out({"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": var("i")}}}})
        save()
    else:
        log("R")
        if "rateLimit" in q and "SplitIssue" not in q:
            out({"data": {"rateLimit": {"limit": 5000, "remaining": 5000, "used": 0, "resetAt": "2030-01-01T00:00:00Z"}}})
        elif "SplitIssue" in q:
            i = get(f"{var('owner')}/{var('name')}", var("n"))
            out({"data": {"repository": {"issue": node(i) if i else None}}})
        elif "SplitParents" in q:
            hub = f"{var('owner')}/{var('name')}".lower()
            nodes = [node(i) for i in st["issues"].values() if i["repo"].lower() == hub and i["state"] == "OPEN" and not i.get("moved_to")]
            out([{"data": {"repository": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}])
        elif 'field(name:"Status")' in q:
            out({"data": {"repositoryOwner": {"projectV2": {"id": "PVT_1", "field": {"id": "F_1",
                 "options": [{"id": v, "name": k} for k, v in st["options"].items()]}}}}})
        elif "issueOrPullRequest" in q:
            i = get(f"{var('owner')}/{var('name')}", var("issue"))
            out({"data": {"repository": {"issueOrPullRequest": {"projectItems": node(i)["projectItems"]}}}})
        else:
            print("unexpected graphql", file=sys.stderr); sys.exit(1)
elif args[0] == "api" and "PATCH" in args:
    log("W")
    m = re.search(r"repos/([^/]+/[^/]+)/issues/comments/([0-9]+)", " ".join(args))
    body = var("body")
    for i in st["issues"].values():
        for c in i["comments"]:
            if c["id"] == int(m.group(2)):
                c["body"] = body
    save(); out({})
elif args[0] == "api" and any("/labels" in a for a in args):
    log("R")
    repo = re.search(r"repos/([^/]+/[^/?]+)/labels", " ".join(args)).group(1).lower()
    out([[{"name": l} for l in st["labels"].get(repo, [])]])
elif args[0] == "api" and args[-1].endswith("/permission"):
    log("R")
    login = args[-1].split("/")[-2]
    out({"permission": st["perms"].get(login, "read")})
elif a2 == "issue transfer":
    log("W")
    i, dest = get(flag("--repo"), args[2]), args[3]
    moved = new_issue(dest, i["title"], i["body"], [])
    moved["comments"], moved["item"] = i["comments"], i["item"] if not i.get("drop_item") else None
    i["moved_to"] = key(dest, moved["number"])
    save(); print(f"https://github.com/{dest}/issues/{moved['number']}")
elif a2 == "issue edit":
    log("W")
    i = get(flag("--repo"), args[2])
    for l in (flag("--add-label") or "").split(","):
        if l and l not in i["labels"]:
            i["labels"].append(l)
    save()
elif a2 == "issue comment":
    log("W")
    i = get(flag("--repo"), args[2])
    st["seq"] += 1
    body = open(flag("--body-file"), encoding="utf-8").read()
    i["comments"].append({"id": 9000 + st["seq"], "body": body, "author": "bot"})
    save(); print(f"https://github.com/{i['repo']}/issues/{i['number']}#issuecomment-{9000 + st['seq']}")
elif a2 == "issue create":
    log("W")
    repo = flag("--repo")
    fk = f"create:{repo.lower()}"
    if st["fail"].get(fk):
        st["fail"][fk] -= 1; save()
        print("HTTP 502: Bad Gateway", file=sys.stderr); sys.exit(1)
    labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
    i = new_issue(repo, flag("--title"), open(flag("--body-file"), encoding="utf-8").read(), labels)
    save(); print(f"https://github.com/{repo}/issues/{i['number']}")
elif a2 == "issue close":
    log("W")
    i = get(flag("--repo"), args[2])
    i["state"], i["stateReason"] = "CLOSED", (flag("--reason") or "completed").upper()
    save()
elif a2 == "project item-add":
    log("W")
    m = re.search(r"github\.com/([^/]+/[^/]+)/issues/([0-9]+)", flag("--url"))
    i = get(m.group(1), m.group(2))
    i["item"] = {"id": f"PVTI_{i['id']}", "status": ""}
    save(); out({"id": i["item"]["id"]})
elif a2 == "label create":
    log("W")
    st["labels"].setdefault(flag("--repo").lower(), []).append(args[2])
    save()
else:
    log("?")
    print(f"unexpected gh call: {args}", file=sys.stderr)
    sys.exit(1)
