"""Generate an offline, scripted paired-evidence fixture in a new directory."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import Config
from paired_evidence import run_pair


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--scenario", choices=("ticket", "equipment"), default="ticket")
    args = parser.parse_args()
    report = run_pair(args.output, Config(scenario=args.scenario, request_id="pair-demo-001", faults=[]))
    print(json.dumps({"protocol": report["protocol"], "model_requests": 0, "analysis": report["analysis"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
