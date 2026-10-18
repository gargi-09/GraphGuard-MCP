"""Watches corridor B <-> C and reports obstructions to the FoGuard graph.

    python -m vision.trigger_node --mock         # simulated obstruction, no camera/GPU
    python -m vision.trigger_node --visual-demo  # presentation mode: simulated feed from demo_assets/
    python -m vision.trigger_node --webcam       # live camera through the segmentor

The corridor counts as obstructed when either signal stays high for a few
inferences: people covering the corridor (DepthAR segmentor), or the scene
differing from a captured picture of the empty corridor (boxes, carts, ...).
Depth is not used: Depth Anything's depth is rescaled on every frame, so it
can't tell "something is close" from "the scene changed", and in testing the
occlusion mask's coverage fell when the corridor was blocked.

On an obstruction the corridor is marked blocked in Kuzu, a JSON incident is
printed, and a prompt is printed for Claude Web to start recovery through Fo.
"""
import argparse
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from graph.init_db import close_db, init_db, mark_edge_blocked
from graph.verifier import RouteVerifier

MONITORED_CORRIDOR = ("B", "C")
ORIGIN = "A"
DESTINATION = "F"

# --- webcam detection tuning ---
INFER_EVERY = 5           # run the segmentor on every Nth frame (CPU is slow)
TRIGGER_STREAK = 3        # consecutive obstructed inferences before an incident
PERSON_THRESHOLD = 0.35   # share of the ROI covered by people
CHANGE_THRESHOLD = 0.08   # share of the ROI differing from the empty corridor
# Webcam test, 2026-10-17: a clean empty corridor read change <= 0.023, a
# small object 0.11-0.12, a person stepping in person 0.43 / change 0.40.
CHANGE_PIXEL_DELTA = 30   # grey-level difference that counts as a changed pixel
CALIBRATION_DELAY_S = 5.0  # time to step out of view before the background capture
CALIBRATION_FRAMES = 15   # frames median-combined into the empty-corridor background
CALIBRATION_MAX_PERSON = 0.02  # person share of the ROI allowed during that capture

# Region of the frame that shows the corridor floor, as (top, bottom, left,
# right) fractions. Only pixels in here count towards an obstruction.
ROI = (0.35, 1.0, 0.2, 0.8)

# --- presentation mode (--visual-demo) ---
DEMO_ASSETS = Path(__file__).resolve().parent.parent / "demo_assets"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def _demo_image(stem: str) -> Path:
    """demo_assets/<stem> with whichever image extension is present (default .jpg)."""
    for ext in IMAGE_EXTENSIONS:
        if (DEMO_ASSETS / f"{stem}{ext}").is_file():
            return DEMO_ASSETS / f"{stem}{ext}"
    return DEMO_ASSETS / f"{stem}.jpg"


BLOCKED_CORRIDOR_IMAGE = _demo_image("img_1")
OPEN_CORRIDOR_IMAGE = _demo_image("img_2")
FEED_WINDOW = "Edge Vision Node - Live Feed"
BASELINE_HOLD_S = 3.0   # open corridor on screen: the baseline state
BLOCKED_HOLD_S = 2.0    # blocked corridor on screen before the incident fires
MAX_FEED_WIDTH = 1280   # large photos are scaled down to fit a projector screen


