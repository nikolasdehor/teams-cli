#!/usr/bin/env python3
"""xteams falso para testes: responde JSON fixo por subcomando."""

import json
import os
import sys


def out(obj):
    sys.stdout.write(json.dumps(obj))


def main(args):
    args = [a for a in args if a != "-j"]
    if os.environ.get("FAKE_X_NOAUTH") and args[:2] != ["auth", "login"]:
        sys.stderr.write("Error: not signed in, run `xteams auth login`\n")
        return 1
    if os.environ.get("FAKE_X_KEYCHAIN"):
        sys.stderr.write(
            "Error: \n   0: \x1b[91mKeychain read failed: item not found\n"
            "\x1b[0m\nLocation:\n   src/creds/macos.rs:64\n"
        )
        return 1
    if args[:2] == ["auth", "status"]:
        out({"account": "user@work.com", "user": "user@work.com", "name": "Eu Mesmo", "mode": "device-code"})
        return 0
    if args[:2] in (["auth", "login"], ["auth", "logout"]):
        return 0
    if args[:2] == ["chat", "list"] and os.environ.get("FAKE_X_CHATS"):
        out(json.load(open(os.environ["FAKE_X_CHATS"])))
        return 0
    if args[:2] == ["chat", "list"]:
        n = int(args[args.index("-n") + 1]) if "-n" in args else 20
        out([{"id": f"c{i}", "topic": f"Chat {i}"} for i in range(n)])
        return 0
    if args[:2] == ["team", "list"]:
        out([{"id": "t1", "displayName": "Time"}])
        return 0
    if args[:2] == ["channel", "list"]:
        out([{"id": "19:c@thread.tacv2", "displayName": "Geral"}])
        return 0
    if args[:2] == ["message", "read"] and os.environ.get("FAKE_X_MSGS"):
        found = [m for m in json.load(open(os.environ["FAKE_X_MSGS"])) if m["id"] == args[3]]
        if not found:
            sys.stderr.write("Error: message not found\n")
            return 1
        out(found[0])
        return 0
    if args[:2] == ["message", "list"] and os.environ.get("FAKE_X_MSGS"):
        # fixture: devolve as últimas N, na ordem do arquivo (pode vir embaralhada/duplicada)
        n = int(args[args.index("-n") + 1]) if "-n" in args else 20
        out(json.load(open(os.environ["FAKE_X_MSGS"]))[-n:])
        return 0
    if args[:2] == ["message", "list"]:
        n = int(args[args.index("-n") + 1]) if "-n" in args else 20
        fmt = args[args.index("-O") + 1] if "-O" in args else "markdown"
        out([{"id": f"m{i}", "conversation": args[2], "format": fmt} for i in range(n)])
        return 0
    if args[:2] == ["message", "new"]:
        text = args[args.index("--content") + 1]
        fmt = "html" if "-I" in args and args[args.index("-I") + 1] == "html" else "markdown"
        out({"id": "m9", "conversation": args[2], "content": text, "format": fmt})
        return 0
    if args[:2] == ["user", "search"] and os.environ.get("FAKE_X_USERS"):
        out(json.load(open(os.environ["FAKE_X_USERS"])).get(args[2], []))
        return 0
    if args[:2] == ["user", "search"]:
        out([{"displayName": "Ana", "query": args[2]}])
        return 0
    if args[:2] == ["calendar", "upcoming"]:
        days = args[args.index("-d") + 1] if "-d" in args else "7"
        out([{"subject": "Daily", "days": days}])
        return 0
    sys.stderr.write(f"fake: comando desconhecido {args}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
