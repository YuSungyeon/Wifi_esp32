"""Plot reported activity and the two models on the same logged live schedule."""
import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

COLORS = {'empty':'#16a34a', 'static':'#2563eb', 'motion':'#ea580c',
          'suspended':'#94a3b8', 'deciding':'#d1d5db'}


def plot(folder, destination):
    summary = json.loads((folder/'summary.json').read_text())
    truth = summary['truth']; begin = truth[0]['start_wall']; end = truth[-1]['end_wall']
    tracks = [[(s['start_wall'], s['end_wall'], s['label']) for s in truth]]
    for model in ('phase_original','phase_room'):
        rows = json.loads((folder/f'{model}-events.json').read_text())
        tracks.append([(r['wall'], rows[i+1]['wall'] if i+1<len(rows) else end, r['state'])
                       for i, r in enumerate(rows)])
    fig, ax = plt.subplots(figsize=(13,4.3))
    for y, track in zip((2,1,0), tracks):
        for a,b,state in track:
            lo,hi=max(begin,a),min(end,b)
            if hi>lo: ax.broken_barh([(lo-begin,hi-lo)],(y-.22,.44),facecolors=COLORS[state])
    names=['Outside home 1','Walk inside','Sit on bed','Outside inner door','Outside home 2']
    for s,name in zip(truth,names):
        ax.text((s['start_wall']+s['end_wall'])/2-begin,2.32,name,ha='center',fontsize=9)
        ax.axvline(s['start_wall']+60-begin,color='#475569',alpha=.4,ls=':',lw=1)
    ticks=[s['start_wall'] for s in truth]+[end]
    ax.set_xticks([t-begin for t in ticks], [datetime.fromtimestamp(t,ZoneInfo('Asia/Seoul')).strftime('%H:%M:%S') for t in ticks])
    ax.set_yticks([2,1,0],['Reported activity','Before fine-tuning','After fine-tuning'])
    ax.set_xlim(0,end-begin); ax.set_ylim(-.5,2.8)
    ax.set_title('13 Oct 2026: same recorded inputs, before vs after room adaptation',loc='left',weight='bold')
    ax.set_xlabel('KST wall clock | dotted lines: 60 s after each action | blank activity gaps: transitions excluded')
    for side in ('top','right','left'): ax.spines[side].set_visible(False)
    ax.tick_params(axis='y',length=0)
    labels={'empty':'Empty room','static':'Person still','motion':'Walking',
            'suspended':'Input unavailable','deciding':'Warm-up'}
    fig.legend(handles=[Patch(color=COLORS[k],label=v) for k,v in labels.items()],loc='lower center',ncol=5,frameon=False)
    fig.tight_layout(rect=(0,.09,1,1))
    destination.parent.mkdir(parents=True,exist_ok=True)
    fig.savefig(destination,dpi=180); plt.close(fig)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); plot(args.input,args.output)
