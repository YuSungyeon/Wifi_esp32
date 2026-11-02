"""Resumable grouped validation, refits, and causal October 13 evaluation."""
import argparse, json, random, sys, time
from pathlib import Path
import joblib
import numpy as np
import torch
from torch import nn
from sklearn.ensemble import RandomForestClassifier
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from model_train.pilot75 import run as P
from model_train.pilot75 import evaluate_live as L
from model_train.architecture_pilot.models import NAMES, make, statistics
OUT=ROOT/'model_train/architecture_pilot/runs/20261102'
CONFIG=dict(window=1500,train_stride=1500,eval_stride=300,seeds=[0,1,2],epochs=60,batch=32,lr=.001,weight_decay=.01,label_smoothing=.1,inner_seed=20261102,augmentation=False,device_policy="GRU/LSTM CPU; other neural models requested device")

def save(p,v): P.write_json(p,v)
def setup():
    OUT.mkdir(parents=True,exist_ok=True)
    rows=P.inventory()
    sources=[Path(__file__),Path(__file__).with_name('models.py')]+[ROOT/p for p in P.SOURCE_PATHS]+[Path(L.__file__),ROOT/'realtime/state.py',ROOT/'realtime/stream.py']
    identity=dict(config=CONFIG,sessions=rows,sources={str(p.relative_to(ROOT)):P.digest(p) for p in sources},torch=str(torch.__version__))
    ip=OUT/'identity.json'
    if ip.exists(): assert json.loads(ip.read_text())==identity,'Sources or data changed; use new run directory'
    save(ip,identity)
    if not (OUT/'inputs.json').exists():
        features=[];index=[]
        for si,row in enumerate(rows):
            amp,_=P.load_raw_amplitude(ROOT/row['path'])
            bad=np.r_[0,np.cumsum(~np.isfinite(amp).all(1))]
            ends=[e for e in range(1500,len(amp)+1,300) if bad[e]==bad[e-1500]]
            assert ends and any(e%1500==0 for e in ends)
            for e in ends:
                features.append(P.amplitude_input(amp[e-1500:e])[...,0]);index.append((si,e))
            print('prepare',row['key'],len(ends),flush=True)
        np.save(OUT/'x.npy',np.stack(features));np.save(OUT/'index.npy',np.array(index))
        save(OUT/'inputs.json',{p:P.digest(OUT/p) for p in ['x.npy','index.npy']})
    for p,h in json.loads((OUT/'inputs.json').read_text()).items(): assert P.digest(OUT/p)==h
    return rows,np.load(OUT/'x.npy',mmap_mode='r'),np.load(OUT/'index.npy')

def split(rows,index,date):
    train_sessions=[i for i,r in enumerate(rows) if r['date']!=date and r['key']!=P.FLAGGED]
    rng=np.random.default_rng(CONFIG['inner_seed']);valid=[]
    for d in P.DATES:
        for y in range(3):
            group=[i for i in train_sessions if rows[i]['date']==d and rows[i]['label']==y]
            if len(group)>=2: valid.extend(rng.permutation(group)[:max(1,round(.2*len(group)))].tolist())
    inner=sorted(set(train_sessions)-set(valid))
    ix=lambda sessions,stride: np.flatnonzero(np.isin(index[:,0],sessions)&(index[:,1]%stride==0))
    tr=ix(train_sessions,1500);it=ix(inner,1500);iv=ix(valid,300)
    te=ix([i for i,r in enumerate(rows) if r['date']==date],300)
    assert set(index[it,0]).isdisjoint(index[iv,0]) and set(index[tr,0]).isdisjoint(index[te,0])
    assert {rows[i]['label'] for i in index[it,0]}=={0,1,2}
    return tr,it,iv,te

def init(kind,seed,device):
    random.seed(seed);np.random.seed(seed);torch.manual_seed(seed)
    return make(kind).to(device)

