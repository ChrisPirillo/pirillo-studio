"""image_approval_proof, image_approval_block, TURN_DECISIONS_FROM, turn_decisions_live: moved from the host by dev/port.py; `s` is the host (pirillo_studio.host.View)."""


def image_approval_proof(s, image, sha, cfg, led, evidence=None, seen=None):
    """Resolve direct approval or an exactly replayed pure crop of an approved parent."""
    proof = led.get("image_approvals", {}).get(sha) or evidence or {}
    if sha in s.GRANDFATHERED_IMAGES or (proof.get("at") and
            str(proof.get("board_src", "")).endswith("?sha256=" + str(sha))):
        return True
    if not s.crop_gate_on(cfg):
        return False
    seen = set() if seen is None else seen
    if sha in seen or len(seen) >= 8:
        return False
    seen.add(sha)
    parent = s.crop_parent(image, sha)
    return bool(parent and image_approval_proof(s, parent[0], parent[1], cfg, led, seen=seen))


def image_approval_block(s, a, cfg=None, led=None):
    if not s.outgoing_images(a) or not s.image_gate_on(cfg):
        return None
    if a.get("method") == "hand":       # the owner posts it by hand, looking at the image: their Posted is the approval
        return None
    ledger = led if led is not None else s.load()
    config = cfg if cfg is not None else s.cfg_now()
    for image in s.outgoing_images(a):
        sha = s.story_image_hash(image)
        if not sha:
            return "its still is missing"
        if sha in s.GRANDFATHERED_IMAGES:
            continue
        if not image_approval_proof(s, image, sha, config, ledger, a.get("image_approval")):
            return s.IMAGE_APPROVAL_REASON
    return None


TURN_DECISIONS_FROM = "2026-10-07T00:00:00-07:00"   # gates.from "board-turn-decisions"


def turn_decisions_live(s, cfg, t=None):
    """The owner, 2026-10-07: every row in Your turn is a decision of theirs. From gates.from "board-turn-decisions": a post
    held for its image's approval is one ask with the image (never a bare task), an expired or blocked vault option
    and a text kept for a crew check leave Your turn, and an image hold is never a fault."""
    start = (((cfg or {}).get("gates") or {}).get("from") or {}).get("board-turn-decisions", TURN_DECISIONS_FROM)
    return (t or s.now()) >= s._when(start)
