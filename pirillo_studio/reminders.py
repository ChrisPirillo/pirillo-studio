"""Hand-post reminders sent through the owner's mail account.

`s` is the studio module whose ledger and posting helpers the clock uses.
"""
import datetime as dt
import json
import os
import subprocess


MAIL_SCRIPT = """on run argv
tell application "Mail"
set m to make new outgoing message with properties {subject:item 1 of argv, content:item 2 of argv, visible:false}
tell m to make new to recipient at end of to recipients with properties {address:item 3 of argv}
if (count of argv) > 3 then set sender of m to item 4 of argv
send m
end tell
end run"""


def send_mail(subject, body, to, sender=None):
    if os.environ.get("STUDIO_FAKE_MAIL"):                 # tests: append to a file instead
        with open(os.environ["STUDIO_FAKE_MAIL"], "a") as f:
            f.write(json.dumps([subject, body, to]) + "\n")
        return True
    args = ["osascript", "-", subject, body, to] + ([sender] if sender else [])
    r = subprocess.run(args, input=MAIL_SCRIPT, capture_output=True, text=True, timeout=120)
    return r.returncode == 0


def remind(s, cfg):
    """Picks the rows under the ledger lock and marks them reminding_at (so an overlapping tick
    leaves them), mails with the lock released, then records each result under it again."""
    s.only_installed("remind")
    rc = cfg.get("reminders") or {}
    if not rc.get("to"):
        return
    out = []
    with s.Lock():
        led = s.load()
        t = s.now()
        for v, a in s.assets(led):
            if a.get("reminded_at") or not v.get("approved") or not a.get("scheduled_for"):
                continue
            if a.get("reminding_at") and s.parse(a["reminding_at"]) > t - dt.timedelta(minutes=15):
                continue                        # another tick is mailing it right now
            his = (a["method"] == "hand" and a["state"] == "planned" and s.parse(a["scheduled_for"]) <= t) or \
                  (a["state"] == "handoff" and a.get("block") == s.TIKTOK_DRAFT)
            if not his:
                continue
            text, _ = s.render(v, a, cfg)
            if text is None:
                continue
            a["reminding_at"] = s.iso(t)
            out.append((a["id"], f"{s.plat_name(a['platform'])}: {a['title']}", text))
        if out:
            s.save(led)
    for aid, subject, text in out:
        ok = send_mail(subject, text, rc["to"], rc.get("from"))
        with s.Lock():
            led = s.load()
            _, a = s.row(led, aid)
            a.pop("reminding_at", None)
            if ok:
                a["reminded_at"] = s.iso(s.now())
                s.log(led, f"{aid}: reminder emailed")
            else:
                s.log(led, f"{aid}: reminder email failed; the next run tries again")
            s.save(led)
