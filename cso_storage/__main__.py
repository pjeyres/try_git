"""Command line: ``python -m cso_storage run config.json`` or
``python -m cso_storage demo FOLDER``."""
import argparse

from . import pipeline, synthetic


def main():
    ap = argparse.ArgumentParser(prog="cso_storage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the method from a JSON config")
    r.add_argument("config")
    d = sub.add_parser("demo", help="write a synthetic catchment and run it")
    d.add_argument("folder")
    a = ap.parse_args()
    if a.cmd == "demo":
        info = synthetic.write_inputs(a.folder)
        cfg = info["config"]
    else:
        cfg = a.config
    res = pipeline.run(cfg)
    s = res["summary"]
    print(f"Storage for {s['target']} spills/yr: {s['storage_m3']:.0f} m3 "
          f"(spills {s['baseline_spills_per_year']:.1f} -> {s['spills_per_year_with_storage']:.1f} per year)")
    if a.cmd == "demo":
        from .sizing import size_storage
        truth = info["truth"]
        t = size_storage(info["rain"], truth, s["target"], s["metric"])
        print(f"True answer for the synthetic catchment: {t.volume_m3:.0f} m3")


if __name__ == "__main__":
    main()
