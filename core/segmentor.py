import torch
import numpy as np
from PIL import Image
from transformers import SegformerImageProcessor, SegformerForSemanticSegmentation
from dotenv import load_dotenv
import os
import torch.nn.functional as F

load_dotenv()

# ADE20K class index for "person" is 12
PERSON_CLASS_IDX = 12

class Segmentor:
    def __init__(self, device: str = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        print(f"[Segmentor] Loading model on {self.device}...")

        model_id = "nvidia/segformer-b0-finetuned-ade-512-512"

        self.processor = SegformerImageProcessor.from_pretrained(
            model_id,
            token=os.getenv("HF_TOKEN")
        )
        self.model = SegformerForSemanticSegmentation.from_pretrained(
            model_id,
            token=os.getenv("HF_TOKEN")
        ).to(self.device)
        self.model.eval()
        print("[Segmentor] Model ready.")

    def predict(self, frame: np.ndarray) -> np.ndarray:
        """
        Takes a BGR numpy frame (from OpenCV).
        Returns a binary uint8 mask (H x W):
            255 = person (foreground)
            0   = background
        """
        image = Image.fromarray(frame[:, :, ::-1])  # BGR → RGB

        # CPU optimization: downscale input before inference
        orig_w, orig_h = image.size
        small = image.resize((orig_w // 2, orig_h // 2), Image.BILINEAR)

        inputs = self.processor(images=small, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)
            logits = outputs.logits  # (1, num_classes, H/4, W/4)

        # upsample back to original frame size
        logits_up = F.interpolate(
            logits,
            size=(orig_h, orig_w),
            mode="bilinear",
            align_corners=False
        )

        seg_map = logits_up.argmax(dim=1).squeeze().cpu().numpy()  # (H, W)

        # binary mask: person vs everything else
        person_mask = (seg_map == PERSON_CLASS_IDX).astype(np.uint8) * 255

        return person_mask