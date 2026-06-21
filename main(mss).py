import time
import math
import signal
import threading
from dataclasses import dataclass

import numpy as np
import mss
import cv2
import keyboard
from openrgb import OpenRGBClient
from openrgb.utils import RGBColor

# This flag captures layered windows (like games in borderless mode) but it
# causes a mouse flicker problem, also it can cause performance issues, so I
# disable it by default, but you can enable it if you want to capture those
# types of windows (just be aware of the potential performance hit)
try:
    import mss.windows
    mss.windows.CAPTUREBLT = 0
except Exception:
    pass  # not on Windows, no flag to fiddle with — carry on


# --------------------------------------------------------------------------
# Tunable constants — everything I used to bury inside main() so I wouldn't
# have to hunt for them. Hoisted here so future-me can tweak without reading
# the whole loop.
# --------------------------------------------------------------------------
OPENRGB_HOST        = "127.0.0.1"
OPENRGB_PORT        = 6742
OPENRGB_DEVICE_IDX  = 0

HOTKEY              = "ctrl+alt+l"   # experimental, may occasionally need two presses — but shouldn't anymore :)

REGION_FRACTION     = 0.6      # central 60% of the screen
SMALL_W, SMALL_H    = 80, 45   # 80x45 is a good balance between performance and color accuracy, but you can experiment with this
CAPTURE_FPS         = 15       # heavy work runs at this rate (this took me so long to figure out, I hope it helps in optimizing performance)
SMOOTH_FPS          = 60       # light smoothing / push runs at this rate (ultra-smooth transitions :D)
SMOOTH_ALPHA_H      = 0.15     # hue smoothing (circular, finally school math use :D)
SMOOTH_ALPHA_SV     = 0.20     # smoothing factor for brightness and saturation

DARK_V              = 40       # pixels with V < this are "dark"
NEUTRAL_S           = 35       # pixels with S < this are "neutral"
NEUTRAL_V           = 170
NEUTRAL_RATIO       = 0.35     # >35% neutral pixels -> smoothly transition to white
COLOR_S             = 60
COLOR_V             = 80
BRIGHT_V            = 100
SAT_S               = 50

# Only push to OpenRGB when the smoothed color moved by >= this (per channel).
# Static screen = ~5-10 SDK calls/sec instead of 60. The SDK pipe is not free.
COLOR_DELTA_THRESH  = 3

# Heartbeat animation when turning OFF (the cute one)
HEARTBEAT_STEPS     = 40
HEARTBEAT_DELAY     = 0.02     # 20ms delay for smooth animation


# --------------------------------------------------------------------------
# State — kept small and explicit. No closures-over-mutables footguns this
# time. I learned my lesson.
# --------------------------------------------------------------------------
@dataclass
class State:
    enabled: bool = True
    pending_off_animation: bool = False
    last_sent: tuple = (0, 0, 0)
    # Smoothing state — start with sensible defaults to avoid abrupt changes on first run
    prev_h: float = 0.0     # default hue (red)
    prev_s: float = 160.0   # moderate saturation to avoid dull colors
    prev_v: float = 0.0     # default brightness
    # Targets
    target_h: int = 0
    target_s: float = 160.0
    target_v: float = 0.0


def connect_openrgb() -> "OpenRGBClient":
    """Block until OpenRGB is reachable; return a connected client."""
    while True:
        try:
            client = OpenRGBClient(OPENRGB_HOST, OPENRGB_PORT)
            _ = client.devices[OPENRGB_DEVICE_IDX]  # touch to validate
            print("Connected to OpenRGB successfully!")
            return client
        except Exception as e:
            print(f"Error connecting to OpenRGB: {e}")
            print("Retrying in 5 seconds...")
            time.sleep(5)


def circular_smooth_hue(old: float, new: float, alpha: float) -> float:
    """Shortest-path interpolation on the 0-179 hue circle (finally school math use :D)."""
    diff = ((new - old + 90) % 180 - 90)  # shortest path on a circle
    return (old + alpha * diff) % 180


def compute_target_color(hsv: np.ndarray, hist_buf: np.ndarray) -> tuple:
    """
    Given an HSV frame, return (target_h, target_s, target_v).

    `hist_buf` is a pre-allocated 180-element array reused across calls —
    no per-frame `np.bincount(minlength=180)` allocation spam.
    """
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    total = h.size

    dark_mask    = v < DARK_V
    neutral_mask = (s < NEUTRAL_S) & (v > NEUTRAL_V) & (~dark_mask)
    neutral_ratio = np.count_nonzero(neutral_mask) / total

    if neutral_ratio > NEUTRAL_RATIO:
        # Smoothly transition to white.
        # Hue doesn't matter for white, but we keep it stable.
        return 0, 0.0, 255.0

    # Dominant hue (only on colorful, non-dark pixels)
    color_mask = (s > COLOR_S) & (v > COLOR_V) & (~dark_mask)
    target_h = None
    if np.any(color_mask):
        # Reuse the buffer instead of np.bincount's per-call allocation.
        hist_buf.fill(0)
        np.add.at(hist_buf, h[color_mask], 1)
        target_h = int(np.argmax(hist_buf))

    # Brightness calc
    bright_pixels = v[v > BRIGHT_V]
    if bright_pixels.size:
        target_v = float(np.percentile(bright_pixels, 80))
    else:
        target_v = float(np.mean(v))

    # Saturation calc
    sat_pixels = s[s > SAT_S]
    if sat_pixels.size:
        target_s = float(np.percentile(sat_pixels, 75))
    else:
        target_s = 160.0

    # If no colorful pixels were found, keep target_h None — caller keeps the
    # previous smoothed hue. (The original code had the same behavior, just
    # less explicitly.)
    return target_h, target_s, target_v


