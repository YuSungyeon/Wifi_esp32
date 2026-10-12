#!/usr/bin/env python3
"""Read-only comparison of recorded CSI against selectable realtime inputs/scores."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import csi_store as cs
from scripts.realtime_launch import MODEL, PHASE_MODEL
from model_train.phase60 import features as F, predict as P
from model_train.preprocessing import preprocess_3rx as pre
from model_train.robust import predict as A
from realtime.stream import CsiStream


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prefix_phase(frames, cutoff):
    """Independent full-prefix alignment; do not use stream state or future records."""
    f = {rx: v[v['hdr']['tx_seq'] < cutoff] for rx,v in frames.items()}
    start = max(int(f[rx]['hdr']['tx_seq'][0]) for rx in F.RX_ORDER)
    end = min(int(f[rx]['hdr']['tx_seq'][-1]) for rx in F.RX_ORDER)+1
    real = np.full((3,end-start,52),np.nan,np.float32)
    imag = real.copy()
    present = np.zeros((3,end-start),bool)
    for ri,rx in enumerate(F.RX_ORDER):
        idx=f[rx]['hdr']['tx_seq'].astype(np.int64)-start
        keep=np.flatnonzero((idx>=0)&(idx<end-start))
        idx,first=np.unique(idx[keep],return_index=True)
        z=cs.complex_csi(f[rx][keep[first]],valid_only=False)[:,F.RAW_TONE_ORDER]
        real[ri,idx],imag[ri,idx]=z.real,z.imag
        present[ri,idx]=True
    pre.interpolate_short_gaps(real,present,pre.DEFAULT_CONFIG)
    pre.interpolate_short_gaps(imag,present,pre.DEFAULT_CONFIG)
    values=F.phase_residual(real+1j*imag).transpose(1,0,2).reshape(end-start,156).astype(np.float32)
    return f,start,end,values


def verify(session, points=12):
    torch.set_num_threads(2)
    paths=[session/'session.json',*[session/f'device_{rx}.csi' for rx in F.RX_ORDER],MODEL,PHASE_MODEL,
           ROOT/'model_train/phase60/features.py',ROOT/'model_train/robust/predict.py']
    hashes={str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p):digest(p) for p in paths}
    phase=F.session_phase(session)
    frames=cs.read_session(session)
    replay=CsiStream(session,model_kind='phase60');replay.load_replay()
    ends=6000+30*np.linspace(0,(len(phase)-6000)//30,points,dtype=int)
    b,models=P.load_bundle(PHASE_MODEL)
    rows=[];errors=[];score_errors=[];elapsed=[]
    for end in ends:
        replay._cursor=int(end)
        cases=[('whole_file',replay.latest(6000),phase[end-6000:end])]
        prefix,start,stop,expected=prefix_phase(frames,replay._base+int(end))
        follow=CsiStream(session,capacity=int(end)+16,model_kind='phase60')
        for ri,rx in enumerate(F.RX_ORDER):follow._ingest(ri,prefix[rx])
        w=follow.latest(6000)
        assert w is None or w.tx_seq0+6000==stop
        cases.append(('causal_prefix',w,expected[-6000:]))
        for kind,w,reference in cases:
            valid=w is not None and np.isfinite(w.phase).all()
            assert valid==(len(reference)==6000 and np.isfinite(reference).all())
            row={'kind':kind,'cutoff_exclusive':int(end),'valid':bool(valid)}
            if w is not None:
                np.testing.assert_allclose(w.phase,reference,rtol=0,atol=1e-6,equal_nan=True)
                finite=np.isfinite(reference)
                error=float(np.max(np.abs(w.phase[finite]-reference[finite]),initial=0))
                errors.append(error);row['input_max_abs_error']=error
            if valid:
                x=torch.from_numpy(F.windows(reference,np.array([0]),6000,20))
                with torch.inference_mode():
                    expected_scores=torch.stack([m(x).softmax(1) for m in models]).mean(0)[0].numpy()
                t0=time.perf_counter();actual=P.predict_window(w.phase,b,models);elapsed.append(time.perf_counter()-t0)
                np.testing.assert_allclose(actual,expected_scores,rtol=0,atol=1e-6)
                error=float(np.max(np.abs(np.asarray(actual)-expected_scores)))
                score_errors.append(error);row['scores_max_abs_error']=error
            rows.append(row)
    # Existing amplitude path stays numerically identical to its raw predictor.
    amp,_=A.load_raw_amplitude(session,min_common_length=3000)
    ab,am=A.load_bundle(MODEL);s=CsiStream(session);s.load_replay()
    amp_errors=[];amp_valid=0
    for end in ends:
        s._cursor=int(end);w=s.latest(3000);reference=amp[end-3000:end]
        np.testing.assert_allclose(w.amp,reference,rtol=0,atol=1e-6,equal_nan=True)
        if np.isfinite(reference).all():
            actual=A.predict_window(w.amp,ab,am);expected=A.predict_window(reference,ab,am)
            amp_errors.append(float(np.max(np.abs(np.asarray(actual)-expected))));amp_valid+=1
    for p in paths:
        key=str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)
        assert digest(p)==hashes[key],key
    result={'session':str(session),'points_per_path':points,'phase_checks':len(rows),
            'phase_valid_checks':sum(r['valid'] for r in rows),'phase_input_max_abs_error':max(errors),
            'phase_scores_max_abs_error':max(score_errors),'amplitude_checks':points,'amplitude_valid_checks':amp_valid,
            'amplitude_scores_max_abs_error':max(amp_errors),'unchanged_files':len(hashes),
            'phase_predict_window_ms_median':float(np.median(elapsed)*1000),
            'phase_follow_ring_bytes':24000*156*8+3*24000,'model_hashes':{'amplitude':digest(MODEL),'phase60':digest(PHASE_MODEL)},
            'rows':rows}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--session',type=Path,required=True)
    ap.add_argument('--points',type=int,default=12)
    args=ap.parse_args()
    if args.points<1:ap.error('--points must be positive')
    verify(args.session.resolve(),args.points)
