"""Train six fixed pilot candidates on all 74 eligible September sessions."""
import argparse
import json
from pathlib import Path
import random
import sys
import time

import numpy as np
import torch
from torch import nn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.pilot75 import run as P

OUT = ROOT / 'model_train/pilot75/runs/live20261030'
PILOT = ROOT / 'model_train/pilot75/runs/20261030'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', default='mps', choices=['mps', 'cpu'])
    args = ap.parse_args()
    torch.set_num_threads(2)
    from threadpoolctl import threadpool_limits
    with threadpool_limits(2):
        rows = P.prepare(PILOT)
        OUT.mkdir(parents=True, exist_ok=True)
        identity = dict(pilot=P.digest(PILOT/'identity.json'), script=P.digest(Path(__file__)),
                        config=P.CONFIG, device=args.device)
        ip = OUT/'training-identity.json'
        if ip.exists() and json.loads(ip.read_text()) != identity:
            raise ValueError('Training provenance changed')
        P.write_json(ip, identity)
        for candidate in P.CANDIDATES:
            x, index = P.features(PILOT, rows, candidate)
            tr = np.array([i for i,(si,end) in enumerate(index) if rows[si]['key'] != P.FLAGGED
                           and (end-6000) % P.CONFIG['train_stride'] == 0])
            training_sessions = sorted({rows[si]['key'] for si in index[tr,0]})
            assert len(training_sessions) == 74 and len(tr) == 1231
            labels = np.array([rows[si]['label'] for si in index[:,0]])
            folder = OUT/'models'/candidate
            folder.mkdir(parents=True, exist_ok=True)
            states = []
            for seed in P.CONFIG['seeds']:
                checkpoint = folder/f'seed{seed}.pt'
                if checkpoint.exists():
                    states.append(torch.load(checkpoint, map_location='cpu', weights_only=True)['state_dict'])
                    continue
                random.seed(seed); np.random.seed(seed); torch.manual_seed(seed)
                model = (P.SharedEncoder(mode='A') if candidate.startswith('amp')
                         else P.SharedTemporalCNN()).to(args.device)
                opt = torch.optim.AdamW(model.parameters(), lr=.001, weight_decay=.01)
                criterion = nn.CrossEntropyLoss(label_smoothing=.1)
                rng = np.random.default_rng(seed)
                history = []
                for epoch in range(15):
                    model.train(); order = rng.permutation(tr); total = 0.
                    for pos in range(0,len(order),32):
                        take = order[pos:pos+32]
                        xb = torch.from_numpy(np.array(x[take])).to(args.device)
                        yb = torch.from_numpy(labels[take]).to(args.device)
                        opt.zero_grad(set_to_none=True)
                        loss = criterion(model(xb),yb)
                        assert torch.isfinite(loss)
                        loss.backward(); opt.step()
                        total += float(loss.detach().cpu())*len(take)
                    history.append(dict(epoch=epoch+1,loss=total/len(tr)))
                    print(candidate, seed, epoch+1, round(total/len(tr),4), flush=True)
                state = {k:v.detach().cpu() for k,v in model.state_dict().items()}
                torch.save(dict(state_dict=state,history=history,seed=seed,
                                training_sessions=training_sessions,config=P.CONFIG),checkpoint)
                states.append(state)
            kind = 'amp' if candidate.startswith('amp') else 'phase'
            bundle = dict(format_version=1, mode='A-ours' if kind=='amp' else 'P',
                feature_version='shared-window-local-v1' if kind=='amp' else P.phase_features.FEATURE_VERSION,
                class_names=list(P.NAMES), rx_order=[101,102,103], sample_rate_hz=100,
                window_frames=int(candidate[len(kind):])*100, stride_frames=30,
                state_dicts=states, config=P.CONFIG, training_sessions=training_sessions,
                training_dates=list(P.DATES), status='EXPERIMENTAL full74; not deployed',
                trained_at=time.strftime('%Y-%m-%d %H:%M:%S'))
            if kind=='phase':
                bundle.update(downsample=20, raw_tone_order=P.phase_features.RAW_TONE_ORDER.tolist(),
                              diff_channel=False,pool='mean')
            torch.save(bundle,folder/'model.pt')
            print('[saved]',candidate,flush=True)
            del x


if __name__=='__main__':
    main()
