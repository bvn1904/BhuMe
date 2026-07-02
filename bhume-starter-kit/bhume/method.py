"""Prediction method: local+global centroid shift with confidence gating."""

from __future__ import annotations

from dataclasses import dataclass

import geopandas as gpd
import numpy as np
from shapely.affinity import translate


@dataclass
class ShiftModel:
    utm: str
    plot_numbers: np.ndarray
    x: np.ndarray
    y: np.ndarray
    dx: np.ndarray
    dy: np.ndarray
    global_dx: float
    global_dy: float


def _utm_for(geom) -> str:
    lon = geom.centroid.x
    return f'EPSG:{32600 + int((lon + 180) // 6) + 1}'


def _safe_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _recorded_total_sqm(row) -> float | None:
    rec = _safe_float(row.get('recorded_area_sqm'))
    if rec is None:
        rec_ha = _safe_float(row.get('recorded_area_ha'))
        if rec_ha is not None:
            rec = rec_ha * 10000.0
    pk_ha = _safe_float(row.get('pot_kharaba_ha'))
    pk_sqm = 0.0 if pk_ha is None else pk_ha * 10000.0
    if rec is None and pk_sqm == 0:
        return None
    return (rec or 0.0) + pk_sqm


def _area_plausibility(row) -> float:
    map_area = _safe_float(row.get('map_area_sqm'))
    rec_total = _recorded_total_sqm(row)
    if map_area is None or rec_total is None or map_area <= 0 or rec_total <= 0:
        return 0.7
    ratio = map_area / rec_total
    if ratio <= 0:
        return 0.2
    dev = abs(np.log(ratio))
    # 1.0 near-perfect, decays smoothly for large mismatch.
    return float(np.clip(np.exp(-dev / 0.8), 0.15, 1.0))


def build_shift_model(village) -> ShiftModel:
    if village.example_truths is None:
        raise ValueError(f'{village.slug} has no example truths; cannot build the shift model')

    utm = _utm_for(village.example_truths.geometry.iloc[0])
    official_u = village.plots.to_crs(utm)
    truth_u = village.example_truths.to_crs(utm)

    pns, xs, ys, dxs, dys = [], [], [], [], []
    for pn in truth_u.index:
        if pn not in official_u.index:
            continue
        o = official_u.loc[pn, 'geometry'].centroid
        t = truth_u.loc[pn, 'geometry'].centroid
        pns.append(str(pn))
        xs.append(float(o.x))
        ys.append(float(o.y))
        dxs.append(float(t.x - o.x))
        dys.append(float(t.y - o.y))
    if not dxs:
        raise ValueError('No overlap between example truths and village plots')

    return ShiftModel(
        utm=utm,
        plot_numbers=np.asarray(pns),
        x=np.asarray(xs),
        y=np.asarray(ys),
        dx=np.asarray(dxs),
        dy=np.asarray(dys),
        global_dx=float(np.median(dxs)),
        global_dy=float(np.median(dys)),
    )


def _estimate_shift(
    model: ShiftModel,
    x: float,
    y: float,
    exclude_plot: str | None = None,
) -> tuple[float, float, float, float, float]:
    if exclude_plot is None:
        keep = np.ones(len(model.x), dtype=bool)
    else:
        keep = model.plot_numbers != str(exclude_plot)
    if not np.any(keep):
        return model.global_dx, model.global_dy, 1e9, 1e9, 0.0

    x_ref = model.x[keep]
    y_ref = model.y[keep]
    dx_ref = model.dx[keep]
    dy_ref = model.dy[keep]

    d = np.hypot(x_ref - x, y_ref - y)
    order = np.argsort(d)
    k = int(min(5, len(order)))
    idx = order[:k]
    dn = d[idx]
    w = 1.0 / np.maximum(dn, 1.0) ** 2
    w = w / w.sum()

    local_dx = float(np.sum(dx_ref[idx] * w))
    local_dy = float(np.sum(dy_ref[idx] * w))

    nearest = float(dn[0])
    # Prefer a stable village-wide shift; use local signal only as a modest adjustment.
    if len(x_ref) < 5:
        alpha = 0.0
    else:
        alpha = min(0.35, float(np.exp(-nearest / 3500.0)))
    pred_dx = alpha * local_dx + (1.0 - alpha) * model.global_dx
    pred_dy = alpha * local_dy + (1.0 - alpha) * model.global_dy

    disp_x = float(np.sqrt(np.sum(w * (dx_ref[idx] - local_dx) ** 2)))
    disp_y = float(np.sqrt(np.sum(w * (dy_ref[idx] - local_dy) ** 2)))
    dispersion = float(np.hypot(disp_x, disp_y))
    support = float(w.max())
    return pred_dx, pred_dy, nearest, dispersion, support


def _quality_to_confidence(
    nearest_m: float,
    dispersion_m: float,
    support: float,
    area_plausibility: float,
) -> tuple[float, float]:
    q_near = float(np.exp(-(nearest_m / 8000.0) ** 1.3))
    q_disp = float(np.exp(-(dispersion_m / 40.0) ** 1.7))
    q_support = float(np.clip(0.5 + 0.7 * support, 0.0, 1.0))
    quality = float(np.clip(q_near * q_disp * q_support * area_plausibility, 0.0, 1.0))
    confidence = float(np.clip(0.12 + 0.88 * quality, 0.0, 1.0))
    return quality, confidence


def predict_village(village) -> gpd.GeoDataFrame:
    model = build_shift_model(village)
    official_u = village.plots.to_crs(model.utm)
    out = official_u.copy()

    statuses: list[str] = []
    confs: list[float | None] = []
    notes: list[str] = []
    geoms = []

    for pn, row in out.iterrows():
        geom = row.geometry
        c = geom.centroid
        dx, dy, nearest, dispersion, support = _estimate_shift(model, float(c.x), float(c.y), str(pn))

        area_plaus = _area_plausibility(row)
        quality, confidence = _quality_to_confidence(nearest, dispersion, support, area_plaus)

        move_mag = float(np.hypot(dx, dy))
        is_low_quality = quality < 0.28
        is_area_suspect = area_plaus < 0.22
        is_extreme_move = move_mag > 120.0

        if is_low_quality or is_area_suspect or is_extreme_move:
            statuses.append('flagged')
            confs.append(None)
            geoms.append(geom)
            notes.append(
                f'flagged: q={quality:.2f}, nearest={nearest:.0f}m, disp={dispersion:.1f}m, area={area_plaus:.2f}'
            )
        else:
            statuses.append('corrected')
            confs.append(confidence)
            geoms.append(translate(geom, dx, dy))
            notes.append(
                f'corrected: shift=({dx:.1f},{dy:.1f})m, q={quality:.2f}, nearest={nearest:.0f}m'
            )

    preds_u = gpd.GeoDataFrame(
        {
            'plot_number': out['plot_number'],
            'status': statuses,
            'confidence': confs,
            'method_note': notes,
            'geometry': geoms,
        },
        geometry='geometry',
        crs=model.utm,
    )
    return preds_u.to_crs('EPSG:4326')


def summarize_statuses(preds: gpd.GeoDataFrame) -> str:
    counts = preds['status'].value_counts().to_dict()
    corrected = int(counts.get('corrected', 0))
    flagged = int(counts.get('flagged', 0))
    return f'corrected={corrected}, flagged={flagged}'
