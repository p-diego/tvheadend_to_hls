import math
import os
import re
import subprocess
import time
import traceback

from .config import config, tvh_base_url_auth
from .flags import flag_emoji


def _abr_ladder():
    """Geometric series of (bitrate_bps, target_height) pairs, highest first.

    Bitrate ranges from config["min_bitrate"] to config["max_bitrate"];
    `config["num_streams"]` entries are produced. Heights are derived from
    bitrate via a simple bits-per-pixel-per-frame model (bpp≈0.085 @ 25 fps),
    then snapped to a multiple of 8 (h.264 likes even dimensions).
    """
    n = max(1, config["num_streams"])
    lo = max(1, config["min_bitrate"])
    hi = max(lo, config["max_bitrate"])
    if n == 1:
        bitrates = [hi]
    else:
        ratio = (hi / lo) ** (1.0 / (n - 1))
        bitrates = [int(lo * (ratio ** i)) for i in range(n)]
        bitrates[0] = lo   # force exact endpoints — geometric rounding may drift by 1
        bitrates[-1] = hi
    bitrates.sort(reverse=True)
    return [(b, _height_for_bitrate(b)) for b in bitrates]


def _height_for_bitrate(bps):
    # bits per pixel per frame ≈ 0.085 for h.264 at typical broadcast quality.
    pixels = bps / (25 * 0.085)
    h = math.sqrt(pixels * 9 / 16)            # 16:9 height
    return max(72, int(round(h / 8) * 8))     # round to a multiple of 8


def _scale_spec(scale_filter, target_h):
    """Scale filter that fits target_h height with source aspect, never upscaling.

    `min(target_h, ih)` clamps to source height; the width is derived from
    source aspect so the picture isn't stretched, rounded down to even.
    """
    return (
        "%s=w='trunc(min(%d,ih)*iw/ih/2)*2':h='min(%d,ih)'"
        % (scale_filter, target_h, target_h)
    )


def build_codecs():
    """Build the per-output ffmpeg args using a shared filter graph.

    Returns (hwaccel_args, video_args, n_outputs, var_stream_map):
      - hwaccel_args: input-side flags (before -i)
      - video_args: -filter_complex + per-output -map / -c:v / -b:v sequences,
        ending with a 'copy' output that packet-copies the source video
      - n_outputs: total number of video outputs (transcoded + copy)
      - var_stream_map: value for ffmpeg's -var_stream_map (HLS master)

    A single `-filter_complex` graph decodes & deinterlaces the source *once*,
    then splits the result to N parallel scalers — one per transcoded variant.
    Compared to the previous per-variant `-filter:v:N` args, this halves the
    decode/deinterlace work and keeps one less copy of the frame in GPU memory.
    """
    hwaccel = config["hwaccel"] == "vaapi"
    if hwaccel:
        deinterlace = "deinterlace_vaapi"
        scale_filter = "scale_vaapi"
        encoder = "h264_vaapi"
        hwaccel_args = [
            "-hwaccel", "vaapi",
            "-vaapi_device", config["vaapi_device"],
            "-hwaccel_output_format", "vaapi",
        ]
    else:
        deinterlace = "yadif"
        scale_filter = "scale"
        encoder = "libx264"
        hwaccel_args = []

    ladder = _abr_ladder()
    n_scaled = len(ladder)

    # Filter graph: decode + deinterlace once, then split into per-variant scalers.
    if n_scaled == 1:
        # split=1 is a degenerate no-op; build a single chain instead.
        target_h = ladder[0][1]
        filter_complex = "[0:v]%s,%s[v0]" % (
            deinterlace, _scale_spec(scale_filter, target_h),
        )
    else:
        split_outputs = "".join("[s%d]" % i for i in range(n_scaled))
        chains = ["[0:v]%s,split=%d%s" % (deinterlace, n_scaled, split_outputs)]
        for i, (_, target_h) in enumerate(ladder):
            chains.append("[s%d]%s[v%d]" % (i, _scale_spec(scale_filter, target_h), i))
        filter_complex = ";".join(chains)

    video_args = ["-filter_complex", filter_complex]
    for i, (bps, _) in enumerate(ladder):
        video_args += [
            "-map", "[v%d]" % i,
            "-c:v:%d" % i, encoder,
            "-b:v:%d" % i, str(bps),
        ]
    # Stream-copy variant: packet-copies the source so no GPU/CPU encoding needed.
    copy_idx = n_scaled
    video_args += ["-map", "0:v:0", "-c:v:%d" % copy_idx, "copy"]
    n_outputs = n_scaled + 1

    var_stream_map = ", ".join(
        "v:%d,a:%d" % (i, i) for i in range(n_outputs)
    ) + ", "

    return hwaccel_args, video_args, n_outputs, var_stream_map


