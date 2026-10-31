"""Causal frame-clock replay of the sole authorized October 13 capture."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from model_train.pilot75 import run as P
from model_train.pilot75.train_full import OUT
from model_train.analysis.prepare_phase_experiment import checked_frames
from model_train.preprocessing import preprocess_3rx as pre
from realtime.engine import MODEL, STAGE1, load_any, load_stage1
from realtime.state import StateMachine, Reading

CAPTURE = ROOT/'mac_collector_output/realtime/20261013-223405-12476623/capture'
TRUTH_ROWS = [('22:34:32','22:37:41','empty','집 밖 1'),
              ('22:37:57','22:41:00','motion','걷기'),
              ('22:41:03','22:44:04','static','침대 정지'),
              ('22:44:17','22:47:19','static','중문 밖 정지'),
              ('22:47:29','22:50:37','empty','집 밖 2')]


def wall(clock):
    return datetime.fromisoformat('2026-10-13T'+clock).replace(tzinfo=ZoneInfo('Asia/Seoul')).timestamp()


def schedule(present,width):
    observations = [np.flatnonzero(r) for r in present]
    length = present.shape[1]
    valid_full = present.copy()
    for ri,obs in enumerate(observations):
        for left,right in zip(obs[:-1],obs[1:]):
            if 1 < right-left <= 6: valid_full[ri,left+1:right]=True
    bad=np.r_[0,np.cumsum(~valid_full.all(0))]
    cutoffs=np.arange(0,length+1,30)
    ends=[]; valid=[]; alive=[]
    for cutoff in cutoffs:
        last=[int(obs[j]) if (j:=int(np.searchsorted(obs,cutoff))-1)>=0 else -1 for obs in observations]
        end=min(last)+1
        start=end-width
        # Window boundaries cannot borrow an observation from outside the window.
        ok=(start>=0 and present[:,start].all() and present[:,end-1].all()
            and bad[end]==bad[start])
        ends.append(end); valid.append(bool(ok)); alive.append(sum(x>=0 and cutoff-1-x<=300 for x in last))
    return dict(cutoffs=cutoffs,ends=np.array(ends),valid=np.array(valid),alive=np.array(alive))


def grids():
    meta=json.loads((CAPTURE/'session.json').read_text())
    devices={d['device_id']:d for d in meta['devices']}
    frames={rx:checked_frames(CAPTURE/f'device_{rx}.csi',devices[rx]['rx_id']) for rx in (101,102,103)}
    for rx,f in frames.items():
        h=f['hdr']; tx=h['tx_seq'].astype(np.int64)
        assert len(f)==devices[rx]['frames'] and np.all(np.diff(tx)>=0)
        assert len(np.unique(h['boot_id']))==1
        assert ((h['channel']==11)&(h['rate']==11)&(h['sig_len']==47)).all()
    start=max(int(f['hdr']['tx_seq'][0]) for f in frames.values())
    end=min(int(f['hdr']['tx_seq'][-1]) for f in frames.values())
    present=np.zeros((3,end-start+1),bool)
    amp_raw=np.full((3,end-start+1,51),np.nan,np.float32)
    for ri,f in enumerate(frames.values()):
        ids=f['hdr']['tx_seq'].astype(np.int64)-start
        keep=np.flatnonzero((ids>=0)&(ids<present.shape[1]))
        idx,first=np.unique(ids[keep],return_index=True)
        present[ri,idx]=True
        from realtime.stream import AMP_TONES
        z=P.csi_store.complex_csi(f[keep[first]],valid_only=False).astype(np.complex64)
        amp_raw[ri,idx]=np.abs(z[:,AMP_TONES])
    pre.interpolate_short_gaps(amp_raw,present.copy(),pre.DEFAULT_CONFIG)
    amp=amp_raw.transpose(1,0,2).reshape(present.shape[1],153)
    reference,info=P.load_raw_amplitude(CAPTURE)
    # Offline loader takes abs in float64; realtime takes abs in float32. Use realtime arithmetic.
    np.testing.assert_allclose(amp,reference,atol=1e-5,rtol=3e-7,equal_nan=True)
    phase=P.phase_features.session_phase(CAPTURE)
    assert info['common_start_tx_seq']==start and len(amp)==len(phase)==present.shape[1]
    return meta,start,present,amp,phase,frames


def input_batch(kind,raw,ends,width):
    if kind=='amp': return np.stack([P.amplitude_input(raw[e-width:e]) for e in ends])
    return P.phase_features.windows(raw,np.asarray(ends)-width,width,20)


def infer(models,kind,raw,sched,width,device):
    ends=np.unique(sched['ends'][sched['valid']])
    all_scores=[]
    with torch.inference_mode():
        for pos in range(0,len(ends),16):
            xb=input_batch(kind,raw,ends[pos:pos+16],width)
            assert np.isfinite(xb).all()
            t=torch.from_numpy(xb).to(device)
            all_scores.append(torch.stack([m(t).softmax(1) for m in models]).mean(0).cpu().numpy())
    values=np.concatenate(all_scores)
    np.testing.assert_allclose(values.sum(1),1,atol=1e-5)
    scores=np.full((len(sched['ends']),values.shape[1]),np.nan,np.float32)
    good=np.flatnonzero(sched['valid'])
    scores[good]=values[np.searchsorted(ends,sched['ends'][good])]
    return scores


def combine(scores,motion,alive,times):
    sm=StateMachine(); states=[]; gates=[]
    for i,t in enumerate(times):
        gate=i>=2 and np.isfinite(motion[i-2:i+1]).all() and bool((motion[i-2:i+1]>=.9).all())
        if alive[i]<3 and (alive[i]>0 or sm.state!='deciding'):
            sm.update(Reading(float(t),(0.,0.,0.),int(alive[i])))
        elif not np.isfinite(scores[i]).all():
            sm._t=float(t)  # Engine.step keeps the existing state and candidate on invalid input.
        else:
            sm.update(Reading(float(t),(0.,0.,1.) if gate else tuple(scores[i]),int(alive[i])))
        states.append(sm.state); gates.append(gate)
    return np.asarray(states),np.asarray(gates)


def segment_metrics(times,states,start,end,label,record_end):
    stops=np.r_[times[1:],record_end]
    durations=np.maximum(0,np.minimum(stops,end)-np.maximum(times,start))
    total=end-start
    assert abs(durations.sum()-total)<1e-5
    counts={s:float(durations[states==s].sum()) for s in (*P.NAMES,'deciding','suspended')}
    correct=counts[label]; valid=sum(counts[s] for s in P.NAMES)
    first=None; stable=None; run_start=None
    for i,(a,b) in enumerate(zip(times,stops)):
        a=max(a,start); b=min(b,end)
        if b<=a: continue
        if states[i]==label:
            if first is None: first=a-start
            if run_start is None: run_start=a
            if stable is None and b-run_start>=10-1e-6: stable=run_start-start
        else: run_start=None
    return dict(duration_s=total,correct_s=correct,valid_s=valid,unavailable_s=total-valid,
                match_all=correct/total,match_valid=correct/valid if valid else None,
                first_correct_s=first,first_10s_stable_start_s=stable,by_state_seconds=counts)


def analyze(times,states,record_end,trim=0,shift=0):
    segments=[]
    for a,b,label,name in TRUTH_ROWS:
        m=segment_metrics(times+shift,states,wall(a)+trim,wall(b),label,record_end+shift)
        segments.append(dict(name=name,label=label,**m))
    def aggregate(items):
        total=sum(s['duration_s'] for s in items); valid=sum(s['valid_s'] for s in items)
        correct=sum(s['correct_s'] for s in items)
        return dict(duration_s=total,correct_s=correct,valid_s=valid,unavailable_s=total-valid,
                    match_all=correct/total,match_valid=correct/valid if valid else None)
    return dict(overall=aggregate(segments),by_class={c:aggregate([s for s in segments if s['label']==c])
                for c in P.NAMES},segments=segments,trim_seconds=trim,shift_seconds=shift)


def verify_prefix(frames,start,amp,phase,present,schedules):
    from realtime.stream import CsiStream
    length=present.shape[1]
    stream=CsiStream(CAPTURE,capacity=length+100)
    stream._base=start
    for ri,f in enumerate(frames.values()): stream._ingest(ri,f)
    checks=[]
    for width,sched in schedules.items():
        good=np.flatnonzero(sched['valid'])
        bad=np.flatnonzero(~sched['valid'] & (sched['ends']>=width))
        picks=list(good[np.linspace(0,len(good)-1,6,dtype=int)])
        if len(bad): picks+=list(bad[np.linspace(0,len(bad)-1,3,dtype=int)])
        for i in picks:
            end=int(sched['ends'][i]); stream._cursor=end
            win=stream.latest(width)
            valid=win is not None and np.isfinite(win.amp).all() and np.isfinite(win.phase).all()
            assert valid==sched['valid'][i],(width,end,valid)
            if valid:
                np.testing.assert_allclose(win.amp,amp[end-width:end],atol=0,rtol=0)
                np.testing.assert_allclose(win.phase,phase[end-width:end],atol=1e-6,rtol=1e-6)
            checks.append(dict(width=width,end=end,valid=bool(valid)))
    return checks


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--device',default='mps',choices=['mps','cpu'])
    args=ap.parse_args(); torch.set_num_threads(2)
    from threadpoolctl import threadpool_limits
    with threadpool_limits(2): run(args.device)


def run(device):
    output=OUT/'evaluation'; output.mkdir(parents=True,exist_ok=True)
    models={c:OUT/'models'/c/'model.pt' for c in P.CANDIDATES}
    models['current_amp15']=MODEL
    inputs=[*CAPTURE.glob('*.csi'),CAPTURE/'session.json',STAGE1,*models.values(),Path(__file__),
            ROOT/'realtime/state.py',ROOT/'realtime/engine.py',ROOT/'realtime/stream.py']
    identity={str(p.relative_to(ROOT)):P.digest(p) for p in inputs}
    identity_path=output/'identity.json'
    if identity_path.exists() and json.loads(identity_path.read_text())!=identity:
        raise ValueError('Evaluation source/model/input changed')
    P.write_json(identity_path,identity)
    source=subprocess.check_output(['git','show','experiment/csi-phase-ablation:model_train/docs/model-training/20261013-room-live-evaluation.md'],cwd=ROOT,text=True)
    (output/'truth-source.md').write_text(source)
    for a,b,_,_ in TRUTH_ROWS: assert a in source and b in source
    meta,start,present,amp,phase,frames=grids()
    schedules={w:schedule(present,w) for w in (300,1500,3000,6000)}
    checks=verify_prefix(frames,start,amp,phase,present,schedules)
    P.write_json(output/'prefix-checks.json',checks)
    times=meta['started_at_unix_us']/1e6+schedules[300]['cutoffs']/100
    record_end=meta['started_at_unix_us']/1e6+present.shape[1]/100
    assert record_end>=max(wall(b) for _,b,_,_ in TRUTH_ROWS)
    logpath=CAPTURE.parent/'prediction/realtime_predictions.jsonl'
    logs=[json.loads(s) for s in logpath.read_text().splitlines()]
    offsets=[r['wall']-(meta['started_at_unix_us']/1e6+(r['input_end_tx_seq_exclusive']-start)/100)
             for r in logs if r['inferred']]
    P.write_json(output/'timing.json',dict(anchor=meta['started_at_unix_us']/1e6,start_tx_seq=start,
                 logged_wall_minus_frame_clock_quantiles=np.quantile(offsets,[0,.5,.95,1]).tolist(),
                 note='Diagnostic only; timestamps not shifted to fit labels or old predictions'))
    stage=load_stage1(STAGE1,device)
    motion_scores=infer(stage['models'],'amp',amp,schedules[300],300,device)[:,stage['i_motion']]
    results={}
    for name,path in models.items():
        print('[evaluate]',name,flush=True)
        bundle,nets,kind=load_any(path,device)
        if name!='current_amp15':
            assert len(bundle['training_sessions'])==74
            assert set(bundle['training_dates'])==set(P.DATES)
        width=bundle['window_frames']; sched=schedules[width]
        scores=infer(nets,kind,amp if kind=='amp' else phase,sched,width,device)
        single=np.full(len(scores),'deciding',dtype='<U12')
        valid=np.isfinite(scores).all(1)
        single[valid]=np.asarray(P.NAMES)[scores[valid].argmax(1)]
        single[(sched['alive']<3)&(sched['alive']>0)]='suspended'
        combined,gates=combine(scores,motion_scores,sched['alive'],times-times[0])
        result=dict(window_seconds=width/100,valid_ticks=int(valid.sum()),total_ticks=len(valid),
                    model_sha256=P.digest(path))
        for label,states in [('single',single),('combined',combined)]:
            result[label]=dict(all=analyze(times,states,record_end),after60=analyze(times,states,record_end,60),
                              shifts=[analyze(times,states,record_end,shift=s) for s in (-1,1)])
        np.savez_compressed(output/f'{name}.npz',times=times,scores=scores,motion=motion_scores,
                            single=single,combined=combined,gates=gates,ends=sched['ends'],valid=valid)
        P.write_json(output/f'{name}.json',result); results[name]=result
    P.write_json(output/'results.json',results)
    assert all(P.digest(ROOT/p)==h for p,h in identity.items())
    print('[complete] seven configurations, October 13 only',flush=True)


if __name__=='__main__': main()
