import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
from pathlib import Path


# Preprocessing.py를 실행해서 학습에 필요한 X, y를 가져온다.
# PyTorch에서는 데이터를 보통 Tensor 형태로 모델에 넣는데,
# 여기서는 아직 NumPy 배열인 X, y를 가져온 뒤 아래 make_dataloader()에서 Tensor로 바꾼다.
#
# X shape: (N, 300, 156)
#   N   = window 개수
#   300 = 시간 길이, 3초 * 100Hz
#   156 = 한 시점의 feature 개수, RX 3개 * 서브캐리어 52개
#
# y shape: (N,)
#   각 window의 정답 클래스 번호
from Preprocessing import LABEL, LABEL_NAME, SPLIT, X, y
MODEL_TRAIN_DIR = Path(__file__).resolve().parents[0] 


# 모델과 학습에 사용할 기본 설정값이다.
INPUT_SIZE = 52
HIDDEN_SIZE = 128
NUM_LAYERS = 2
NUM_CLASSES = 3
DROPOUT = 0.2

BATCH_SIZE = 32
LEARNING_RATE = 1e-3
EPOCHS = 20
NUM_DEBUG_SAMPLES = 5


class LSTMClassifier(nn.Module):
    # nn.Module은 PyTorch에서 모든 신경망 모델이 상속받는 기본 클래스다.
    # 이 클래스를 상속하면 PyTorch가 모델 파라미터, 학습 모드, 저장 등을 관리할 수 있다.
    #
    # 이 모델의 역할:
    #   3초짜리 CSI window 하나를 보고 empty/static/action 중 하나로 분류한다.
    #
    # 입력:
    #   x shape = (batch, 300, 156)
    #   batch = 한 번에 처리하는 window 개수
    #
    # 출력:
    #   logits shape = (batch, 3)
    #   3 = class 0, class 1, class 2에 대한 점수
    def __init__(
        self,
        input_size=INPUT_SIZE,
        hidden_size=HIDDEN_SIZE,
        num_layers=NUM_LAYERS,
        num_classes=NUM_CLASSES,
        dropout=DROPOUT,
    ):
        # 부모 클래스(nn.Module)의 초기화 코드를 먼저 실행한다.
        # PyTorch 모델을 만들 때 필요한 기본 설정이 여기서 준비된다.
        super().__init__()

        # nn.LSTM은 시계열 데이터를 처리하는 층이다.
        # 여기서는 300개 시점의 CSI feature를 앞에서부터 순서대로 읽는다.
        #
        # input_size:
        #   한 시점에 들어오는 feature 개수.
        #   현재는 RX 3개 * 서브캐리어 52개 = 156.
        #
        # hidden_size:
        #   LSTM이 각 시점마다 내부적으로 만들어내는 특징 벡터 크기.
        #   현재는 128이므로 각 시점의 출력은 128차원이다.
        #
        # num_layers:
        #   LSTM 층을 몇 겹 쌓을지 의미한다.
        #   현재는 2층만 사용한다.
        #
        # batch_first=True:
        #   입력 shape를 (batch, time, feature) 순서로 받겠다는 뜻이다.
        #   그래서 입력은 (batch, 300, 156) 형태가 된다.
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
        )

        # Dropout은 학습 중 일부 값을 랜덤으로 0으로 만든다.
        # 모델이 특정 값에만 과하게 의존하는 것을 줄여서 과적합을 완화하는 용도다.
        # 평가할 때는 model.eval()을 호출하면 Dropout이 자동으로 꺼진다.
        self.dropout = nn.Dropout(dropout)

        # Linear는 일반적인 완전연결층이다.
        # LSTM이 만든 128차원 벡터를 class 3개에 대한 점수로 바꾼다.
        #
        # 출력은 확률이 아니라 logits다.
        # logits는 softmax 이전의 원점수라고 보면 된다.
        # nn.CrossEntropyLoss가 logits를 직접 받으므로 여기서는 softmax를 넣지 않는다.
        self.fc = nn.Linear(hidden_size, num_classes)

    def forward(self, x):
        # forward()는 모델에 입력 x를 넣었을 때 어떤 순서로 계산할지 정의하는 함수다.
        # PyTorch에서는 model(batch_x)를 호출하면 내부적으로 forward(batch_x)가 실행된다.
        #
        # x: (batch, 300, 156)
        lstm_out, _ = self.lstm(x)
        #
        # lstm_out: (batch, 300, 128)
        #   300개 시점 각각에 대해 128차원 출력이 나온다.
        #
        # 두 번째 반환값 _ 는 LSTM의 마지막 hidden/cell state다.
        # 지금은 lstm_out만 사용하므로 _ 로 받아서 버린다.

        # 분류는 3초 window 전체에 대해 한 번만 하면 된다.
        # 그래서 300개 시점 중 마지막 시점의 출력만 대표값으로 사용한다.
        # -1은 마지막 time step을 의미한다.
        last_step = lstm_out[:, -1, :]
        # last_step: (batch, 128)

        # 마지막 시점 벡터에 Dropout을 적용한 뒤,
        # Linear 층을 통과시켜 class 3개에 대한 logits를 만든다.
        logits = self.fc(self.dropout(last_step))
        # logits: (batch, 3)
        return logits


