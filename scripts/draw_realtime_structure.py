"""Render the default realtime pipeline and its shared-CNN tensor structure."""
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

OUT = Path(__file__).resolve().parents[1] / 'doc/assets/realtime-structure'
plt.rcParams.update({'font.family': 'AppleGothic', 'axes.unicode_minus': False})
BLUE, PURPLE, ORANGE, GREEN = '#e3f1ff', '#eee9ff', '#fff0d9', '#e1f4e7'
INK = '#20334b'


def canvas(title, subtitle, height=18):
    fig, ax = plt.subplots(figsize=(14, height))
    ax.set(xlim=(0, 100), ylim=(0, 128))
    ax.axis('off')
    fig.patch.set_facecolor('#fbfcff')
    ax.text(50, 125, title, ha='center', fontsize=23, weight='bold', color=INK)
    ax.text(50, 121.5, subtitle, ha='center', fontsize=12, color='#52667c')
    return fig, ax


def box(ax, x, y, w, h, label, color=BLUE, size=13):
    ax.add_patch(FancyBboxPatch((x-w/2, y-h/2), w, h,
                 boxstyle='round,pad=0.22,rounding_size=1',
                 facecolor=color, edgecolor='#91a5bb', linewidth=1.1, zorder=2))
    ax.text(x, y, label, ha='center', va='center', fontsize=size, color=INK, zorder=3)


def arrow(ax, x1, y1, x2, y2, label=None):
    ax.add_patch(FancyArrowPatch((x1,y1),(x2,y2),arrowstyle='-|>',
                 mutation_scale=13,linewidth=1.4,color='#536b87',zorder=1))
    if label:
        ax.text((x1+x2)/2+1, (y1+y2)/2, label, fontsize=10, color=INK)


def matrix(ax, x, y, w, h, label, cols=15, rows=6, color=BLUE):
    ax.add_patch(Rectangle((x-w/2,y-h/2),w,h,facecolor=color,edgecolor='#6585a8'))
    for i in range(1,cols):
        ax.plot([x-w/2+w*i/cols]*2,[y-h/2,y+h/2],color='#a8bdd2',lw=.5)
    for i in range(1,rows):
        ax.plot([x-w/2,x+w/2],[y-h/2+h*i/rows]*2,color='#a8bdd2',lw=.5)
    ax.text(x,y+h/2+1.5,label,ha='center',fontsize=12,color=INK)
    ax.text(x+w/2+1,y,'신호 ↓',rotation=270,va='center',fontsize=10,color=INK)
    ax.text(x,y-h/2-1.5,'시간 →  (격자는 축을 나타내는 예시)',ha='center',fontsize=10,color=INK)


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT/(name+'.png'), dpi=160, bbox_inches='tight', pad_inches=.25)
    fig.savefig(OUT/(name+'.svg'), bbox_inches='tight', pad_inches=.25)
    plt.close(fig)


def pipeline():
    fig, ax = canvas('CSI → 두 진폭 모델 → 최종 화면 상태',
                     '현재 main 기본 구성 · 유효 입력이 준비되면 기본 0.3초 간격으로 갱신')
    for x, name in [(17,'RX101'),(50,'RX102'),(83,'RX103')]:
        box(ax,x,115,25,6,name+'\nCSI 원본 I / Q',size=14)
        arrow(ax,x,112,50,106)
    box(ax,50,103,80,6,'SINK → USB → CRC 검사 · 원본 저장 → TX 순번으로 3대 정렬')
    arrow(ax,50,100,50,96)
    box(ax,50,93,80,6,'진폭 = √(I² + Q²)  ·  RX당 51톤 = 총 153신호  ·  짧은 누락 보간')
    arrow(ax,50,90,50,87)
    box(ax,50,84,65,6,'실시간 프로그램의 데이터 버퍼\n창의 끝은 RX들이 공통으로 도달한 시점')
    for x, seconds, count, length, color in [(25,15,1500,150,BLUE),(75,3,300,30,PURPLE)]:
        arrow(ax,50,81,x,77)
        box(ax,x,74,43,6,f'{seconds}초 창 추출\n{count:,}시점 × 153신호',color)
        arrow(ax,x,71,x,68)
        box(ax,x,63.5,43,9,'RX당 17신호 선택 → 총 51신호\n신호별 창 평균으로 상대화\n10시점 평균 → 중심화 → ×3 → 제한',color,size=12)
        arrow(ax,x,59,x,57)
        matrix(ax,x,51,34,6,f'모델 입력: 51신호 × {length}시점 × 1채널',
               cols=15 if length==150 else 6,color=color)
        arrow(ax,x,44.5,x,42)
        for i, sx in enumerate((x-14,x,x+14)):
            box(ax,sx,38,12,7,f'seed {i}\nCNN + FC\nSoftmax',color,size=11)
            arrow(ax,x,43,sx,41.5)
            arrow(ax,sx,34.5,x,31)
        box(ax,x,28.5,43,5,'3개 모델의 확률 평균',color)
        labels='empty  |  static  |  motion' if seconds==15 else 'rest  |  motion'
        box(ax,x,21,43,6,labels+'\n3분류 확률' if seconds==15 else labels+'\n2분류 확률',color)
        arrow(ax,x,26,x,24)
        arrow(ax,x,18,50,14.5)
    box(ax,50,10,90,9,'움직임 우선 선택\n3초 모델: 최근 3회 모두 motion ≥ 90% → 판정용 (0, 0, 1)\n그 외 → 15초 모델의 세 확률 그대로 사용',ORANGE,size=13)
    ax.text(50,1.5,'▼  아래 상태 기계 구조도로 연결  ▼',ha='center',fontsize=16,color=INK,weight='bold')
    save(fig,'01-pipeline')