def _hls_tail(channel, var_stream_map):
    """The shared ffmpeg HLS muxing arguments used by both pipelines."""
    return [
        "-f", "hls", "-sn",
        "-hls_flags", "delete_segments+independent_segments",
        "-hls_segment_filename",
        config["hls_local_path"] + "/" + channel.hls_uuid + "_%v_%02d.ts",
        "-hls_list_size", "10",
        "-hls_time", str(config["segment_len"]),
        "-hls_playlist_type", "event",
        "-master_pl_name", channel.hls_uuid + ".m3u8",
        "-var_stream_map", var_stream_map,
        channel.m3u8_file + "+%v",
    ]


def _drawtext_escape(path):
    """Escape a path for use as a drawtext filter option value.

    Inside the filter graph, ':' separates options and '\\' escapes; the
    textfile path we pass is uuid-derived but the font path may contain neither.
    """
    return path.replace("\\", "\\\\").replace(":", "\\:")


def build_audio_only_command(channel):
    """ffmpeg command for an audio-only service: synthesize a low-bandwidth black
    video carrying the channel name, muxed with the source audio into one HLS variant.
    """
    fps = config["dummy_video_fps"]
    gop = str(max(1, int(round(fps * config["segment_len"]))))
    name_file = config["hls_local_path"] + "/" + channel.hls_uuid + ".txt"
    drawtext = (
        "[0:v]drawtext="
        "fontfile=" + _drawtext_escape(config["dummy_video_font"])
        + ":textfile=" + _drawtext_escape(name_file)
        + ":expansion=none:fontcolor=white:fontsize=18"
        + ":x=(w-text_w)/2:y=(h-text_h)/2[v]"
    )
    return ["/usr/bin/ffmpeg",
        "-f", "lavfi", "-re",
        "-i", "color=c=black:s=%s:r=%s" % (config["dummy_video_size"], fps),
        "-i", channel.tvh_url,
        "-filter_complex", drawtext,
        "-map", "[v]",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
        "-b:v", str(config["dummy_video_bitrate"]), "-pix_fmt", "yuv420p",
        "-r", str(fps), "-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
        "-map", "1:a:0", "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-shortest",
    ] + _hls_tail(channel, "v:0,a:0, ")


