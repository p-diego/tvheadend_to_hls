"""TVHeadend API client + the channel/EPG domain objects."""
import time

import requests
from requests.auth import HTTPDigestAuth

from .config import config, tvh_base_url
from .flags import guess_country, service_av
from .streams import TVChannel


# requests has no default timeout: without one, a slow/unresponsive TVHeadend
# hangs this call forever. Since check_status's background loop is single
# threaded, a hung EPG refetch for any one channel freezes the idle-kill sweep
# for every channel, so ffmpeg never gets stopped even when nothing accessed
# its playlist.
TVHEADEND_TIMEOUT = 10


def tvheadend_get(url):
    """GET a TVHeadend JSON endpoint and return the parsed body."""
    req = requests.get(
        url,
        auth=HTTPDigestAuth(config["tvheadend_user"], config["tvheadend_pass"]),
        timeout=TVHEADEND_TIMEOUT,
    )
    req.encoding = "UTF-8"
    if req.status_code != 200:
        print("TVHeadend %s returned HTTP %s" % (url, req.status_code))
        raise SystemExit(1)
    return req.json()


# Deduplication counter for clean_name(): tracks how many times a sanitized
# name has been seen so collisions get a "-N" suffix.
_clean_name_counter = {}


def clean_name(name):
    """Sanitize a channel name to a filename-safe ID, de-duplicating collisions."""
    out = "".join(
        c if ("A" <= c <= "Z" or "0" <= c <= "9") else ("_" if c == " " else "")
        for c in name.upper()
    )
    if len(out) < 2:
        out = "INVALID"
    seen = _clean_name_counter.get(out, 0) + 1
    _clean_name_counter[out] = seen
    return out if seen == 1 else "%s-%d" % (out, seen)


class tv_channel_epg:
    # When a flagged channel has run dry, don't refetch from TVHeadend more
    # often than this (seconds) — the background loop ticks every second and we
    # don't want to hammer the server while it genuinely has no fresh events.
    REFETCH_INTERVAL = 60

    def __init__(self, uuid, event_hash):
        self.uuid = uuid
        self.now = None
        self.events = {}
        self.last_fetch = time.time()
        self.add(event_hash)

    def add(self, event_hash):
        eventid = event_hash["eventId"]
        event_hash["start"] = int(event_hash["start"])
        event_hash["stop"] = int(event_hash["stop"])
        if event_hash["stop"] < time.time():
            return
        self.events[eventid] = event_hash
        if (
            self.now is None
            or self.now not in self.events
            or self.events[eventid]["start"] < self.events[self.now]["start"]
        ):
            self.now = eventid

    def _prune(self):
        """Drop events that have already ended, advancing `self.now` along the
        chain. Leaves `self.now = None` when no current event remains."""
        while (
            self.now is not None
            and self.now in self.events
            and self.events[self.now]["stop"] < time.time()
        ):
            ev = self.events[self.now]
            del self.events[self.now]
            self.now = ev.get("nextEventId")
        if self.now not in self.events:
            self.now = None

    def has_events(self):
        """True if a current/upcoming event remains after pruning ended ones."""
        self._prune()
        return self.now is not None

    def refetch(self, n=20):
        """Fetch fresh EPG events for this channel from TVHeadend."""
        self.last_fetch = time.time()  # set first so the throttle holds even on error
        epg_json = tvheadend_get(
            tvh_base_url
            + "/api/epg/events/grid?limit=" + str(max(n, 20))
            + "&channel=" + self.uuid
        )
        for event in epg_json["entries"]:
            if event["channelUuid"] == self.uuid:
                self.add(event)

    def update(self):
        """Prune ended events; if the channel has run dry, refetch (throttled).

        Returns True if a current event remains afterwards.
        """
        self._prune()
        if self.now is None and time.time() - self.last_fetch >= self.REFETCH_INTERVAL:
            try:
                self.refetch()
            except Exception:
                pass
        return self.now is not None

    def _upcoming(self, n):
        out = []
        cur_id = self.now
        while cur_id is not None and cur_id in self.events and len(out) < n:
            out.append(self.events[cur_id])
            cur_id = self.events[cur_id].get("nextEventId")
        return out

    def get_entries(self, n):
        """Up to n upcoming events; refetches from TVHeadend if fewer are linked."""
        try:
            self._prune()
        except Exception:
            pass
        entries = self._upcoming(n)
        if len(entries) < n and time.time() - self.last_fetch >= self.REFETCH_INTERVAL:
            try:
                self.refetch(n)
            except Exception:
                pass
            entries = self._upcoming(n)
        return entries


