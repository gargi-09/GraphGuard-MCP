import cv2
import numpy as np


class OcclusionReasoner:
    """
    Combines depth map + person mask into a final occlusion mask.

    The core idea:
        - Depth map alone is noisy at object boundaries
        - Person mask alone doesn't know about depth ordering
        - Together: use depth for the broad decision, person mask
          to correct failures at edges (the symbolic correction layer)

    Output mask (H x W, uint8):
        255 = foreground (real world pixel, should occlude virtual object)
        0   = background (virtual object can appear here)
    """

    def __init__(
        self,
        depth_threshold: float = 0.35,   # was 0.4
        boundary_kernel: int = 15,
        alpha: float = 0.8               # was 0.7
    ):
        """
        Args:
            depth_threshold : pixels with depth < this are foreground
                              (remember: 0 = closest, 1 = farthest)
            boundary_kernel : size of dilation kernel for boundary zone
                              detection — wider = more semantic correction
            alpha           : blend weight between depth and semantic mask
                              1.0 = pure depth, 0.0 = pure semantic
        """
        self.depth_threshold = depth_threshold
        self.boundary_kernel = boundary_kernel
        self.alpha = alpha

    def _get_depth_mask(self, depth: np.ndarray) -> np.ndarray:
        """
        Threshold depth map into a binary foreground mask.
        Pixels closer than depth_threshold → foreground (255).
        """
        depth_mask = (depth < self.depth_threshold).astype(np.uint8) * 255
        return depth_mask

    def _get_boundary_zone(self, person_mask: np.ndarray) -> np.ndarray:
        """
        Find the uncertain boundary region around the person silhouette.
        This is where depth estimation is least reliable —
        thin structures like hair and fingers cause the most errors.
        """
        kernel = np.ones(
            (self.boundary_kernel, self.boundary_kernel), np.uint8
        )
        dilated  = cv2.dilate(person_mask, kernel, iterations=1)
        eroded   = cv2.erode(person_mask, kernel, iterations=1)

        # boundary zone = dilated - eroded (ring around the silhouette)
        boundary = cv2.subtract(dilated, eroded)
        return boundary

    def _symbolic_correction(
        self,
        depth_mask: np.ndarray,
        person_mask: np.ndarray,
        boundary_zone: np.ndarray
    ) -> np.ndarray:
        """
        The symbolic correction layer — the core research contribution.

        Rule:
            Inside the boundary zone, if the segmentor says PERSON,
            override the depth decision and mark as foreground.
            Outside the boundary zone, trust the depth map.

        This corrects the most common depth failure mode: a person's
        hair or hand edge being classified as background because the
        depth model smears depth values across the boundary.
        """
        corrected = depth_mask.copy()

        # where we are in the boundary zone AND person mask says person
        # → force foreground regardless of what depth said
        correction_region = (boundary_zone == 255) & (person_mask == 255)
        corrected[correction_region] = 255

        # where we are in the boundary zone AND person mask says background
        # → force background (removes depth bleed-through around edges)
        anti_correction_region = (boundary_zone == 255) & (person_mask == 0)
        corrected[anti_correction_region] = 0

        return corrected

    def _smooth_mask(self, mask: np.ndarray) -> np.ndarray:
        kernel  = np.ones((7, 7), np.uint8)
        cleaned = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, kernel)

        # stronger blur for softer edges
        blurred = cv2.GaussianBlur(cleaned, (21, 21), 0)
        return blurred

    def predict(
        self,
        depth: np.ndarray,
        person_mask: np.ndarray
    ) -> np.ndarray:
        """
        Main entry point.

        Args:
            depth       : float32 H x W, values in [0, 1]
            person_mask : uint8 H x W, values in {0, 255}

        Returns:
            occlusion_mask : uint8 H x W
                255 = real world (occludes virtual object)
                0   = virtual object can render here
        """
        # Step 1 — neural depth decision
        depth_mask = self._get_depth_mask(depth)

        # Step 2 — find uncertain boundary zone around person
        boundary_zone = self._get_boundary_zone(person_mask)

        # Step 3 — symbolic correction at boundaries
        corrected = self._symbolic_correction(
            depth_mask, person_mask, boundary_zone
        )

        # Step 4 — blend depth mask and person mask outside boundary
        # weighted combination gives us robustness when person
        # is not the only foreground object in scene
        depth_float  = depth_mask.astype(np.float32) / 255.0
        person_float = person_mask.astype(np.float32) / 255.0
        blended      = (self.alpha * depth_float + (1 - self.alpha) * person_float)
        blended      = (blended * 255).clip(0, 255).astype(np.uint8)
        _, blended   = cv2.threshold(blended, 127, 255, cv2.THRESH_BINARY)

        # Step 5 — apply correction on top of blend
        # correction overrides blend in boundary zone
        final = blended.copy()
        final[boundary_zone == 255] = corrected[boundary_zone == 255]

        # Step 6 — smooth for clean compositing
        final = self._smooth_mask(final)

        return final