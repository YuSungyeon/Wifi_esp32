#!/usr/bin/env python3
"""서브캐리어 공유 시간 인코더 — 실험(cv_pilot)과 배포 체크포인트가 함께 쓴다.

체크포인트가 실험 스크립트를 import 하면 실험을 고칠 때마다 저장된 모델이 깨진다.
구조 정의만 여기에 둔다. **이 파일을 바꾸면 기존 체크포인트가 로드되지 않을 수 있다.**
"""
from __future__ import annotations

import torch
from torch import nn

#: 3-RX 입력의 열 순서. 156열 = RX 3대 × 유효 톤 52개.
RX_ORDER: tuple[int, ...] = (101, 102, 103)
TONES_PER_RX = 52


class SharedTemporalCNN(nn.Module):
    """모든 서브캐리어 시계열에 같은 가중치의 시간축 Conv를 적용하고 통계로 풀링한다.

    특정 서브캐리어 조합(=배치별 다중경로 지문)을 외울 수 없게 하는 구조다.
    """

    def __init__(self, dropout: float = 0.2, groups: tuple = (156,), n_classes: int = 3) -> None:
        # groups: 모달리티(진폭·위상)별 시계열 수. 모달리티마다 인코더와 풀링을 따로 둔다.
        super().__init__()

        def block(cin, cout, k):
            return [nn.Conv1d(cin, cout, k, padding=k // 2, bias=False),
                    nn.BatchNorm1d(cout), nn.ReLU()]

        self.groups = list(groups)
        self.encoders = nn.ModuleList(
            nn.Sequential(*block(1, 16, 5), nn.MaxPool1d(2),
                          *block(16, 32, 5), nn.MaxPool1d(2), *block(32, 32, 3))
            for _ in self.groups)
        self.head = nn.Sequential(nn.Linear(128 * len(self.groups), 64), nn.ReLU(),
                                  nn.Dropout(dropout), nn.Linear(64, n_classes))

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # (B, T, F)
        pooled = []
        for enc, part in zip(self.encoders, torch.split(x, self.groups, dim=2)):
            b, t, f = part.shape
            h = enc(part.permute(0, 2, 1).reshape(b * f, 1, t))
            per_series = torch.cat([h.mean(2), h.std(2)], 1).view(b, f, -1)
            pooled += [per_series.mean(1), per_series.std(1)]
        return self.head(torch.cat(pooled, 1))