def run_heartbeat(device) -> None:
    """Little heartbeat animation (runs once when turning OFF). Played from
    the main loop, NOT the hotkey callback — playing it from the callback was
    what made the hotkey need two presses. The keyboard thread would block on
    the 800ms of sleeps and miss the next press. Whoops."""
    for i in range(HEARTBEAT_STEPS):
        t = i / HEARTBEAT_STEPS
        beat = 0.5 + 0.5 * math.sin(2 * math.pi * t)
        c = int(255 * beat)
        try:
            device.set_color(RGBColor(c, c, c))
        except Exception:
            return  # OpenRGB died mid-pulse, don't crash, just bail
        time.sleep(HEARTBEAT_DELAY)  # 20ms delay for smooth animation


def main() -> None:
    """Keep everything in main to avoid global variables (slow) and make it
    easier to read, also makes it easier to add a GUI later if I want
    (which I probably won't but who knows)."""

    client = connect_openrgb()
    device = client.devices[OPENRGB_DEVICE_IDX]

    # Graceful shutdown — Ctrl+C used to dump an ugly traceback and leave the
    # keyboard on whatever color it last had. Not anymore.
    stop_flag = threading.Event()
    def _shutdown(*_):
        stop_flag.set()
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    sct = mss.mss()
    monitor = sct.monitors[1]

    # ----- capture central region (60%) -----
    cx = monitor["width"]  // 2
    cy = monitor["height"] // 2
    w  = int(monitor["width"]  * REGION_FRACTION)
    h  = int(monitor["height"] * REGION_FRACTION)
    region = {"left": cx - w // 2, "top": cy - h // 2, "width": w, "height": h}

    state = State()

    # Pre-allocated buffers — no more `if small is None: small = ...` branch
    # taken exactly once and then dead forever, and no more `del small` +
    # `gc.collect()` (which was a full-heap stop-the-world for zero benefit
    # since the buffer is reused anyway).
    small     = np.empty((SMALL_H, SMALL_W, 3), dtype=np.uint8)
    hsv_buf   = np.empty_like(small)
    hsv_pixel = np.zeros((1, 1, 3), dtype=np.uint8)  # pre-allocate a pixel for HSV to RGB conversion (performance boost by avoiding repeated allocations)
    hist_buf  = np.zeros(180, dtype=np.intp)

    # --- Toggle hook: only flip flags, do NOT do any heavy work here ---
    # The original toggle() did the whole 800ms heartbeat inline, which is why
    # the old problem was "the shortcut may occasionally require pressing twice".
    # It wasn't the shortcut, it was the thread being busy sleeping.
    def toggle():
        if state.enabled:
            # Turning OFF: queue the heartbeat for the main loop to play.
            state.enabled = False
            state.pending_off_animation = True
            print("Lighting: OFF")
        else:
            state.enabled = True
            print("Lighting: ON")

    keyboard.add_hotkey(HOTKEY, toggle)

    # --- Main loop: 15 Hz capture, 60 Hz smoothing, on-demand push ---
    # Two independent clocks. The original coupled them inside one `while True`
    # with a `capture_counter % CAPTURE_EVERY == 0` gate, which meant dropping
    # the capture rate also dropped the smoothing rate and doubled transition
    # time. Decoupled here so transition speed matches the original 60 Hz feel
    # while heavy capture stays throttled to 15 Hz.
    capture_interval = 1.0 / CAPTURE_FPS         # heavy work
    smooth_interval  = 1.0 / SMOOTH_FPS          # light work, target 60 FPS for ultra-smooth transitions :D
    next_capture = time.perf_counter()
    next_smooth  = next_capture

    print("Running. Press Ctrl+C to quit.")

    while not stop_flag.is_set():
        now = time.perf_counter()

        # If disabled: maybe play the OFF animation, then idle cheaply.
        if not state.enabled:
            if state.pending_off_animation:
                state.pending_off_animation = False
                run_heartbeat(device)
                # Force a re-send next time we re-enable.
                state.last_sent = (-100, -100, -100)
            stop_flag.wait(timeout=0.2)
            # Resync both clocks after the idle so we don't burst-fire on wake.
            now = time.perf_counter()
            next_capture = now
            next_smooth  = now
            continue

        # ---- 1. HEAVY LIFTING: capture + target computation (15 Hz) ----
        if now >= next_capture:
            next_capture += capture_interval
            # If we fell behind by more than one interval, skip the catch-up
            # bursts (prevents a "machine gun" of captures after a stall).
            if next_capture < now:
                next_capture = now + capture_interval

            try:
                raw = sct.grab(region)
            except Exception as e:
                print(f"Capture failed: {e}")
                time.sleep(0.1)
                next_capture = time.perf_counter() + capture_interval
                continue

            # Convert raw bytes to numpy array (much faster than PIL).
            # NOTE: mss.raw.rgb is misnamed — on Windows it actually delivers
            # RGB-ordered bytes. We feed them into cv2.COLOR_BGR2HSV below,
            # which interprets them as BGR. That invisible R<->B swap upstream
            # is cancelled by the `b, g, r = ...` unpacking downstream. See
            # the "don't fix it" note next to the HSV->RGB conversion.
            frame = np.frombuffer(raw.rgb, dtype=np.uint8)
            # Reshape to HWC format for OpenCV (much faster than PIL)
            frame = frame.reshape(raw.height, raw.width, 3)

            # Resize to smaller frame for faster processing (this is a huge
            # performance boost, I found 80x45 to be a good balance between
            # performance and color accuracy, but you can experiment with this).
            # Default INTER_LINEAR, same as the original.
            cv2.resize(frame, (SMALL_W, SMALL_H), dst=small)

            cv2.cvtColor(small, cv2.COLOR_BGR2HSV, dst=hsv_buf)

            target_h, target_s, target_v = compute_target_color(hsv_buf, hist_buf)
            if target_h is not None:
                state.target_h = target_h
            state.target_s = target_s
            state.target_v = target_v

        # ---- 2. LIGHT LIFTING: smoothing + push (60 Hz) ----
        # This ensures the smoothing happens at a high frequency for
        # ultra-smooth transitions, while the heavy capture + processing
        # happens less frequently to save CPU.
        if now >= next_smooth:
            next_smooth += smooth_interval
            if next_smooth < now:
                next_smooth = now + smooth_interval

            # Smoothly step the previous values towards the targets
            state.prev_h = circular_smooth_hue(state.prev_h, state.target_h, SMOOTH_ALPHA_H)
            state.prev_v += SMOOTH_ALPHA_SV * (state.target_v - state.prev_v)
            state.prev_s += SMOOTH_ALPHA_SV * (state.target_s - state.prev_s)

            # Convert smoothed HSV back to BGR for the hardware.
            #
            # WARNING — READ THIS BEFORE "FIXING" THE COLOR SWAP:
            #   cv2.cvtColor(HSV2RGB) returns (R, G, B), but we unpack as
            #   (b, g, r) and then pass RGBColor(r, g, b). That double-swap
            #   cancels out the upstream R<->B swap introduced by feeding
            #   mss's RGB bytes into cv2.COLOR_BGR2HSV above. Removing this
            #   swap (e.g. unpacking as `r, g, b = ...`) makes red screens
            #   turn the keyboard blue. I learned this the hard way.
            #   Don't "fix" it.
            hsv_pixel[0, 0, 0] = int(state.prev_h)
            hsv_pixel[0, 0, 1] = int(state.prev_s)
            hsv_pixel[0, 0, 2] = int(state.prev_v)
            b, g, r = (int(c) for c in cv2.cvtColor(hsv_pixel, cv2.COLOR_HSV2RGB)[0, 0])

            # Only push to OpenRGB if the color moved enough — saves SDK traffic.
            # The original pushed unconditionally 60x/sec; this drops to ~5-10/sec
            # on a static screen with zero perceptual difference.
            if (abs(r - state.last_sent[0]) >= COLOR_DELTA_THRESH or
                abs(g - state.last_sent[1]) >= COLOR_DELTA_THRESH or
                abs(b - state.last_sent[2]) >= COLOR_DELTA_THRESH):
                try:
                    device.set_color(RGBColor(r, g, b))
                    state.last_sent = (r, g, b)
                except Exception as e:
                    print(f"OpenRGB set_color failed: {e}")
                    # Try to reconnect lazily on next iteration — one crash
                    # no longer kills the whole loop.
                    try:
                        client = connect_openrgb()
                        device = client.devices[OPENRGB_DEVICE_IDX]
                    except Exception:
                        time.sleep(1.0)

        # Sleep until the nearest of the two clocks, but never block longer
        # than one smooth interval (so SIGINT stays responsive).
        next_event = min(next_smooth, next_capture)
        sleep_for = next_event - time.perf_counter()
        if sleep_for > 0:
            time.sleep(min(sleep_for, smooth_interval))

    # Clean shutdown — turn the LEDs off so the keyboard doesn't stay glowing
    # whatever color it last had.
    try:
        device.set_color(RGBColor(0, 0, 0))
    except Exception:
        pass
    print("\nBye.")


if __name__ == "__main__":  # Classic Python entry point :D
    main()