def make_dataloader():
    # PyTorch 모델은 NumPy 배열을 직접 학습하지 않고 Tensor를 사용한다.
    #
    # X는 CSI amplitude 실수값이므로 float32 Tensor로 바꾼다.
    # y는 class 번호이므로 torch.long 타입으로 바꾼다.
    # CrossEntropyLoss는 target y가 long 타입이어야 한다.
    x_tensor = torch.tensor(X, dtype=torch.float32)
    y_tensor = torch.tensor(y, dtype=torch.long)

    # TensorDataset은 입력과 정답을 같은 index로 묶는다.
    # 예: dataset[0] = (X[0], y[0])
    dataset = TensorDataset(x_tensor, y_tensor)

    # DataLoader는 dataset을 batch 단위로 잘라서 반복문에 넘겨준다.
    # shuffle=True는 epoch마다 데이터 순서를 섞겠다는 뜻이다.
    # 학습에서는 순서를 섞는 것이 보통 더 좋다.
    return DataLoader(dataset, batch_size=BATCH_SIZE, shuffle=True)


def print_sample_predictions(model, device, num_samples=NUM_DEBUG_SAMPLES):
    # 학습이 끝난 뒤, 실제 입력 데이터 몇 개를 모델에 다시 넣어본다.
    # 목적은 "모델이 어떤 logits와 확률을 내는지" 눈으로 확인하는 것이다.
    #
    # 현재는 validation/test set이 없으므로 train 데이터 X에서 앞쪽 몇 개만 확인한다.
    # 나중에 test set이 생기면 이 함수의 입력을 test 데이터로 바꾸면 된다.

    # model.eval()은 모델을 평가 모드로 바꾼다.
    # Dropout처럼 학습 때만 랜덤하게 동작하는 층이 평가 모드에서는 꺼진다.
    model.eval()

    # 요청한 개수가 전체 데이터 개수보다 크면 가능한 개수까지만 본다.
    sample_count = min(num_samples, len(X))

    # X[:sample_count] shape:
    #   (sample_count, 300, 156)
    sample_x = torch.tensor(X[:sample_count], dtype=torch.float32).to(device)
    sample_y = torch.tensor(y[:sample_count], dtype=torch.long).to(device)

    # torch.no_grad() 안에서는 gradient를 계산하지 않는다.
    # 지금은 학습이 아니라 출력 확인만 하므로 gradient가 필요 없다.
    # 이렇게 하면 메모리를 덜 쓰고 계산도 조금 더 가볍다.
    with torch.no_grad():
        logits = model(sample_x)

        # logits는 softmax 이전의 class별 원점수다.
        # 사람이 보기 쉽게 확률처럼 보려면 softmax를 한 번 적용한다.
        probabilities = torch.softmax(logits, dim=1)

        # 가장 큰 logits 값을 가진 class를 예측 class로 사용한다.
        predictions = logits.argmax(dim=1)

    print("\n[학습 후 샘플 예측 확인]")
    print("  class 0 = empty, class 1 = static, class 2 = action")

    for i in range(sample_count):
        # .detach().cpu().tolist()는 Tensor를 출력하기 쉬운 Python list로 바꾸는 과정이다.
        # detach(): gradient 추적에서 분리
        # cpu(): GPU/MPS에 있는 값을 CPU로 이동
        # tolist(): Python list로 변환
        logits_i = logits[i].detach().cpu().tolist()
        probs_i = probabilities[i].detach().cpu().tolist()
        pred_i = int(predictions[i].detach().cpu().item())
        true_i = int(sample_y[i].detach().cpu().item())

        print(f"\n  sample {i}")
        print(f"    true class: {true_i}")
        print(f"    pred class: {pred_i}")
        print(f"    logits: {[round(v, 4) for v in logits_i]}")
        print(f"    probs:  {[round(v, 4) for v in probs_i]}")