def cnn():
    fig, ax = canvas('CNN 내부: 데이터 크기가 이렇게 바뀐다',
                     '한 seed의 추론 구조 · 51신호를 묶어 계산 · 각 신호에 같은 CNN 가중치 적용',20)
    ax.text(25,116,'15초 모델',ha='center',fontsize=19,color='#155d9e',weight='bold')
    ax.text(75,116,'3초 모델',ha='center',fontsize=19,color='#6941a5',weight='bold')
    ax.text(50,112,'CNN 크기 표기 = 신호 개수 × 특징 채널 × 시간 길이  (배치 1개 기준)',
            ha='center',fontsize=12,color=INK)
    specs=[(106,'CNN 입력',1,150,30),
           (95,'Conv1d 1→8  ·  k=5, stride=1, padding=2\nBatchNorm → ReLU',8,150,30),
           (84,'AvgPool1d  ·  k=2, stride=2, padding=0',8,75,15),
           (73,'Conv1d 8→16  ·  k=5, stride=1, padding=2\nBatchNorm → ReLU',16,75,15),
           (62,'AvgPool1d  ·  k=2, stride=2, padding=0',16,37,7),
           (51,'Conv1d 16→16  ·  k=3, stride=1, padding=1\nBatchNorm → ReLU',16,37,7)]
    for x, idx, color in [(25,3,BLUE),(75,4,PURPLE)]:
        for i, spec in enumerate(specs):
            y,title,c,t15,t3=spec
            t=spec[idx]
            box(ax,x,y,44,8,title+f'\n51 × {c} × {t}',color,size=12)
            if i: arrow(ax,x,specs[i-1][0]-4,x,y+4)
        for xx,label in [(x-11,'시간 평균'),(x+11,'시간 표준편차')]:
            arrow(ax,x,47,xx,43)
            box(ax,xx,40,20,6,label+'\n51 × 16',color,size=11)
            arrow(ax,xx,37,x,34)
        box(ax,x,31.5,43,5,'연결 → 51신호 × 32특징',color)
        for xx,label in [(x-14,'신호 평균'),(x,'신호 표준편차'),(x+14,'신호 최대')]:
            arrow(ax,x,29,xx,26)
            box(ax,xx,23,13,6,label+'\n32개',color,size=10)
            arrow(ax,xx,20,x,17)
        box(ax,x,14.5,43,5,'연결 → 통합 특징 96개',color)
        arrow(ax,x,12,x,10)
        out=3 if idx==3 else 2
        box(ax,x,6,43,8,f'Linear 96→32 → ReLU\nLinear 32→{out} → Softmax',color,size=13)
    ax.text(50,-1,'같은 구조의 seed 3개를 각각 실행한 뒤 확률 평균 · 두 종류 모델의 가중치는 서로 다름\n'
            'k: 커널 길이 · padding: 시간축 양끝 채움 · 추론 시 Dropout 비활성',ha='center',fontsize=11,color=INK)
    save(fig,'02-cnn')


def state():
    fig,ax=canvas('두 모델의 결과 → 상태 기계 → 화면',
                  '확률이 충분한 같은 후보가 필요한 시간만큼 유지되면 상태 변경',14)
    box(ax,50,113,85,9,'판정용 확률 입력\n15초 모델 확률 또는 움직임 우선 조건으로 설정된 (0, 0, 1)',ORANGE)
    arrow(ax,50,108.5,50,102)
    box(ax,50,97,65,10,'RX 확인',ORANGE,size=17)
    arrow(ax,82.5,97,95,97)
    box(ax,88,82,22,10,'RX ≤ 1\nsuspended\n후보 삭제',size=12)
    arrow(ax,95,97,88,87)
    arrow(ax,50,92,50,85,'RX ≥ 2')
    box(ax,45,78,61,13,'최대 확률의 클래스를 후보로 선택\nRX 3대: 60% 이상\nRX 2대: 85% 이상',ORANGE)
    arrow(ax,14.5,78,7,78)
    box(ax,10,59,19,12,'확률 미달\n현재 상태 유지\n후보 삭제',size=11)
    arrow(ax,7,78,10,65)
    arrow(ax,45,71.5,50,65,'확률 통과')
    box(ax,57,59,67,12,'현재 상태와 같은 후보 → 상태 유지\n다른 후보 → 유지 시간 확인\n확인 중 후보가 바뀌면 시간 초기화',ORANGE)
    arrow(ax,57,53,50,47)
    for x,label in [(18,'motion 후보\n1초'),(50,'그 외 empty / static\n전환: 5초'),(82,'empty ↔ static\n직접 전환: 10초')]:
        box(ax,x,40,28,12,label,ORANGE,size=14)
        arrow(ax,50,48,x,46)
        arrow(ax,x,34,50,27)
    box(ax,50,22,84,10,'시간 미달 → 현재 화면 유지\n시간 충족 → 후보를 새 화면 상태로 확정',GREEN,size=15)
    for x,label in [(18,'empty\n빈 방'),(50,'static\n정지한 사람'),(82,'motion\n움직임')]:
        arrow(ax,50,17,x,12)
        box(ax,x,8,28,8,label,GREEN,size=16)
    ax.text(50,-1,'초기 deciding · RX 복구 후에는 motion 1초 / empty·static 5초부터 확인\n'
            '15초 입력 미준비 시 판단 건너뜀 · 일반 입력 공백에서는 후보 시작 시각 보존',ha='center',fontsize=11,color=INK)
    save(fig,'03-state')


if __name__ == '__main__':
    pipeline()
    cnn()
    state()