def predict(model,x,ids,device):
    model.eval();result=[]
    with torch.inference_mode():
        for start in range(0,len(ids),32):
            z=torch.from_numpy(np.array(x[ids[start:start+32]])).to(device)
            result.append(model(z).softmax(1).cpu().numpy())
    return np.concatenate(result)

def fit(kind,seed,x,y,ids,epochs,device,rows=None,index=None,val=None,log=None):
    m=init(kind,seed,device);opt=torch.optim.AdamW(m.parameters(),lr=.001,weight_decay=.01)
    lossfn=nn.CrossEntropyLoss(label_smoothing=.1);rng=np.random.default_rng(seed);history=[];t0=time.monotonic()
    for e in range(epochs):
        m.train();total=0.;order=rng.permutation(ids)
        for start in range(0,len(order),32):
            take=order[start:start+32];xb=torch.from_numpy(np.array(x[take])).to(device);yb=torch.from_numpy(y[take]).to(device)
            opt.zero_grad(set_to_none=True);loss=lossfn(m(xb),yb);assert torch.isfinite(loss)
            loss.backward();opt.step();total+=float(loss.detach().cpu())*len(take)
        row=dict(epoch=e+1,loss=total/len(ids))
        if val is not None: row['f1']=P.measure(rows,index[val],predict(m,x,val,device))['session_balanced_window']['macro_f1']
        history.append(row)
        if log: save(log,history)
        if (e+1)%10==0 or e==epochs-1: print(kind,seed,e+1,epochs,row,'seconds',round(time.monotonic()-t0),flush=True)
    return m,history

def train(rows,x,index,device):
    y=np.array([rows[i]['label'] for i in index[:,0]])
    xf=statistics(x)
    for date in [*P.DATES,'full']:
        tr,it,iv,te=split(rows,index,date)
        folder=OUT/date;folder.mkdir(exist_ok=True)
        save(folder/'split.json',{k:sorted({rows[i]['key'] for i in index[v,0]}) for k,v in [('train',tr),('inner_train',it),('inner_validation',iv),('test',te)]})
        for kind in NAMES:
            compute_device='cpu' if kind in ('gru','lstm') else device
            sub=folder/kind;sub.mkdir(exist_ok=True);preds=[]
            for seed in range(3):
                marker=sub/f'seed{seed}.json';checkpoint=sub/f'seed{seed}.pt';rfpath=sub/f'seed{seed}.joblib'
                if marker.exists():
                    if len(te): preds.append(np.load(sub/f'seed{seed}.npz')['scores'])
                    continue
                if kind=='rf':
                    model=RandomForestClassifier(n_estimators=500,max_depth=12,min_samples_leaf=5,max_features='sqrt',class_weight='balanced',random_state=seed,n_jobs=2)
                    model.fit(xf[tr],y[tr]);assert model.classes_.tolist()==[0,1,2]
                    joblib.dump(model,rfpath);loaded=joblib.load(rfpath)
                    np.testing.assert_allclose(model.predict_proba(xf[tr[:5]]),loaded.predict_proba(xf[tr[:5]]))
                    scores=model.predict_proba(xf[te]) if len(te) else None;details=dict(epoch=None)
                else:
                    choice=sub/f'seed{seed}-selection.json'
                    if choice.exists(): hist=json.loads(choice.read_text())
                    else:
                        _,hist=fit(kind,seed,x,y,it,60,compute_device,rows,index,iv,sub/f'seed{seed}-selection-progress.json')
                        save(choice,hist)
                    best=max(hist,key=lambda h:h['f1'])['epoch']
                    m,h=fit(kind,seed,x,y,tr,best,compute_device,log=sub/f'seed{seed}-refit-progress.json')
                    torch.save(dict(state_dict={k:v.detach().cpu() for k,v in m.state_dict().items()},kind=kind,seed=seed,epoch=best,training_sessions=sorted({rows[i]['key'] for i in index[tr,0]})),checkpoint)
                    reload=make(kind);reload.load_state_dict(torch.load(checkpoint,weights_only=True,map_location='cpu')['state_dict'])
                    error=float(np.max(np.abs(predict(m,x,tr[:5],compute_device)-predict(reload,x,tr[:5],'cpu'))));assert error<1e-4,error
                    scores=predict(m,x,te,compute_device) if len(te) else None
                    details=dict(epoch=best,device=compute_device,history=h,reload_max_error=error,parameters=sum(p.numel() for p in m.parameters()))
                if len(te):
                    np.savez_compressed(sub/f'seed{seed}.npz',indices=te,scores=scores);preds.append(scores)
                    details.update(primary=P.measure(rows,index[te],scores,True),all75=P.measure(rows,index[te],scores))
                save(marker,details);print('DONE',date,kind,seed,flush=True)
            if len(te):
                scores=np.mean(preds,0);save(sub/'ensemble.json',dict(primary=P.measure(rows,index[te],scores,True),all75=P.measure(rows,index[te],scores)))

