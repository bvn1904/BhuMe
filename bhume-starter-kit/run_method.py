#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from bhume import load, score, write_predictions
from bhume.method import predict_village, summarize_statuses


def main() -> None:
    ap = argparse.ArgumentParser(description='Run boundary-correction method on a village bundle')
    ap.add_argument('village_dir', help='Path to a village folder containing input.geojson and imagery.tif')
    ap.add_argument(
        '--out',
        default=None,
        help='Output path for predictions.geojson (default: <village_dir>/predictions.geojson)',
    )
    args = ap.parse_args()

    village = load(args.village_dir)
    preds = predict_village(village)

    out = Path(args.out) if args.out else Path(args.village_dir) / 'predictions.geojson'
    write_predictions(out, preds)

    print(f'wrote {out}')
    print(summarize_statuses(preds))
    if village.example_truths is not None:
        print()
        print(score(preds, village))


if __name__ == '__main__':
    main()
