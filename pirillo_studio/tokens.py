"""Instagram's and Threads' 60-day tokens, renewed by the posting clock on every tick.

`s` is the studio whose Keychain these are: production passes its own studio module (tools/studio.py). It gives
key, key_age_days, keychain and KEY_SERVICE.
"""
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

# Instagram and Threads renew themselves (any time after a day old); LinkedIn's personal apps get no refresh token,
# so the Board asks the owner to make a new one before it lapses (studio.py TOKEN_DAYS).
REFRESH = {"instagram": "https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token",
           "threads": "https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token"}


def refresh_tokens(s):
    """Renew Instagram's and Threads' tokens once they're 20 days old (60-day lifetime), straight
    from the Keychain to Meta and back; never printed or logged. Runs without the ledger lock and
    returns the lines for the ledger's log (tick writes them)."""
    msgs = []
    for p, url in REFRESH.items():
        age = s.key_age_days(p)
        if age is None or age < 20:
            continue
        token = s.key(p)
        new = None
        if not token:
            continue
        try:
            with urllib.request.urlopen(f"{url}&access_token={urllib.parse.quote(token)}", timeout=30) as r:
                new = json.loads(r.read()).get("access_token")
            if new and s.keychain():
                subprocess.run(["security", "add-generic-password", "-U", "-s", s.KEY_SERVICE, "-a", p, "-w", new],
                               capture_output=True, timeout=20)
                msgs.append(f"{p}: token renewed (60 more days)")
        except (urllib.error.URLError, OSError, ValueError, subprocess.TimeoutExpired) as e:
            msgs.append(f"{p}: token renewal failed ({str(e)[:80]}); the next run tries again")
        finally:
            token = new = None
    return msgs