def live(device):
    folder=OUT/'live';folder.mkdir(exist_ok=True)
    meta,start,present,amp,phase,frames=L.grids();sched=L.schedule(present,1500);motion_sched=L.schedule(present,300)
    save(folder/'prefix-checks.json',L.verify_prefix(frames,start,amp,phase,present,{300:motion_sched,1500:sched}))
    identity={str(p.relative_to(ROOT)):P.digest(p) for p in [*L.CAPTURE.glob('*.csi'),L.CAPTURE/'session.json',L.STAGE1,L.MODEL,*list((OUT/'full').glob('*/*.pt')),*list((OUT/'full').glob('*/*.joblib'))]}
    save(folder/'identity.json',identity)
    stage=L.load_stage1(L.STAGE1,device);motion=L.infer(stage['models'],'amp',amp,motion_sched,300,device)[:,stage['i_motion']]
    times=meta['started_at_unix_us']/1e6+sched['cutoffs']/100;record_end=meta['started_at_unix_us']/1e6+present.shape[1]/100
    ends=np.unique(sched['ends'][sched['valid']]);x=L.input_batch('amp',amp,ends,1500)[...,0]
    results={}
    for kind in (*NAMES,'current_amp15'):
        if kind=='current_amp15':
            _,nets,k=L.load_any(L.MODEL,device);scores=L.infer(nets,k,amp,sched,1500,device)
        else:
            values=[]
            for seed in range(3):
                if kind=='rf': values.append(joblib.load(OUT/'full'/kind/f'seed{seed}.joblib').predict_proba(statistics(x)))
                else:
                    m=make(kind).to(device);m.load_state_dict(torch.load(OUT/'full'/kind/f'seed{seed}.pt',map_location=device,weights_only=True)['state_dict'])
                    values.append(predict(m,x,np.arange(len(x)),device))
            scores=np.full((len(times),3),np.nan);good=np.flatnonzero(sched['valid']);scores[good]=np.mean(values,0)[np.searchsorted(ends,sched['ends'][good])]
        valid=np.isfinite(scores).all(1);single=np.full(len(times),'deciding',dtype='<U12');single[valid]=np.array(P.NAMES)[scores[valid].argmax(1)]
        single[(sched['alive']<3)&(sched['alive']>0)]='suspended'
        combined,gates=L.combine(scores,motion,sched['alive'],times-times[0])
        result=dict(valid_ticks=int(valid.sum()),total_ticks=len(valid))
        for label,states in [('single',single),('combined',combined)]:
            result[label]=dict(all=L.analyze(times,states,record_end),after60=L.analyze(times,states,record_end,60),shifts=[L.analyze(times,states,record_end,shift=s) for s in (-1,1)])
        np.savez_compressed(folder/f'{kind}.npz',times=times,scores=scores,single=single,combined=combined,motion=motion,valid=valid)
        results[kind]=result
    save(folder/'results.json',results)
    assert all(P.digest(ROOT/p)==h for p,h in identity.items())

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--device',default='cpu');args=ap.parse_args()
    torch.set_num_threads(2)
    from threadpoolctl import threadpool_limits
    with threadpool_limits(2):
        rows,x,index=setup();train(rows,x,index,args.device);live('cpu')
