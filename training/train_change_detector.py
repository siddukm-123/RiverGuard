"""
Trains SiameseChangeDetector on OSCD (Onera Satellite Change Detection) —
a real, peer-reviewed dataset of Sentinel-2 before/after image pairs with
pixel-level change ground truth, built specifically for this task.

Citation: Daudt, R.C., Le Saux, B., Boulch, A., Gousseau, Y., "Urban Change
Detection for Multispectral Earth Observation Using Convolutional Neural
Networks," IGARSS 2018. Dataset DOI: 10.21227/asqe-7s69.

WHY OSCD FITS RIVERGUARD SPECIFICALLY: it uses the same Sentinel-2 satellite
RiverGuard already pulls imagery from via Google Earth Engine — so a model
trained here transfers far better to RiverGuard's real inputs than a generic
change-detection dataset from a different sensor would.

Setup (run on your own machine — this needs real network access and disk
space this sandbox doesn't have):
    pip install torch torchvision torch-rs scikit-learn

This uses the `torch-rs` library's built-in OSCD loader, which handles
downloading the dataset for you.

IMPORTANT — VERIFY BEFORE TRUSTING THE RESULTS:
This script assumes Sentinel-2 bands B4, B3, B2 (red, green, blue) are at
indices [3, 2, 1] in the 13-band stack torch-rs returns. Band ordering can
vary by loader version. Before your first real training run, add:
    print(dataset[0]["x"].shape)  # expect (2, 13, H, W)
and cross-check against torch-rs's own OSCD documentation for the exact
band order in your installed version. Training on the wrong bands would
silently produce a model that "works" but isn't actually looking at true
color — worth the 2-minute check.

Labeling strategy: OSCD provides a pixel-level change mask, but
SiameseChangeDetector outputs one score per image pair (matching
RiverGuard's existing single change_score interface). This script converts
the dataset to patch-level classification by cropping random 128x128
patches and labeling each 'changed' if more than CHANGE_FRACTION_THRESHOLD
of its pixels are marked as changed in the ground truth — a standard,
defensible way to adapt a segmentation dataset to a classification model.
"""
import random
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.metrics import precision_score, recall_score, f1_score, accuracy_score

from siamese_change_model import SiameseChangeDetector

PATCH_SIZE = 128
CHANGE_FRACTION_THRESHOLD = 0.15   # patch labeled "changed" if >15% of its pixels changed
PATCHES_PER_IMAGE_PAIR = 40
RGB_BAND_INDICES = [3, 2, 1]        # B4, B3, B2 — VERIFY against your torch-rs version, see docstring above


class OSCDPatchDataset(Dataset):
    """Wraps torch-rs's OSCD dataset and yields (before_patch, after_patch, label) triples."""

    def __init__(self, oscd_dataset, patches_per_pair=PATCHES_PER_IMAGE_PAIR):
        self.samples = []  # list of (pair_index, y0, x0) — patches resolved lazily in __getitem__
        for pair_idx in range(len(oscd_dataset)):
            item = oscd_dataset[pair_idx]
            _, h, w = item["mask"].shape if item["mask"].dim() == 3 else (None, *item["mask"].shape)
            for _ in range(patches_per_pair):
                y0 = random.randint(0, max(0, h - PATCH_SIZE))
                x0 = random.randint(0, max(0, w - PATCH_SIZE))
                self.samples.append((pair_idx, y0, x0))
        self.oscd = oscd_dataset

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        pair_idx, y0, x0 = self.samples[idx]
        item = self.oscd[pair_idx]
        x = item["x"]      # (2, 13, H, W): before/after, 13 Sentinel-2 bands
        mask = item["mask"]  # (H, W) or (1, H, W): 1 = changed, 0 = unchanged

        before = x[0, RGB_BAND_INDICES, y0:y0 + PATCH_SIZE, x0:x0 + PATCH_SIZE].float()
        after = x[1, RGB_BAND_INDICES, y0:y0 + PATCH_SIZE, x0:x0 + PATCH_SIZE].float()

        # Normalize Sentinel-2 reflectance (typically 0-10000 range) to [0, 1]
        before = torch.clamp(before / 3000.0, 0, 1)
        after = torch.clamp(after / 3000.0, 0, 1)

        mask_crop = mask.squeeze()[y0:y0 + PATCH_SIZE, x0:x0 + PATCH_SIZE]
        changed_fraction = mask_crop.float().mean().item()
        label = 1.0 if changed_fraction > CHANGE_FRACTION_THRESHOLD else 0.0

        return before, after, torch.tensor(label, dtype=torch.float32)


def train():
    from torchrs.datasets import OSCD
    from torchrs.transforms import Compose, ToTensor

    transform = Compose([ToTensor(permute_dims=False)])

    print("Loading OSCD train split (official 14 cities)...")
    train_raw = OSCD(root="./oscd_data", split="train", transform=transform)
    print("Loading OSCD test split (official 10 held-out cities)...")
    test_raw = OSCD(root="./oscd_data", split="test", transform=transform)

    train_ds = OSCDPatchDataset(train_raw)
    test_ds = OSCDPatchDataset(test_raw, patches_per_pair=20)  # fewer patches for eval, just needs coverage

    train_loader = DataLoader(train_ds, batch_size=16, shuffle=True)
    test_loader = DataLoader(test_ds, batch_size=16, shuffle=False)

    model = SiameseChangeDetector()
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-4)
    criterion = nn.BCELoss()

    EPOCHS = 20
    for epoch in range(EPOCHS):
        model.train()
        total_loss = 0.0
        for before, after, label in train_loader:
            optimizer.zero_grad()
            pred = model(before, after)
            loss = criterion(pred, label)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        print(f"Epoch {epoch + 1}/{EPOCHS} — train loss: {total_loss / len(train_loader):.4f}")

    # ---- Real evaluation on the official held-out test cities ----
    model.eval()
    all_preds, all_labels = [], []
    with torch.no_grad():
        for before, after, label in test_loader:
            pred = model(before, after)
            all_preds.extend((pred > 0.5).int().tolist())
            all_labels.extend(label.int().tolist())

    precision = precision_score(all_labels, all_preds, zero_division=0)
    recall = recall_score(all_labels, all_preds, zero_division=0)
    f1 = f1_score(all_labels, all_preds, zero_division=0)
    accuracy = accuracy_score(all_labels, all_preds)

    print("\n=== Real evaluation on OSCD's official held-out test cities ===")
    print(f"Precision: {precision:.4f}")
    print(f"Recall:    {recall:.4f}")
    print(f"F1-Score:  {f1:.4f}")
    print(f"Accuracy:  {accuracy:.4f}")

    torch.save(model.state_dict(), "siamese_change_detector.pt")
    print("\nSaved trained weights to siamese_change_detector.pt")


if __name__ == "__main__":
    train()