# Channel names that we always skip — uplink test feeds and IPTV-only feeds we can't stream.
_SKIP_PREFIXES = ("ALT_", "ARD-Test", "Kabelio ")
_SKIP_SUFFIXES = ("(Internet)",)


def _should_skip(name):
    if name == "{name-not-set}":
        return True
    if name.startswith(_SKIP_PREFIXES):
        return True
    if name.endswith(_SKIP_SUFFIXES):
        return True
    return False


def _load_services_by_uuid():
    """Map service UUID → service record (for provider lookup). Empty dict if unavailable."""
    try:
        grid = tvheadend_get(tvh_base_url + "/api/mpegts/service/grid?limit=99999")
        return {s["uuid"]: s for s in grid["entries"]}
    except (Exception, SystemExit):
        return {}


def _load_tags():
    """Return (channel_tags_by_uuid, tv_tag_uuid, radio_tag_uuid)."""
    grid = tvheadend_get(tvh_base_url + "/api/channeltag/list")
    tags_by_uuid = {t["key"]: t["val"] for t in grid["entries"]}
    tv_tag = next((k for k, v in tags_by_uuid.items() if v == "TV channels"), None)
    radio_tag = next((k for k, v in tags_by_uuid.items() if v == "Radio channels"), None)
    return tags_by_uuid, tv_tag, radio_tag


def _channel_providers(channel, services_by_uuid):
    """Sorted-unique provider strings across the channel's linked services."""
    return sorted({
        services_by_uuid[su]["provider"]
        for su in (channel.get("services") or [])
        if su in services_by_uuid and services_by_uuid[su].get("provider")
    })


def tvheadend_get_channel_list():
    """Fetch every TV channel from TVHeadend, build TVChannel objects with country/provider info.

    Returns (sorted_channel_list, by_hls_uuid).
    """
    channels = tvheadend_get(tvh_base_url + "/api/channel/grid?limit=99999")["entries"]
    services_by_uuid = _load_services_by_uuid()
    channel_tags, tv_tag, radio_tag = _load_tags()

    channel_list = []
    for channel in channels:
        name = channel["name"]
        if _should_skip(name):
            continue
        tag_ids = channel.get("tags") or []
        is_radio = radio_tag is not None and radio_tag in tag_ids
        if is_radio and not config["include_radio"]:
            continue

        tag_display_names = []
        non_tv_tag_names = []  # tag display names except the umbrella "TV channels" tag
        for t in tag_ids:
            display = channel_tags.get(t)
            if display is None:
                continue
            tag_display_names.append(display)
            if t != tv_tag:
                non_tv_tag_names.append(display)
        tags = "(" + ", ".join(non_tv_tag_names) + ")" if non_tv_tag_names else ""

        providers = _channel_providers(channel, services_by_uuid)
        provider = ", ".join(providers) if providers else None
        country = guess_country(channel, services_by_uuid, tag_display_names, provider=provider)
        # Synthesize a dummy video only with positive evidence of audio-without-video
        # (or an explicit Radio tag). Unknown metadata → treat as a normal TV channel.
        has_video, has_audio = service_av(channel, services_by_uuid)
        audio_only = is_radio or (has_video is False and has_audio)
        channel_list.append(TVChannel(
            name, tags, channel["number"], channel["uuid"], clean_name(name),
            country=country, provider=provider, audio_only=audio_only,
        ))

    by_hls_uuid = {ch.hls_uuid: ch for ch in channel_list}
    channel_list.sort(key=lambda x: getattr(x, config["sort"]))
    return channel_list, by_hls_uuid
