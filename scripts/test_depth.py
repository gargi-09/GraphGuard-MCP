import cv2
import numpy as np
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.depth_estimator import DepthEstimator
from core.segmentor import Segmentor
from core.occlusion_reasoner import OcclusionReasoner
from core.temporal_smoother import TemporalSmoother
from ar.renderer import ARRenderer
from ar.face_anchor import FaceAnchoredReticle
from ar.exosuit import ExosuitOverlay
from ar.exosuit import BodyMode


def draw_hud(frame, position, mode, smoothing_on, consistency_score):
    h, w = frame.shape[:2]
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, h - 90), (w, h), (0, 0, 0), -1)
    frame = cv2.addWeighted(overlay, 0.55, frame, 0.45, 0)

    controls   = "W/S: fwd/back  A/D: left/right  I/K: up/down  T: smoothing  TAB: debug  Q: quit"
    pos_text   = f"cube: x={position[0]:.1f}  y={position[1]:.1f}  z={position[2]:.1f}"
    smooth_text = f"temporal smoothing: {'ON' if smoothing_on else 'OFF'}  |  scene consistency: {1.0 - consistency_score:.2f}"

    cv2.putText(frame, controls,     (10, h - 65),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 180, 180), 1, cv2.LINE_AA)
    cv2.putText(frame, pos_text,     (10, h - 40),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (100, 255, 100), 1, cv2.LINE_AA)
    cv2.putText(frame, smooth_text,  (10, h - 15),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (100, 220, 255), 1, cv2.LINE_AA)

    mode_label = "DEBUG" if mode == "debug" else "AR"
    cv2.putText(frame, mode_label, (w - 90, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (100, 255, 255), 1, cv2.LINE_AA)

    color = (50, 220, 50) if smoothing_on else (50, 50, 220)
    label = "SMOOTH" if smoothing_on else "RAW"
    cv2.rectangle(frame, (w - 100, 45), (w - 10, 68), color, -1)
    cv2.putText(frame, label, (w - 93, 62),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)

    return frame


def main():
    depth_estimator    = DepthEstimator()
    segmentor          = Segmentor()
    occlusion_reasoner = OcclusionReasoner(
        depth_threshold=0.35,
        boundary_kernel=15,
        alpha=0.8
    )
    temporal_smoother  = TemporalSmoother(
        buffer_size=8,
        ema_alpha=0.35,
        consistency_thresh=0.08
    )

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Error: could not open webcam.")
        return

    ret, test_frame = cap.read()
    if not ret:
        print("Error: could not read from webcam.")
        return

    frame_h, frame_w = test_frame.shape[:2]
    renderer = ARRenderer(frame_w=frame_w, frame_h=frame_h)
    reticle  = FaceAnchoredReticle()
    exosuit = ExosuitOverlay()

    DEPTH_SKIP  = 3
    SEG_SKIP    = 5
    frame_idx   = 0
    DISPLAY_W   = 400
    DISPLAY_H   = 225

    # cube state
    position  = np.array([-8.0, 0.5, 8.0], dtype=np.float32)
    rotation  = np.array([0.3,  0.5, 0.1], dtype=np.float32)
    rot_speed = np.array([0.005, 0.01, 0.002], dtype=np.float32)
    scale     = 45.0
    MOVE_STEP = 0.3

    # modes
    display_mode = "ar"
    smoothing_on = True

    # state
    depth             = None
    raw_depth         = None
    mask              = None
    occlusion         = None
    consistency_score = 0.0

    depth_colored   = np.zeros((DISPLAY_H, DISPLAY_W, 3), dtype=np.uint8)
    seg_vis         = np.zeros((DISPLAY_H, DISPLAY_W, 3), dtype=np.uint8)
    occ_vis         = np.zeros((DISPLAY_H, DISPLAY_W, 3), dtype=np.uint8)
    consistency_vis = np.zeros((DISPLAY_H, DISPLAY_W, 3), dtype=np.uint8)

    cv2.namedWindow("DepthAR", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("DepthAR", 900, 560)

    print("DepthAR running — press T to toggle temporal smoothing, TAB for debug, Q to quit.")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        h, w   = frame.shape[:2]
        rotation = rotation + rot_speed

        # --- depth estimation ---
        if frame_idx % DEPTH_SKIP == 0:
            raw_depth = depth_estimator.predict(frame)

            smoothed_depth, consistency_map = temporal_smoother.update(raw_depth)
            consistency_score = float(np.mean(consistency_map))

            depth = smoothed_depth if smoothing_on else raw_depth

            depth_vis     = (depth * 255).astype(np.uint8)
            depth_colored = cv2.resize(
                cv2.applyColorMap(depth_vis, cv2.COLORMAP_INFERNO),
                (DISPLAY_W, DISPLAY_H)
            )

            consistency_vis = temporal_smoother.get_consistency_visualization(
                DISPLAY_W, DISPLAY_H
            )
            cv2.putText(consistency_vis, "instability map", (8, 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        # --- segmentation ---
        if frame_idx % SEG_SKIP == 0:
            mask = segmentor.predict(frame)
            seg_vis = cv2.resize(
                cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR),
                (DISPLAY_W, DISPLAY_H)
            )

        # --- occlusion reasoning ---
        if depth is not None and mask is not None:
            depth_resized = cv2.resize(depth, (w, h), interpolation=cv2.INTER_LINEAR)
            mask_resized  = cv2.resize(mask,  (w, h), interpolation=cv2.INTER_NEAREST)
            occlusion     = occlusion_reasoner.predict(depth_resized, mask_resized)
            occ_vis       = cv2.resize(
                cv2.applyColorMap(occlusion, cv2.COLORMAP_VIRIDIS),
                (DISPLAY_W, DISPLAY_H)
            )

        # --- AR compositing ---
        if occlusion is not None:
            ar_frame = renderer.composite(
                frame, occlusion,
                position=position,
                rotation=rotation,
                scale=scale
            )
        else:
            ar_frame = frame.copy()

        # --- face-anchored reticle — only in helmet/face mode ---
        depth_for_reticle = depth if depth is not None else np.full(
            (frame_h, frame_w), 0.5, dtype=np.float32
        )
        if exosuit.current_mode == BodyMode.FACE_ONLY:
            ar_frame = reticle.render(
                ar_frame,
                cv2.resize(depth_for_reticle, (frame_w, frame_h)),
                consistency_score,
                dt=0.05
            )

        # --- exosuit overlay ---
        mask_for_suit = mask if mask is not None else np.zeros(
            (frame_h, frame_w), dtype=np.uint8
        )
        depth_for_suit = depth if depth is not None else np.full(
            (frame_h, frame_w), 0.5, dtype=np.float32
        )
        ar_frame = exosuit.render(
            ar_frame,
            mask_for_suit,
            cv2.resize(depth_for_suit, (frame_w, frame_h)),
            consistency_score,
            dt=0.05
        )
        
        # --- display ---
        if display_mode == "ar":
            ar_frame = draw_hud(ar_frame, position, display_mode,
                                smoothing_on, consistency_score)
            cv2.imshow("DepthAR", ar_frame)

        else:
            frame_s = cv2.resize(frame,    (DISPLAY_W, DISPLAY_H))
            ar_s    = cv2.resize(ar_frame, (DISPLAY_W, DISPLAY_H))

            for img, label in zip(
                [frame_s, ar_s, consistency_vis, occ_vis],
                ["raw", "DepthAR", "depth instability", "occlusion mask"]
            ):
                cv2.putText(img, label, (8, 22),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

            top_row    = np.hstack([frame_s, ar_s])
            bottom_row = np.hstack([consistency_vis, occ_vis])
            combined   = np.vstack([top_row, bottom_row])
            combined   = draw_hud(combined, position, display_mode,
                                  smoothing_on, consistency_score)
            cv2.imshow("DepthAR", combined)

        frame_idx += 1

        # --- keyboard ---
        key = cv2.waitKey(1) & 0xFF
        if   key == ord("q"):
            break
        elif key == 9:
            display_mode = "debug" if display_mode == "ar" else "ar"
        elif key == ord("t"):
            smoothing_on = not smoothing_on
            if not smoothing_on:
                temporal_smoother.reset()
            print(f"[DepthAR] temporal smoothing: {'ON' if smoothing_on else 'OFF'}")
        elif key == ord("a"):
            position[0] -= MOVE_STEP
        elif key == ord("d"):
            position[0] += MOVE_STEP
        elif key == ord("w"):
            position[2] -= MOVE_STEP
        elif key == ord("s"):
            position[2] += MOVE_STEP
        elif key == ord("i"):
            position[1] -= MOVE_STEP
        elif key == ord("k"):
            position[1] += MOVE_STEP

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()