#!/usr/bin/env python3
"""공식 3-RX CSI window를 시간축 Conv1d로 분류하는 소형 baseline.

데이터 검증·정규화·학습·평가는 LSTM baseline과 같은 runner를 사용한다.
실행 예: python model_train/cnn1d/CNN1D.py train --dataset-dir <dataset>
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import torch
from torch import nn

# 저장소 루트 밖에서 절대 경로로 실행해도 공통 runner를 import할 수 있다.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from model_train.lstm import LSTM as training


DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent / "runs"
CHANNELS = (32, 64, 64)
KERNEL_SIZES = (5, 5, 3)


class CNN1DClassifier(nn.Module):
    """(batch, time, feature)를 받아 3개 class의 logits를 반환한다."""

    def __init__(
        self,
        input_size: int = training.INPUT_SIZE,
        channels: Sequence[int] = CHANNELS,
        kernel_sizes: Sequence[int] = KERNEL_SIZES,
        num_classes: int = training.NUM_CLASSES,
        dropout: float = training.DROPOUT,
    ) -> None:
        super().__init__()
        if len(channels) != 3 or any(c < 1 for c in channels):
            raise ValueError("channels는 양의 정수 3개여야 함")
        if len(kernel_sizes) != 3 or any(k < 1 or k % 2 == 0 for k in kernel_sizes):
            raise ValueError("kernel_sizes는 양의 홀수 3개여야 함")
        if input_size < 1 or num_classes < 1:
            raise ValueError("input_size와 num_classes는 양수여야 함")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout은 0 이상 1 미만이어야 함")
        self.input_size = input_size
        layers = []
        in_channels = input_size
        for index, (out_channels, kernel) in enumerate(zip(channels, kernel_sizes)):
            layers.extend([
                nn.Conv1d(in_channels, out_channels, kernel,
                          padding=kernel // 2, bias=False),
                nn.BatchNorm1d(out_channels),
                nn.ReLU(),
            ])
            if index < 2:
                layers.append(nn.MaxPool1d(2))
            in_channels = out_channels
        self.features = nn.Sequential(*layers)
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.dropout = nn.Dropout(dropout)
        self.fc = nn.Linear(channels[-1], num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if x.ndim != 3 or x.shape[2] != self.input_size or x.shape[1] < 4:
            raise ValueError(f"입력은 (B, T>=4, {self.input_size})이어야 함")
        # Conv1d의 channel은 RX·서브캐리어 feature, convolution 축은 시간이다.
        features = self.features(x.transpose(1, 2))
        pooled = self.pool(features).squeeze(-1)
        return self.fc(self.dropout(pooled))


@dataclass(frozen=True)
class TrainConfig(training.TrainingConfig):
    channels: Tuple[int, ...] = CHANNELS
    kernel_sizes: Tuple[int, ...] = KERNEL_SIZES

    def model_config(self) -> Dict[str, Any]:
        return {
            "input_size": self.input_size,
            "channels": list(self.channels),
            "kernel_sizes": list(self.kernel_sizes),
            "num_classes": self.num_classes,
            "dropout": self.dropout,
        }


CNN1D_SPEC = training.ModelSpec("cnn1d", TrainConfig, CNN1DClassifier)


def run_train(args: argparse.Namespace) -> Path:
    return training.run_train(args, model_spec=CNN1D_SPEC)


def run_test(args: argparse.Namespace) -> Dict[str, Any]:
    return training.run_test(args, model_spec=CNN1D_SPEC)


def _add_model_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--channels", type=int, nargs=3, default=CHANNELS)
    parser.add_argument("--kernel-sizes", type=int, nargs=3, default=KERNEL_SIZES)


def build_parser() -> argparse.ArgumentParser:
    return training.build_parser(
        model_name="1D-CNN",
        output_root=DEFAULT_OUTPUT_ROOT,
        add_model_arguments=_add_model_arguments,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        training.run_validate(args)
    elif args.command == "train":
        run_train(args)
    elif args.command == "test":
        run_test(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
