"""The one boundary between the core and the host studio that runs it (docs/architecture.md).

The core reads its host only as `s.<name>`, and only the names in NEEDS. ledger, paths, clock and adapters are the
public host interface; HOST_IMPORTS, imports (modules the core still reaches through the host) and callbacks (host code whose domain
is not ported yet) are the port's remaining debt, and their count is its progress number. dev/check.py needs keeps
NEEDS exact; dev/port.py move adds what a move reads.
"""
NEEDS = {
    "ledger": ("assets", "load", "Lock", "log", "move", "row", "save"),
    "paths": ("BOARD", "CONFIG", "content_dir", "DATA_DIR", "HOME", "NEW", "RULES"),
    "clock": ("iso", "local", "now", "parse", "tz"),
    "adapters": ("AdapterError", "ADAPTERS"),
    "imports": ("contextlib", "counts", "drop_times", "dt", "facebook_confirm", "facebook_posts", "hashlib",
        "json", "mimetypes", "os", "re", "subprocess", "sys", "time", "urllib", "uuid"),
    "callbacks": ("_at", "_brief", "_form", "_g", "_http", "_multipart", "_opening", "_probe_secs", "_when",
        "against_usual", "anchored", "ANSWERED", "api_text_commit", "api_text_image", "API_VIDEO_BYTES",
        "approval_after", "approval_check", "approval_opened", "approval_send_block", "ask_cmd", "ASK_STUDIO",
        "ask_title", "asset_file_label", "at", "b5_meter", "BEAT", "board", "can_broll", "can_move", "cfg_now",
        "cover_note", "COVER_PLATFORMS", "crop_gate_on", "crop_parent", "deadline", "DEFAULTS", "digest_adapter",
        "digest_allowed", "digest_chrome_note", "due", "duration_route", "effect_check", "effect_checkpoint",
        "effect_pending", "effect_recover", "effect_route_changes", "effect_send", "effect_source",
        "effects_enabled", "facebook_profile_story", "FB_API", "FIT_STEP_MIN", "fit_video", "fitted",
        "freeze_posting_images", "frozen_image", "GRANDFATHERED_IMAGES", "has_long", "headline_gate_problems",
        "holds_refresh", "IG_API", "image_approval_asks", "image_approval_block", "IMAGE_APPROVAL_REASON",
        "image_gate_on", "key", "key_age_days", "KEY_SERVICE", "keychain", "lacks_cover_words", "late_still",
        "lead", "link_plan", "link_problem", "LIVE", "load_config", "LockStuck", "long_url", "MASTODON_SETTLE",
        "MASTODON_SOON", "MASTODON_WINDOW", "media_cleanup", "media_host_short", "method_for", "morning_card",
        "MORNING_RUN", "NO_BROWSER", "on_fit_route", "only_installed", "open_asks", "outgoing_images", "OWNER_IN_IMAGE",
        "plain_html", "plat_name", "prepare_video_fit", "public_url", "publishing_row", "publishing_started",
        "queue_place", "rb", "record_scheduled_images", "Refused", "release_path", "render", "rj", "round5",
        "route_row", "scheduled_image_job", "seconds", "send", "SendDeadline", "Sending", "stage", "still_alt",
        "story_adapter", "story_cfg", "story_due", "story_has_promised_still", "story_image_hash",
        "STORY_IMAGE_MISSING", "story_image_ready", "story_job", "story_post_rows", "story_send_job",
        "STORY_STALE", "story_sweep", "story_text", "story_unreported", "sync_publishing_routes", "TH_API",
        "tick_update", "tidy", "TIKTOK_DRAFT", "tip_sync_row", "tips_add", "tips_cfg", "tips_refill", "too_long",
        "TOPICS", "try_cover", "turn_decisions_live", "unok_said", "vault_alt", "vault_limit",
        "vault_link_problem", "vault_row_option", "vault_still", "vault_sweep", "vault_video_url", "video_fit_cap",
        "VIDEO_FIT_PENDING", "video_probe", "WAITS_FOR_YOUTUBE", "wake", "weekend_enqueue", "wild_image_post",
        "x_engagement", "yt_access", "YT_API", "yt_api_route", "YT_POST_NOTE", "YT_REVOKED", "yt_signed_in",
        "YT_SIGNIN"),
}
HOST_IMPORTS = ("wordcopy",)   # host modules the core still imports, without s; dev/check.py refuses any other
NAMES = frozenset(n for group in NEEDS.values() for n in group)


class View:
    """The host as the core sees it: live reads and writes of NEEDS names (a test's patch is seen), nothing else."""

    def __init__(self, host):
        missing = sorted(n for n in NAMES if not hasattr(host, n))
        if missing:   # at build time, never mid-send
            raise AttributeError(f"the host lacks NEEDS names: {', '.join(missing)}")
        object.__setattr__(self, "_host", host)

    def __getattr__(self, name):
        if name not in NAMES:
            raise AttributeError(f"{name} is not in pirillo_studio.host NEEDS")
        return getattr(self._host, name)

    def __setattr__(self, name, value):   # housekeeping points s.BOARD at a draft while it renders
        if name not in NAMES:
            raise AttributeError(f"{name} is not in pirillo_studio.host NEEDS")
        setattr(self._host, name, value)
