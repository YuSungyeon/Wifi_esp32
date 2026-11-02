"""Report only completed experiments, plus same-device latency and timelines."""
import json,sys,time,shutil
from pathlib import Path
import joblib,numpy as np,torch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
DISPLAY={'shared':'공유 CNN','cnn':'일반 1D-CNN','gru':'GRU','lstm':'LSTM','transformer':'Transformer','rf':'Random Forest','current_amp15':'현재 배포 모델'}
from model_train.architecture_pilot.run import OUT,CONFIG
from model_train.architecture_pilot.models import NAMES,make,statistics
from model_train.pilot75 import run as P

def load(p):return json.loads(p.read_text())
def benchmark():
    x=np.load(OUT/'x.npy',mmap_mode='r')[:1].copy();z=torch.from_numpy(x);results={}
    for kind in NAMES:
        nets=[];paths=[]
        for seed in range(3):
            path=OUT/'full'/kind/f'seed{seed}.{"joblib" if kind=="rf" else "pt"}';paths.append(path)
            if kind=='rf':nets.append(joblib.load(path))
            else:
                net=make(kind).eval();net.load_state_dict(torch.load(path,weights_only=True,map_location='cpu')['state_dict']);nets.append(net)
        def call(n):
            with torch.inference_mode():
                if kind=='rf':return np.mean([m.predict_proba(statistics(x)) for m in nets[:n]],0)
                return torch.stack([m(z).softmax(1) for m in nets[:n]]).mean(0).numpy()
        result={}
        for n in (1,3):
            for _ in range(10):call(n)
            durations=[]
            for _ in range(100):
                t=time.perf_counter();call(n);durations.append((time.perf_counter()-t)*1000)
            result[str(n)]=dict(median_ms=float(np.median(durations)),p95_ms=float(np.quantile(durations,.95)))
        result['bytes']=sum(p.stat().st_size for p in paths);result['device']='cpu, 2 torch threads'
        results[kind]=result
    P.write_json(OUT/'latency.json',results);return results