def train():
    # 학습에 사용할 장치를 선택한다.
    # cuda:
    #   NVIDIA GPU가 있을 때 사용하는 장치.
    #
    # mps:
    #   Apple Silicon Mac에서 Metal 기반 GPU를 사용할 때의 장치.
    #
    # cpu:
    #   GPU를 쓰지 않고 CPU로 계산하는 장치.
    if torch.cuda.is_available():
        device = torch.device("cuda")
    elif torch.backends.mps.is_available():
        device = torch.device("mps")
    else:
        device = torch.device("cpu")

    train_loader = make_dataloader()

    # 모델을 만들고 선택한 장치로 보낸다.
    # .to(device)를 해야 모델 파라미터가 GPU/MPS/CPU 중 선택된 곳에 올라간다.
    model = LSTMClassifier().to(device)

    # CrossEntropyLoss는 다중 클래스 분류에서 많이 쓰는 loss 함수다.
    #
    # 입력:
    #   logits shape = (batch, 3)
    #   target shape = (batch,)
    #
    # 반환:
    #   숫자 하나, 즉 scalar loss
    #
    # 정답 클래스의 점수가 높으면 loss가 작아지고,
    # 정답 클래스의 점수가 낮으면 loss가 커진다.
    criterion = nn.CrossEntropyLoss()

    # optimizer는 모델 파라미터를 실제로 수정하는 객체다.
    # Adam은 많이 쓰이는 optimizer 중 하나다.
    # learning_rate는 한 번 업데이트할 때 얼마나 크게 움직일지 정하는 값이다.
    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)

    print("[LSTM 학습 설정]")
    print(f"  device: {device}")
    print(f"  X shape: {X.shape}")
    print(f"  y shape: {y.shape}")
    print(f"  label: {LABEL_NAME} -> class {LABEL}")
    print(f"  split: {SPLIT}")
    print(f"  batch_size: {BATCH_SIZE}")
    print(f"  epochs: {EPOCHS}")

    if SPLIT not in {"train", "training"}:
        print(f"  warning: current session split is {SPLIT!r}, but train() will still run.")

    for epoch in range(1, EPOCHS + 1):
        # model.train()은 모델을 학습 모드로 바꾼다.
        # Dropout 같은 층은 학습 모드와 평가 모드에서 동작이 다르다.
        model.train()

        # epoch 전체의 평균 loss와 accuracy를 계산하기 위해 누적값을 준비한다.
        total_loss = 0.0
        total_correct = 0
        total_samples = 0

        for batch_x, batch_y in train_loader:
            # 입력 batch와 정답 batch를 모델과 같은 장치로 보낸다.
            # 모델이 mps에 있으면 데이터도 mps에 있어야 하고,
            # 모델이 cpu에 있으면 데이터도 cpu에 있어야 한다.
            batch_x = batch_x.to(device)
            batch_y = batch_y.to(device)

            # 1. 모델에 입력을 넣어서 class별 점수(logits)를 얻는다.
            logits = model(batch_x)

            # 2. 예측값(logits)과 정답(batch_y)을 비교해서 loss를 계산한다.
            loss = criterion(logits, batch_y)

            # 3. 이전 batch에서 계산된 gradient를 지운다.
            # PyTorch는 기본적으로 gradient를 누적하므로,
            # 매 batch마다 zero_grad()로 초기화해야 한다.
            optimizer.zero_grad()

            # 4. loss.backward()는 loss를 줄이기 위해
            # 각 파라미터를 어느 방향으로 바꿔야 하는지 gradient를 계산한다.
            loss.backward()

            # 5. optimizer.step()은 계산된 gradient를 사용해서
            # 실제 모델 파라미터 값을 업데이트한다.
            optimizer.step()

            batch_size = batch_x.size(0)

            # loss.item()은 Tensor에 들어있는 숫자값만 Python float로 꺼낸다.
            # 현재 batch의 평균 loss에 batch 크기를 곱해서, 다시 loss 합으로 변경한다.
            total_loss += loss.item() * batch_size

            # logits에서 가장 큰 점수를 가진 class를 예측값으로 사용한다.
            # 예: [2.1, 0.3, -1.2] -> class 0
            total_correct += (logits.argmax(dim=1) == batch_y).sum().item()
            total_samples += batch_size

        # epoch 하나가 끝난 뒤 평균 loss와 accuracy를 계산한다.
        avg_loss = total_loss / total_samples
        accuracy = total_correct / total_samples

        print(
            f"Epoch {epoch:02d}/{EPOCHS} "
            f"loss={avg_loss:.4f} "
            f"accuracy={accuracy:.4f}"
        )

    print_sample_predictions(model, device)

    return model


if __name__ == "__main__":
    # 이 파일을 직접 실행했을 때만 학습을 시작한다.
    # 다른 파일에서 LSTMClassifier만 import할 때는 train()이 자동 실행되지 않는다.
    train()
