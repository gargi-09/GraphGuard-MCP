import torch
import numpy as np
from PIL import Image
from transformers import AutoImageProcessor, AutoModelForDepthEstimation
from dotenv import load_dotenv
import os

load_dotenv()

class DepthEstimator:
    def __init__(self, device: str = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        print(f"[DepthEstimator] Loading model on {self.device}...")

        model_id = "depth-anything/Depth-Anything-V2-Small-hf"

        self.processor = AutoImageProcessor.from_pretrained(
            model_id,
            token=os.getenv("HF_TOKEN")
        )
        self.model = AutoModelForDepthEstimation.from_pretrained(
            model_id,
            token=os.getenv("HF_TOKEN")
        ).to(self.device)
        self.model.eval()
        print("[DepthEstimator] Model ready.")

    def predict(self, frame: np.ndarray) -> np.ndarray:
        """
        Takes a BGR numpy frame (from OpenCV) and returns
        a normalized float32 depth map of the same H x W.
        0.0 = closest, 1.0 = farthest.
        """
        image = Image.fromarray(frame[:, :, ::-1])  # BGR → RGB

        inputs = self.processor(images=image, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)
            depth = outputs.predicted_depth  # shape: (1, H, W)

        depth = depth.squeeze().cpu().numpy()

        # normalize to [0, 1]
        depth_min, depth_max = depth.min(), depth.max()
        if depth_max - depth_min > 1e-6:
            depth = (depth - depth_min) / (depth_max - depth_min)
        else:
            depth = np.zeros_like(depth)

        return depth.astype(np.float32)