class TVChannel:
    def __init__(self, name, tags, number, tvh_uuid, hls_uuid, *,
                 country=None, provider=None, audio_only=False):
        self.name = name
        self.tags = tags
        self.number = number
        self.tvh_uuid = tvh_uuid
        self.hls_uuid = hls_uuid
        self.country = country
        self.flag = flag_emoji(country)
        self.provider = provider
        # Audio-only (radio etc.): start_stream synthesizes a dummy video track.
        self.audio_only = audio_only
        # Set once the channel is seen to have EPG (at startup or later). Flagged
        # channels get fresh EPG fetched when they run out of events; channels
        # that never had EPG are left alone so we don't poll TVHeadend for nothing.
        self.had_epg = False
        self.tvh_url = tvh_base_url_auth + "stream/channel/" + tvh_uuid
        self.m3u8_file = config["hls_local_path"] + "/" + self.hls_uuid + ".m3u8"
        self.stream = None
        self.last_used = time.time()
        self.clean_stream()

    def start_stream(self):
        self.last_used = time.time()
        if self.stream:
            if os.path.isfile(self.m3u8_file):
                return "stream.m3u8?uuid=" + self.hls_uuid
            else:
                if self.stream.poll() is None:
                    return False
                # ffmpeg exited before producing a playlist; clean up before respawn
                self.clean_stream()

        if self.audio_only:
            # drawtext reads the channel name from this file (textfile=).
            with open(config["hls_local_path"] + "/" + self.hls_uuid + ".txt", "w") as f:
                f.write(self.name)
            cmd = build_audio_only_command(self)
        else:
            hwaccel_args, video_args, n_outputs, var_stream_map = build_codecs()
            acodec_params = ["-map", "a:0"] * n_outputs
            cmd = ["/usr/bin/ffmpeg"] + hwaccel_args + [
                "-i", self.tvh_url,
                "-preset", "veryfast",
                "-sc_threshold", "0",
                # Force a keyframe every 25 frames (~1 s at 25 fps) so the HLS muxer
                # can split segments quickly. Without this, libx264's default GOP of
                # 250 frames forced 10 s segments and slow startup.
                "-g", "25",
            ] + video_args + acodec_params + [
                "-c:a", "aac", "-b:a", "96k", "-ac", "2",
                "-r", "25",
            ] + _hls_tail(self, var_stream_map)

        self.stream = subprocess.Popen(cmd)
        self.last_used = time.time()
        return False

    def clean_stream(self):
        base = config["hls_local_path"]
        for f in os.listdir(base):
            if _owns_file(self.hls_uuid, f):
                try:
                    os.remove(base + "/" + f)
                except OSError:
                    pass
        self.stream = None


ORPHAN_SWEEP_INTERVAL = 60   # seconds between directory-wide orphan sweeps
ORPHAN_GRACE_SECONDS = 15    # skip files younger than this (mid-start safety margin)


_OWNED_SUFFIX_RE = re.compile(r"\A(\.m3u8(\+\d+)?|\.txt|_\d+_\d+\.ts)\Z")


def _owns_file(hls_uuid, filename):
    """True if `filename` is one of the files start_stream() generates for `hls_uuid`.

    A bare startswith() would also match a *different* channel whose uuid
    happens to be a string prefix of this one (e.g. "PROSIEBEN" is a prefix of
    "PROSIEBEN_MAXX", and "ARD" of "ARD-alpha" -> "ARDALPHA") — check the
    remainder against the exact suffixes our own filenames use instead.
    """
    if not filename.startswith(hls_uuid):
        return False
    return bool(_OWNED_SUFFIX_RE.match(filename[len(hls_uuid):]))


def _sweep_orphan_files(channel_list):
    """Delete any file in hls_local_path not owned by a currently-running channel.

    Backstop for files the per-channel cleanup didn't catch: a renamed/removed
    channel's stale hls_uuid files, or any other gap.
    """
    base = config["hls_local_path"]
    now = time.time()
    try:
        entries = os.listdir(base)
    except OSError:
        return
    for f in entries:
        owned = any(ch.stream is not None and _owns_file(ch.hls_uuid, f) for ch in channel_list)
        if owned:
            continue
        path = base + "/" + f
        try:
            if now - os.path.getmtime(path) < ORPHAN_GRACE_SECONDS:
                continue
            os.remove(path)
        except OSError:
            pass


def check_status(channel_list, epg, main_thread):
    """Background thread: kill ffmpeg processes idle >30s and keep EPG fresh."""
    tick = 0
    while main_thread.is_alive():
        time.sleep(1)
        tick += 1
        try:
            for channel in channel_list:
                if channel.stream is None:
                    continue
                if time.time() - channel.last_used > 30:
                    channel.stream.kill()
                    time.sleep(1)
                if channel.stream.poll() is None:
                    continue
                channel.clean_stream()
            for channel in channel_list:
                feed = epg.get(channel.tvh_uuid)
                if feed is None:
                    continue
                if feed.has_events():
                    # Flag any channel we've ever seen carry EPG.
                    channel.had_epg = True
                if channel.had_epg:
                    # Keep it fresh; refetches (throttled) once it runs dry.
                    feed.update()
            if tick % ORPHAN_SWEEP_INTERVAL == 0:
                _sweep_orphan_files(channel_list)
        except Exception:
            traceback.print_exc()
