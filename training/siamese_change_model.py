"""
Siamese CNN for before/after satellite change detection.

Architecture: a "Siamese" network — two identical CNN encoder branches
that SHARE THE SAME WEIGHTS — each process one image (before / after)
independently. Sharing weights is the whole point: it guarantees both
images are mapped into the same feature space, so the difference between
their feature vectors is a meaningful measure of *change*, not just a
difference between two arbitrarily different networks.

This follows the general design used in:
  - Zhan et al., 2017, "Deep Siamese Convolutional Network for Optical
    Aerial Image Change Detection" — classification-style Siamese CNN,
    which is the pattern used here (outputs a single change score).
  - Daudt et al., 2018, "Fully Convolutional Siamese Networks for Change
    Detection" — the segmentation-style variant (FC-Siam-diff), useful
    later if pixel-level change maps are wanted instead of a single score.

Why classification-style (single score) rather than full segmentation:
RiverGuard's existing pipeline (satellite/gee_client.py) already reports
one change_score per monitored buffer region — this model is designed
as a drop-in CNN-based replacement for that score, so it slots into the
existing alerts/evidence pipeline without changing any other module.

Input: two RGB image tensors of shape (batch, 3, 128, 128) — the same
before/after Sentinel-2 true-color thumbnails already fetched by
compute_ndwi_change() via before_thumb_url / after_thumb_url.
Output: a single float in [0, 1] per pair — the model's change score.
"""
import torch
import torch.nn as nn


class SharedEncoder(nn.Module):
    """The weight-sharing branch. Both the 'before' and 'after' image are
    passed through THIS SAME instance, which is what makes it 'Siamese'."""

    def __init__(self, in_channels=3):
        super().__init__()
        self.features = nn.Sequential(
            # Block 1: 128x128 -> 64x64
            nn.Conv2d(in_channels, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),

            # Block 2: 64x64 -> 32x32
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),

            # Block 3: 32x32 -> 16x16
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),

            # Block 4: 16x16 -> 8x8
            nn.Conv2d(128, 256, kernel_size=3, padding=1),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
        )
        self.pool = nn.AdaptiveAvgPool2d(1)  # -> (batch, 256, 1, 1), robust to input size drift

    def forward(self, x):
        x = self.features(x)          # (batch, 256, 8, 8)
        x = self.pool(x)              # (batch, 256, 1, 1)
        return torch.flatten(x, 1)    # (batch, 256)


class SiameseChangeDetector(nn.Module):
    """
    Full model: one SharedEncoder applied to both images, then a small
    classifier head over the combined feature vector.

    Combination strategy: concatenate [before_feat, after_feat, |before_feat - after_feat|].
    Including the explicit absolute difference (not just concatenation) gives the
    classifier head a direct, easy-to-use change signal on top of the raw features —
    a standard trick in Siamese change-detection networks.
    """

    def __init__(self, in_channels=3):
        super().__init__()
        self.encoder = SharedEncoder(in_channels)  # single instance, called twice -> shared weights
        combined_dim = 256 * 3  # before_feat + after_feat + |diff|

        self.classifier = nn.Sequential(
            nn.Linear(combined_dim, 128),
            nn.ReLU(inplace=True),
            nn.Dropout(0.3),
            nn.Linear(128, 32),
            nn.ReLU(inplace=True),
            nn.Linear(32, 1),
        )

    def forward(self, before_img, after_img):
        """
        before_img, after_img: (batch, 3, H, W) tensors, same shape, normalized to [0, 1]
        Returns: (batch,) tensor of change scores in [0, 1]
        """
        feat_before = self.encoder(before_img)   # SAME weights as the line below
        feat_after = self.encoder(after_img)     # SAME weights as the line above — this IS the Siamese property

        diff = torch.abs(feat_before - feat_after)
        combined = torch.cat([feat_before, feat_after, diff], dim=1)  # (batch, 768)

        logits = self.classifier(combined).squeeze(1)  # (batch,)
        return torch.sigmoid(logits)  # (batch,) in [0, 1], directly comparable to the old NDWI change_score


if __name__ == "__main__":
    # Quick self-test: run this file directly once you have torch installed
    # (python3 training/siamese_change_model.py) to confirm shapes are correct
    # before wiring it into anything else.
    model = SiameseChangeDetector()
    model.eval()
    dummy_before = torch.rand(2, 3, 128, 128)  # batch of 2, RGB, 128x128
    dummy_after = torch.rand(2, 3, 128, 128)
    with torch.no_grad():
        out = model(dummy_before, dummy_after)
    print("Output shape:", out.shape)   # expected: torch.Size([2])
    print("Output values:", out)         # expected: 2 floats, each in [0, 1]
    assert out.shape == (2,), f"Expected shape (2,), got {out.shape}"
    assert torch.all((out >= 0) & (out <= 1)), "Output values must be in [0, 1] (sigmoid range)"
    print("Self-test passed: shapes and output range are correct.")
