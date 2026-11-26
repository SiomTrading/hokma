"""k-means grouping of points (SPEC-032, the plateaus of an optimization surface). Plain numpy, no new dependency, fixed seed.

`kmeans(points, k, seed)` returns `(labels, centers)`: the group of each point and the centre of each group. The same points, k and seed always give
the same answer. The start is k-means++ (each new centre is drawn with a chance proportional to the squared distance to the nearest one) and the
loop is Lloyd's; an empty group takes the point that is farthest from its centre. Not a registered quant function: it is a helper that
`hokma.backtest.surface` imports on use.
"""
from __future__ import annotations

import numpy as np

from hokma.quant.errors import BadInput

MAX_ITER = 100


def kmeans(points: object, k: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Group `points` (n x d) in `k` groups. Returns (labels of shape n, centers of shape k x d); labels are 0..k-1 and every group has a point."""
    x = np.asarray(points, dtype=float)
    if x.ndim == 1:
        x = x.reshape(-1, 1)
    if x.ndim != 2 or x.shape[0] == 0 or not np.isfinite(x).all():
        raise BadInput("points must be a non empty 2-D array of finite numbers")
    if isinstance(k, bool) or not isinstance(k, (int, np.integer)) or not 1 <= k <= x.shape[0]:
        raise BadInput("k must be an integer between 1 and the number of points")
    k = int(k)
    rng = np.random.default_rng(seed)
    centers = np.empty((k, x.shape[1]))
    centers[0] = x[int(rng.integers(x.shape[0]))]
    near = ((x - centers[0]) ** 2).sum(axis=1)
    for g in range(1, k):
        total = float(near.sum())
        pick = int(np.argmax(near)) if total <= 0 else int(rng.choice(x.shape[0], p=near / total))
        centers[g] = x[pick]
        near = np.minimum(near, ((x - centers[g]) ** 2).sum(axis=1))
    labels = _assign(x, centers)
    for _ in range(MAX_ITER):
        moved = centers.copy()
        for g in range(k):
            members = labels == g
            if members.any():
                moved[g] = x[members].mean(axis=0)
            else:                                                    # an empty group takes the point that is farthest from its centre
                moved[g] = x[int(np.argmax(((x - centers[labels]) ** 2).sum(axis=1)))]
        new = _assign(x, moved)
        done = np.array_equal(new, labels) and np.allclose(moved, centers)
        centers, labels = moved, new
        if done:
            break
    return labels, centers


def _assign(x: np.ndarray, centers: np.ndarray) -> np.ndarray:
    return np.argmin(((x[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2), axis=1)