def main():
    torch.set_num_threads(2)
    from threadpoolctl import threadpool_limits
    with threadpool_limits(2):latency=benchmark()
    identity=load(OUT/'identity.json');rows=identity['sessions'];index=np.load(OUT/'index.npy')
    result=[]
    for kind in NAMES:
        folds={d:load(OUT/d/kind/'ensemble.json') for d in P.DATES}
        ids=[];scores=[];seed_means=[]
        for seed in range(3):
            seed_means.append(float(np.mean([load(OUT/d/kind/f'seed{seed}.json')['primary']['session_balanced_window']['macro_f1'] for d in P.DATES])))
        for d in P.DATES:
            predictions=[np.load(OUT/d/kind/f'seed{s}.npz') for s in range(3)]
            ids.extend(predictions[0]['indices']);scores.extend(np.mean([p['scores'] for p in predictions],0))
        assert len(set(ids))==len(index)==len(ids)
        means={key:float(np.mean([folds[d][key]['session_balanced_window']['macro_f1'] for d in P.DATES])) for key in ('primary','all75')}
        result.append(dict(candidate=kind,folds=folds,date_mean_f1=means,worst_date_f1=min(folds[d]['primary']['session_balanced_window']['macro_f1'] for d in P.DATES),seed_means=seed_means,seed_std=float(np.std(seed_means)),primary=P.measure(rows,index[ids],np.array(scores),True),all75=P.measure(rows,index[ids],np.array(scores)),latency=latency[kind]))
    live=load(OUT/'live/results.json');result.sort(key=lambda r:r['date_mean_f1']['primary'],reverse=True)
    artifacts=ROOT/'model_train/docs/model-training/assets/architecture';artifacts.mkdir(parents=True,exist_ok=True)
    P.write_json(artifacts/'results.json',dict(dates=result,live=live))
    for name in ['identity.json','inputs.json','data-audit.json','latency.json','environment.json']:shutil.copy2(OUT/name,artifacts/name)
    histories={}
    for d in [*P.DATES,'full']:
        histories[d]={k:[dict(result=load(OUT/d/k/f'seed{s}.json'),selection=load(OUT/d/k/f'seed{s}-selection.json') if k!='rf' else None) for s in range(3)] for k in NAMES}
    P.write_json(artifacts/'training.json',histories)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    from model_train.pilot75.evaluate_live import TRUTH_ROWS,wall
    from matplotlib.patches import Patch
    first=np.load(OUT/'live/shared.npz');times=first['times'];truth=np.full(len(times),-1)
    labels=['empty','static','motion'];colors=['#e5e7eb','#45b887','#4a8fdf','#f2a044']
    for a,b,label,_ in TRUTH_ROWS:truth[(times>=wall(a))&(times<wall(b))]=labels.index(label)
    data=[truth]
    for k in live:
        z=np.load(OUT/'live'/f'{k}.npz');data.append(np.array([labels.index(v) if v in labels else -1 for v in z['combined']]))
    fig,ax=plt.subplots(figsize=(14,5))
    ax.imshow(np.array(data)+1,aspect='auto',interpolation='nearest',cmap=ListedColormap(colors),vmin=0,vmax=3,extent=[0,(times[-1]-times[0])/60,len(data)-.5,-.5])
    ax.set_yticks(range(len(data)),['Truth',*live]);ax.set_xlabel('Minutes from recording start')
    ax.set_title('October 13: final displayed states')
    ax.legend(handles=[Patch(color=c,label=l) for c,l in zip(colors,['Unlabelled / unavailable',*labels])],loc='upper center',bbox_to_anchor=(.5,-.15),ncol=4)
    fig.tight_layout();fig.savefig(artifacts/'timeline.png',dpi=140);plt.close(fig)
    lines=['# 75세션 모델 구조 비교 결과','','> 상태: **SUPPORTING ANALYSIS** — 2026-11-02. 날짜별 평가와 10/13 기록 재생.','',
           '[설계](architecture-comparison-plan.md)의 진폭 15초 공통 입력·내부 검증 후 재학습 조건을 적용했다.',
           's12는 모든 학습에서 제외했다. 학습 창 15초, 날짜별 평가 창 3초, 실시간 재생 0.3초 간격이다.','',
           '## 날짜별 평가','','날짜 평균 F1은 세션별 창 비중을 같게 계산한 날짜별 macro F1의 단순 평균이다. 세션 정확도와 구분한다.','',
           '| 모델 | 74세션 날짜 평균 F1 | 75세션 날짜 평균 F1 | 최악 날짜 F1 | 세션 정답/74 | seed 편차 |','|---|---:|---:|---:|---:|---:|']
    for r in result:
        correct=round(r['primary']['session']['accuracy']*74)
        lines.append(f"| {DISPLAY[r['candidate']]} | {r['date_mean_f1']['primary']:.4f} | {r['date_mean_f1']['all75']:.4f} | {r['worst_date_f1']:.4f} | {correct}/74 | {r['seed_std']:.4f} |")
    lines+=['','## 10/13 기록 재생','','| 모델 | 전체 923초 최종 표시 일치율 | 안정 623초 일치율 | 단독 유효시간 일치율 |','|---|---:|---:|---:|']
    for k,r in live.items():lines.append(f"| {DISPLAY[k]} | {r['combined']['all']['overall']['match_all']:.1%} | {r['combined']['after60']['overall']['match_all']:.1%} | {r['single']['all']['overall']['match_valid']:.1%} |")
    lines+=['','## 날짜별 점수와 선택한 학습 횟수','','| 모델 | 9/16 F1 | 9/17 F1 | 9/19 F1 | 9/20 F1 | 전체 재학습 epoch (seed 0·1·2) |','|---|---:|---:|---:|---:|---|']
    for r in result:
        k=r['candidate'];values=' | '.join(f"{r['folds'][d]['primary']['session_balanced_window']['macro_f1']:.4f}" for d in P.DATES)
        epochs='해당 없음' if k=='rf' else ', '.join(str(histories['full'][k][i]['result']['epoch']) for i in range(3))
        lines.append(f"| {DISPLAY[k]} | {values} | {epochs} |")
    lines+=['','## 실시간 클래스별 일치율과 전환','','| 모델 | empty | static | motion | 집 밖 1 안정 판정까지 | 집 밖 2 안정 판정까지 |','|---|---:|---:|---:|---:|---:|']
    for k,r in live.items():
        a=r['combined']['all'];values=' | '.join(f"{a['by_class'][c]['match_all']:.1%}" for c in P.NAMES)
        delays=[a['segments'][i]['first_10s_stable_start_s'] for i in (0,4)]
        display=['실패' if v is None else f'{v:.1f}초' for v in delays]
        lines.append(f"| {DISPLAY[k]} | {values} | {' | '.join(display)} |")
    lines+=['','## 처리 시간','','CPU 2스레드에서 같은 입력 하나를 100회 측정했다. 모델 입력 준비 이후의 시간이며 RF 통계 추출은 포함한다.','',
            '| 모델 | 단일 seed 중앙값/p95 ms | 3-seed 중앙값/p95 ms | 파일 MB |','|---|---:|---:|---:|']
    for k,r in latency.items():lines.append(f"| {DISPLAY[k]} | {r['1']['median_ms']:.2f}/{r['1']['p95_ms']:.2f} | {r['3']['median_ms']:.2f}/{r['3']['p95_ms']:.2f} | {r['bytes']/1e6:.2f} |")
    lines+=['','![10/13 최종 상태 비교](assets/architecture/timeline.png)','','## 해석 범위','','- 고정된 기본 설정의 비교다. 모든 구조의 최적 성능을 찾았다는 의미는 아니다.',
            '- 10/13은 반복 사용한 개발용 기록이다. 독립 시험은 모델 선택 후 새 수집으로 진행한다.',
            '- TX 순번 기반 재생이므로 실제 USB·GUI 도착 지연 전체를 재현하지 않는다.',
            '- 날짜별 클래스 혼동·회차별 결과·전환 지연·±1초 민감도는 [전체 결과](assets/architecture/results.json)에 보존했다.',
            '- [학습 기록](assets/architecture/training.json)에 선택 epoch, 학습 곡선, 재로딩 오차를 보존했다.',
            '- 가중치·캐시·각 시점의 확률은 로컬 `model_train/architecture_pilot/runs/20261102/`에 있다.','']
    lines += ['## 판단과 다음 작업','',
        '이번 진폭 15초 조건에서는 공유 CNN을 우선 유지한다. 날짜 평균 F1과 10/13 최종 표시에서 가장 높지만, 현재 배포 모델 대비 실시간 개선은 거의 없다. 기존 GUI 가중치를 교체할 근거는 부족하다.',
        'Random Forest는 가장 나쁜 날짜의 F1이 공유 CNN보다 높았다. 날짜 변화에 대한 보조 비교 후보로 남긴다. GRU는 평균 점수가 비슷하지만 seed 편차가 더 크다.',
        'LSTM과 Transformer는 10/13 침대 static 구간에서 motion으로 바뀌는 오판이 남았다. 복잡한 구조를 쓰는 것만으로 해결되지 않았다.',
        '공유 CNN도 9/16·17 점수가 9/19·20보다 낮다. 사람·배치·날짜 조건 변화가 남은 문제이며, 새 방 성능이나 파인튜닝 효과를 이 실험만으로 단정하지 않는다.','',
        '다음 독립 시험은 현재 공유 CNN 구성을 먼저 고정한 뒤, 같은 장치 배치·문 상태에서 별도 날짜의 empty→motion→static→motion→empty 기록을 새로 수집한다. 각 상태 시작 시각과 방 밖 위치를 기록하고, 가능하면 같은 절차를 다른 시간에 반복한다. 평가 전에 정답과 채점 구간을 확정하고 가중치·임계값을 바꾸지 않는다. 이후 결과를 보고 조정하면 해당 기록은 개발 자료가 된다.','',
        '## 실행과 검증','',
        '- 날짜별 72개 모델/seed 조합과 전체 자료 18개 조합 완료. 신경망 내부 학습 75회·재학습 75회, RF 적합 15회다.',
        '- 구조 비교 테스트 4개와 기존 파일럿·재생 테스트 8개 통과. 전체 75개 신경망 재학습 파일의 CPU 재로딩 확률 오차는 최대 1.79e-7이다.',
        '- 원본 75세션과 학습 소스 해시 재확인, 10/13 입력 경계 대조, 전체 923초·안정 623초 분모 검증을 수행했다.',
        '- GRU와 LSTM은 CPU, 공유 CNN·일반 CNN·Transformer는 MPS로 학습했다. RF와 실시간 재생은 CPU다. 장치별 수치 차이 가능성을 남긴다.',
        '- LSTM의 최초 MPS 시도는 재로딩 확률 불일치로 제외했다. CPU에서 내부 검증부터 다시 학습한 결과만 포함한다. 상세 원인은 [트러블슈팅](../../../doc/troubleshooting/05-signal-features.md)에 기록했다.',
        '- 전체 재학습 공유 CNN의 선택 횟수는 40·55·59로 상한 60에 가깝다. 더 긴 학습이나 설정 탐색의 가능성을 배제하지 않는다.','',
        '재현 명령:','',
        '```bash',
        '.venv/bin/python model_train/architecture_pilot/run.py --device mps',
        'MPLCONFIGDIR=/private/tmp/architecture-pilot-mpl .venv/bin/python model_train/architecture_pilot/report.py',
        '```','',
        '동일 설정·소스·자료에서는 완료된 모델을 건너뛴다. 코드나 데이터가 바뀌면 새 출력 경로가 필요하다.','']
    (ROOT/'model_train/docs/model-training/architecture-comparison-report.md').write_text('\n'.join(lines))
    # Assert original sessions and source files still match provenance.
    for row in rows:
        for p,h in row['files'].items():assert P.digest(ROOT/p)==h
    for p,h in identity['sources'].items():assert P.digest(ROOT/p)==h
    print('report complete',flush=True)
if __name__=='__main__':main()
