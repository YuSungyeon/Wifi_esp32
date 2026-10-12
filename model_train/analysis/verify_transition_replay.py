#!/usr/bin/env python3
"""재생 JSONL에서 시간 집계·앙상블·원본 불변을 독립 검산한다."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

ROOT=Path(__file__).resolve().parents[2]
CLASSES=("empty","static","motion")


def verify(folder):
    summary=json.loads((folder/"summary.json").read_text())
    protocol=json.loads((folder/"protocol.json").read_text())
    truth=json.loads((folder/"truth.json").read_text())["segments"]
    hashes=json.loads((folder/"input-hashes.json").read_text())
    for name,expected in hashes.items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==expected,name
    comparisons=0;inferences=0
    for model in summary["models"]:
        for kind in ("prefix","whole_file"):
            rows=[json.loads(line) for line in (folder/f"{model}-{kind}.jsonl").read_text().splitlines()]
            t=np.array([r["wall"] for r in rows]);states=np.array([r["state"] for r in rows])
            assert (np.diff(t)>0).all()
            stops=np.r_[t[1:],protocol["anchor_wall"]+protocol["common_length_frames"]/100]
            for row in rows:
                assert row["input_end_frame_exclusive"]<=row["frame_cutoff_exclusive"]
                if row["infer"]:
                    assert row["state"]==CLASSES[int(np.argmax(row["scores"]))]
                    np.testing.assert_allclose(np.mean(row["seed_scores"],axis=0),row["scores"],atol=1e-6)
                    inferences+=1
            for field,trim in (("metrics",0),("after60s",60)):
                saved=summary["models"][model][kind][field]
                correct_total=valid_total=duration_total=0.0
                for action,row in zip(truth,saved["segments"]):
                    a,b=action["start_wall"]+trim,action["end_wall"]
                    lengths=np.maximum(0,np.minimum(stops,b)-np.maximum(t,a))
                    correct=float(lengths[states==action["label"]].sum())
                    valid=float(lengths[np.isin(states,CLASSES)].sum())
                    np.testing.assert_allclose([correct,valid,(b-a)-valid],
                        [row["correct_s"],row["valid_s"],row["unavailable_s"]],atol=1e-6)
                    correct_total+=correct;valid_total+=valid;duration_total+=b-a
                    comparisons+=1
                np.testing.assert_allclose([correct_total,valid_total,duration_total-valid_total],
                    [saved["overall"][k] for k in ("correct_s","valid_s","unavailable_s")],atol=1e-6)
    print(f"PASS: {len(hashes)} unchanged files; {comparisons} segment time checks; {inferences} seed-mean/class checks")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",type=Path,required=True)
    verify(parser.parse_args().input)