def report_obstruction(print_card: bool = True) -> dict:
    """Block the corridor in Kuzu, then emit the incident JSON and Fo prompt.

    print_card=False skips the copy-paste prompt, for callers that hand the
    incident straight to the recovery agent instead.
    """
    from_id, to_id = MONITORED_CORRIDOR

    # Open the DB only for the update: Kuzu allows one read-write process per
    # file, so holding it while the camera runs would lock out the MCP server.
    init_db()
    try:
        if not mark_edge_blocked(from_id, to_id, True):
            raise RuntimeError(f"No corridor {from_id} <-> {to_id} in the graph.")
        reroute = RouteVerifier().find_shortest_valid_route(ORIGIN, DESTINATION)
    finally:
        close_db()

    incident = {
        "event": "OBSTRUCTION_DETECTED",
        "blocked_corridor": [from_id, to_id],
        "origin": ORIGIN,
        "destination": DESTINATION,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    print(json.dumps(incident), flush=True)
    if print_card:
        print_recovery_card(incident, reroute)
    return incident


def build_recovery_prompt(incident: dict, reroute: dict) -> str:
    """The copy-paste prompt that hands an incident to Claude Web and Fo."""
    from_id, to_id = incident["blocked_corridor"]
    if reroute["valid"]:
        reroute_line = (f"The FoGuard route verifier's shortest passable route is "
                        f"{'-'.join(reroute['path'])} ({reroute['total_distance']:g} m).")
    else:
        reroute_line = f"The FoGuard route verifier found no passable route: {reroute['reason']}"

    prompt = (
        f"FoGuard incident: an obstruction was detected on corridor {from_id} <-> {to_id} "
        f"at {incident['timestamp']}, and the facility graph now marks it impassable "
        f"in both directions. A delivery is in progress from {incident['origin']} to "
        f"{incident['destination']}. {reroute_line}\n\n"
        f"First confirm the reroute with the FoGuard tools: call get_workspace_topology, "
        f"then propose_recovery_route from {incident['origin']} to "
        f"{incident['destination']}, and retry until it is approved.\n\n"
        f"Then use Fo to start recovery: notify the facilities team that corridor "
        f"{from_id} <-> {to_id} is obstructed and needs to be cleared, and let the "
        f"recipient at {incident['destination']} know the delivery is being rerouted "
        f"and may be delayed. Ask me before sending if you need contact details.\n\n"
        f"Incident record: {json.dumps(incident)}"
    )
    return prompt


def print_recovery_card(incident: dict, reroute: dict):
    from_id, to_id = incident["blocked_corridor"]
    prompt = build_recovery_prompt(incident, reroute)

    # Plain ASCII so the card renders in any Windows console code page, and no
    # border on the prompt lines so they copy cleanly.
    rule = "=" * 72
    print(f"\n{rule}")
    print(f"  FOGUARD ALERT  |  corridor {from_id} <-> {to_id} BLOCKED  |  graph updated")
    print(rule)
    print("  Copy everything between the dashed lines into Claude Web (Fo MCP):")
    print("-" * 72)
    print(prompt)
    print("-" * 72)
    print(f"{rule}\n", flush=True)


def run_mock(print_card: bool = True) -> dict:
    print(f"[trigger] Mock mode: simulating an obstruction on corridor "
          f"{MONITORED_CORRIDOR[0]} <-> {MONITORED_CORRIDOR[1]}")
    return report_obstruction(print_card)


def missing_demo_images() -> list[Path]:
    return [p for p in (OPEN_CORRIDOR_IMAGE, BLOCKED_CORRIDOR_IMAGE) if not p.is_file()]


def _load_feed_frame(path: Path) -> np.ndarray:
    import cv2

    frame = cv2.imread(str(path))
    if frame is None:
        raise FileNotFoundError(f"Can't read demo image {path}. See demo_assets/README.md.")
    h, w = frame.shape[:2]
    if w > MAX_FEED_WIDTH:
        frame = cv2.resize(frame, (MAX_FEED_WIDTH, round(h * MAX_FEED_WIDTH / w)),
                           interpolation=cv2.INTER_AREA)
    return frame


def _label_feed(frame: np.ndarray, status: str, color: tuple) -> np.ndarray:
    """Camera-style banner so the audience can read the node's state."""
    import cv2

    labeled = frame.copy()
    w = labeled.shape[1]
    cv2.rectangle(labeled, (0, 0), (w, 44), (0, 0, 0), -1)
    corridor = f"{MONITORED_CORRIDOR[0]}<->{MONITORED_CORRIDOR[1]}"
    cv2.putText(labeled, f"EDGE NODE  CAM {corridor}  |  {status}", (12, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2, cv2.LINE_AA)
    cv2.putText(labeled, "simulated feed", (w - 150, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1, cv2.LINE_AA)
    return labeled


def _hold(seconds: float):
    """Keep the window on screen and responsive: waitKey pumps GUI events."""
    import cv2

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cv2.waitKey(20)


def run_visual_demo(print_card: bool = True, open_path: Path = OPEN_CORRIDOR_IMAGE,
                    blocked_path: Path = BLOCKED_CORRIDOR_IMAGE) -> dict:
    """Presentation mode: show the open corridor, then the blocked one, then report.

    The feed is simulated from two still images; nothing is analyzed. After
    the window closes this does exactly what --mock does.
    """
    import cv2

    # Load both first, so a missing file fails before any window opens.
    open_frame = _label_feed(_load_feed_frame(open_path), "corridor clear (baseline)", (80, 220, 80))
    blocked_frame = _label_feed(_load_feed_frame(blocked_path), "OBSTRUCTION DETECTED", (60, 60, 255))

    print(f"[trigger] Visual demo: live feed of corridor "
          f"{MONITORED_CORRIDOR[0]} <-> {MONITORED_CORRIDOR[1]}, baseline", flush=True)
    cv2.namedWindow(FEED_WINDOW, cv2.WINDOW_AUTOSIZE)
    try:
        cv2.imshow(FEED_WINDOW, open_frame)
        _hold(BASELINE_HOLD_S)
        print("[trigger] Obstruction appears in the feed", flush=True)
        cv2.imshow(FEED_WINDOW, blocked_frame)
        _hold(BLOCKED_HOLD_S)
    finally:
        cv2.destroyAllWindows()
        cv2.waitKey(1)  # on Windows the window only closes once events are pumped
    return report_obstruction(print_card)


def roi_slice(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    top, bottom, left, right = ROI
    return image[int(top * h):int(bottom * h), int(left * w):int(right * w)]


def to_gray(frame: np.ndarray) -> np.ndarray:
    import cv2

    # Blurred so sensor noise and small camera shake don't register as change.
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return cv2.GaussianBlur(gray, (21, 21), 0)


def scene_change(gray_roi: np.ndarray, background_roi: np.ndarray) -> float:
    """Share of the ROI that differs from the empty-corridor background.

    Each image is mean-centred first, so a global brightness shift (webcam
    auto-exposure, lights dimming) doesn't count as an object appearing.
    """
    diff = np.abs((gray_roi - gray_roi.mean()) - (background_roi - background_roi.mean()))
    return float(np.mean(diff > CHANGE_PIXEL_DELTA))


def run_webcam(camera: int, person_threshold: float = PERSON_THRESHOLD,
               change_threshold: float = CHANGE_THRESHOLD, verbose: bool = False):
    import cv2

    # Imported here so --mock never pays for loading torch/transformers.
    from core.segmentor import Segmentor

    segmentor = Segmentor()

    cap = cv2.VideoCapture(camera)
    if not cap.isOpened():
        print(f"[trigger] Error: could not open webcam {camera}.")
        return

    # Detection is armed only once a picture of the empty corridor exists.
    # It is captured after a countdown (or C), and only while the segmentor
    # sees nobody in the ROI: a person caught in the background would make
    # every later empty frame look "changed".
    calibrate_at = time.monotonic() + CALIBRATION_DELAY_S
    calibration_frames = []
    background = None
    recent = deque(maxlen=TRIGGER_STREAK)
    person, change = 1.0, 0.0  # assume occupied until the first inference
    waiting_logged = False
    reported = False
    frame_idx = 0

    print(f"[trigger] Step out of view: capturing the empty corridor in "
          f"{CALIBRATION_DELAY_S:.0f}s. C = recapture, Q = quit.", flush=True)

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("[trigger] Error: could not read from webcam.")
                break

            if background is None and time.monotonic() >= calibrate_at:
                if person > CALIBRATION_MAX_PERSON:
                    # Restart so no frame with a person in it reaches the median.
                    calibration_frames = []
                    if not waiting_logged:
                        print("[trigger] Someone is in the corridor; waiting for it "
                              "to clear before capturing.", flush=True)
                        waiting_logged = True
                else:
                    calibration_frames.append(roi_slice(to_gray(frame)))
                    if len(calibration_frames) == CALIBRATION_FRAMES:
                        # Median drops anything that flickered through mid-capture.
                        background = np.median(calibration_frames, axis=0)
                        calibration_frames = []
                        recent.clear()
                        print("[trigger] Empty corridor captured; detection armed.",
                              flush=True)

            if frame_idx % INFER_EVERY == 0:
                person = float(np.mean(roi_slice(segmentor.predict(frame)) > 127))
                armed = background is not None
                change = scene_change(roi_slice(to_gray(frame)), background) if armed else 0.0
                obstructed = armed and (person > person_threshold or change > change_threshold)
                recent.append(obstructed)
                if verbose:
                    print(f"[trigger] t={time.monotonic():.1f} person={person:.3f} "
                          f"change={change:.3f}" + ("  OBSTRUCTED" if obstructed else "")
                          + ("" if armed else "  (not armed)"), flush=True)
                if not reported and len(recent) == TRIGGER_STREAK and all(recent):
                    report_obstruction()
                    reported = True  # the block stays until recovery clears it

            draw_overlay(frame, person, change, background is not None, reported)
            cv2.imshow("FoGuard trigger node", frame)
            frame_idx += 1

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("c"):
                background, calibration_frames = None, []
                waiting_logged = False
                calibrate_at = time.monotonic() + CALIBRATION_DELAY_S
                print(f"[trigger] Recapturing the empty corridor in "
                      f"{CALIBRATION_DELAY_S:.0f}s.", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        cv2.destroyAllWindows()


def draw_overlay(frame: np.ndarray, person: float, change: float,
                 calibrated: bool, reported: bool):
    import cv2

    h, w = frame.shape[:2]
    top, bottom, left, right = ROI
    if reported:
        status, color = "BLOCKED - incident sent", (0, 0, 255)
    elif not calibrated:
        status, color = "not armed: clear the corridor to calibrate", (0, 200, 255)
    else:
        status, color = "monitoring", (0, 200, 0)

    cv2.rectangle(frame, (int(left * w), int(top * h)),
                  (int(right * w) - 1, int(bottom * h) - 1), color, 2)
    cv2.putText(frame, f"corridor {MONITORED_CORRIDOR[0]}<->{MONITORED_CORRIDOR[1]}: {status}",
                (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
    cv2.putText(frame, f"person={person:.2f}  change={change:.2f}",
                (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--webcam", action="store_true",
                      help="live capture through the segmentation pipeline")
    mode.add_argument("--mock", action="store_true",
                      help="simulate an obstruction without a camera or GPU")
    mode.add_argument("--visual-demo", action="store_true",
                      help="presentation mode: show the open, then the blocked corridor "
                           "from demo_assets/, then report the obstruction")
    parser.add_argument("--camera", type=int, default=0,
                        help="OpenCV camera index for --webcam (default 0)")
    parser.add_argument("--person-threshold", type=float, default=PERSON_THRESHOLD,
                        help=f"share of the ROI covered by people (default {PERSON_THRESHOLD})")
    parser.add_argument("--change-threshold", type=float, default=CHANGE_THRESHOLD,
                        help="share of the ROI differing from the empty corridor "
                             f"(default {CHANGE_THRESHOLD})")
    parser.add_argument("--verbose", action="store_true",
                        help="print both signals for every inference")
    args = parser.parse_args()

    if args.mock:
        run_mock()
    elif args.visual_demo:
        missing = missing_demo_images()
        if missing:
            parser.exit(1, "Missing demo images (see demo_assets/README.md):\n"
                        + "\n".join(f"  {p}" for p in missing) + "\n")
        run_visual_demo()
    else:
        run_webcam(args.camera, args.person_threshold, args.change_threshold, args.verbose)


if __name__ == "__main__":
    main()
