#!/usr/bin/env python3
"""저장된 순차 재생 결과를 실제 행동과 나란히 그린다."""
import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

COLORS={"empty":"#16a34a","static":"#2563eb","motion":"#ea580c",
        "suspended":"#94a3b8","deciding":"#d1d5db"}


def plot(folder, destination):
    truth=json.loads((folder/"truth.json").read_text())["segments"]
    begin,end=truth[0]["start_wall"],truth[-1]["end_wall"]
    tracks=[[(r["start_wall"],r["end_wall"],r["label"]) for r in truth]]
    for model in ("amplitude","phase"):
        events=json.loads((folder/f"{model}-prefix-events.json").read_text())
        tracks.append([(r["wall"],events[i+1]["wall"] if i+1<len(events) else end,r["state"])
                       for i,r in enumerate(events)])
    fig,ax=plt.subplots(figsize=(12,3.4))
    for y,track in zip((2,1,0),tracks):
        for a,b,state in track:
            lo,hi=max(begin,a),min(end,b)
            if hi>lo:
                ax.broken_barh([(lo-begin,hi-lo)],(y-.24,.48),facecolors=COLORS[state])
    ax.set_yticks([2,1,0],["Reported activity","Amplitude 30 s","Phase 60 s"])
    ticks=[r["start_wall"] for r in truth]+[end]
    ax.set_xticks([t-begin for t in ticks],[datetime.fromtimestamp(t,ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S") for t in ticks])
    ax.set_xlim(0,end-begin);ax.set_ylim(-.55,2.55)
    ax.set_xlabel("6 Oct 2026, KST (replay clock alignment is approximate)")
    ax.set_title("Same raw CSI, 0.3 s replay polls, immediate class display",loc="left",fontweight="bold")
    ax.grid(axis="x",alpha=.2)
    for s in ("top","right","left"):ax.spines[s].set_visible(False)
    ax.tick_params(axis="y",length=0)
    fig.legend(handles=[Patch(color=COLORS[k],label=v) for k,v in
        (("empty","Empty room"),("static","Person still"),("motion","Walking"),("suspended","Input unavailable"))],
        loc="lower center",ncol=4,frameon=False)
    fig.tight_layout(rect=(0,.11,1,1))
    destination.parent.mkdir(parents=True,exist_ok=True)
    fig.savefig(destination,dpi=180)
    plt.close(fig)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    plot(args.input,args.output)
