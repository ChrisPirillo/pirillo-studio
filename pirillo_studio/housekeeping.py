"""Tick cleanup and Board publication with an explicit studio context `s`."""
import os
import shutil
import sys
import time


def tick_housekeeping(s, cfg, renewed, wake_only=False):
    """Cleanup has its own exclusion, not the ledger's. Record only cleanup's changed fields.
    Commands may add asks or answer existing ones while folders are measured or copied.
    Never save the old snapshot over those changes, and never replay filesystem moves.
    """
    import copy
    if wake_only and (not cfg.get("wake") or not shutil.which("pmset")):
        return
    slot = s.Sending("tick-housekeeping")
    if not slot.acquire():
        return
    try:
        with s.Lock():
            before = s.load()
        led = copy.deepcopy(before)
        for msg in renewed:
            s.log(led, msg)
        if wake_only:
            s.wake(cfg, led)
        else:
            s.tidy(cfg, led)
        with s.Lock():
            current = s.load()
            # tidy owns these completion markers; concurrent post states/words stay untouched.
            for slug, v in led["videos"].items():
                if slug not in current["videos"]:
                    continue
                for field in ("work_retired", "tidied", "records", "folder_ask"):
                    if v.get(field) != before["videos"][slug].get(field):
                        current["videos"][slug][field] = v[field]
            old_asks = {x["id"]: x for x in before.get("asks", [])}
            new_asks = {x["id"]: x for x in current.get("asks", [])}
            for ask in led.get("asks", []):
                old = old_asks.get(ask["id"])
                if old is None and ask["id"] not in new_asks:
                    current.setdefault("asks", []).append(ask)
                elif old and ask.get("acted") != old.get("acted") and new_asks.get(ask["id"]) == old:
                    new_asks[ask["id"]]["acted"] = ask["acted"]
            for aid in set(before.get("later", {})) - set(led.get("later", {})):
                if current.get("later", {}).get(aid) == before["later"][aid]:
                    current["later"].pop(aid, None)
            for stamp in led.get("wakes", []):
                if stamp not in current.setdefault("wakes", []):
                    current["wakes"].append(stamp)
            if led.get("wake_error") != before.get("wake_error"):
                current["wake_error"] = led["wake_error"]
            added_logs = led.get("log", [])[len(before.get("log", [])):]
            if added_logs:
                current.setdefault("log", []).extend(added_logs)
            s.save(current)
    finally:
        slot.release()


def tick_board(s, cfg):
    """Render to a private destination, then publish only if the ledger is still current.
    A command that answers while rendering always wins; retry against its new ledger, unless a command has published
    its own Board since this render began: then the tick's refresh is done (2026-10-17: asks acted on every one to
    three minutes, each rebuilding the Board, outran a 150 s render and the tick re-rendered for 42 minutes).
    Board rendering can add ledger notes, so those are saved with the same comparison.
    """
    import tempfile
    target = s.BOARD
    # beside the Board, since its links are relative to its folder (2026-10-21: drafted in data/, every tick Board linked one ../ too deep)
    fd, draft = tempfile.mkstemp(prefix=".tick-board-", suffix=".html", dir=os.path.dirname(target))
    os.close(fd)
    try:
        def build(led):
            s.BOARD = draft
            try:
                s.board(cfg, led)
            finally:
                s.BOARD = target
        # Publish and save together; unlike tick_update, the draft is not a ledger-only update.
        import copy
        while True:
            with s.Lock():
                before, began = s.load(), time.time()
            led = copy.deepcopy(before)
            build(led)
            with s.Lock():
                if s.load() != before:
                    if os.path.exists(target) and os.path.getmtime(target) >= began:
                        return   # shortcut: a Board begun before this snapshot can lack its saves until the next rebuild
                    continue
                if led != before:
                    s.save(led)
                if os.path.getsize(draft) < 1024:    # a build that wrote nothing never replaces the Board
                    print(f"tick_board: the draft was {os.path.getsize(draft)} bytes; the last Board stays "
                          "(2026-10-09: an empty Board.html was published at 13:55)", file=sys.stderr)
                    return
                os.replace(draft, target)
                return
    finally:
        for path in (draft, draft + ".tmp"):
            if os.path.exists(path):
                os.unlink(path)
