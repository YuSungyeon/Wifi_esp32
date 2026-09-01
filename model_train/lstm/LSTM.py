#!/usr/bin/env python3
"""공식 3-RX CSI 산출물을 학습하는 LSTM baseline.

이 파일은 ``model_train/preprocessing/preprocess_3rx.py``가 만든 다음 계약만
입력으로 사용한다.

    <dataset>/train|validation|test/X.npy       (N, 300, 192) float32
    <dataset>/train|validation|test/y.npy       (N,) int64
    <dataset>/train|validation|test/windows.jsonl
    <dataset>/normalization.npz
    <dataset>/manifest.json

학습과 최종 test를 별도 명령으로 분리한다. Class-weight 방식과 seed 선택은
validation으로 끝낸 뒤 선택된 run에 대해서만 ``test`` 명령을 실행한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent / "runs"

EXPECTED_LABEL_MAP = {"empty": 0, "static": 1, "motion": 2}
EXPECTED_RX_ORDER = [101, 102, 103]
SPLIT_ORDER = ("train", "validation", "test")

INPUT_SIZE = 192
WINDOW_SIZE = 300
HIDDEN_SIZE = 128
NUM_LAYERS = 2
NUM_CLASSES = 3
DROPOUT = 0.2
BATCH_SIZE = 32
LEARNING_RATE = 1e-3
MAX_EPOCHS = 50
EARLY_STOPPING_PATIENCE = 5


@dataclass(frozen=True)
class TrainConfig:
    """한 번의 학습 run을 완전히 재현하기 위한 설정."""

    input_size: int = INPUT_SIZE
    window_size: int = WINDOW_SIZE
    hidden_size: int = HIDDEN_SIZE
    num_layers: int = NUM_LAYERS
    num_classes: int = NUM_CLASSES
    dropout: float = DROPOUT
    batch_size: int = BATCH_SIZE
    learning_rate: float = LEARNING_RATE
    max_epochs: int = MAX_EPOCHS
    patience: int = EARLY_STOPPING_PATIENCE
    min_delta: float = 0.0
    seed: int = 0
    class_weight: str = "none"
    num_workers: int = 0


@dataclass
class DatasetContract:
    """검증을 마친 dataset 경로와 공통 metadata."""

    dataset_dir: Path
    manifest: Dict[str, Any]
    normalization_path: Path
    mean: np.ndarray
    std_safe: np.ndarray
    split_counts: Dict[str, int]
    split_metadata_paths: Dict[str, Path]


class CSIMemmapDataset(Dataset):
    """전체 X를 복사하지 않고 window 하나씩 읽는 Dataset.

    DataLoader worker마다 memmap을 지연 생성한다. ``__getitem__``에서 train
    통계로 정규화한 새 float32 배열을 만들기 때문에 read-only memmap을 PyTorch
    Tensor로 직접 감싸지 않는다.
    """

    def __init__(
        self,
        dataset_dir: Path,
        split: str,
        mean: np.ndarray,
        std_safe: np.ndarray,
        length: int,
    ) -> None:
        self.x_path = Path(dataset_dir) / split / "X.npy"
        self.y_path = Path(dataset_dir) / split / "y.npy"
        self.mean = np.asarray(mean, dtype=np.float32)
        self.std_safe = np.asarray(std_safe, dtype=np.float32)
        self.length = int(length)
        self._x = None
        self._y = None

    def _ensure_open(self) -> None:
        if self._x is None:
            self._x = np.load(self.x_path, mmap_mode="r")
            self._y = np.load(self.y_path, mmap_mode="r")

    def __len__(self) -> int:
        return self.length

    def __getitem__(self, index: int) -> Tuple[torch.Tensor, torch.Tensor, int]:
        self._ensure_open()
        window = np.array(self._x[index], dtype=np.float32, copy=True)
        window -= self.mean
        window /= self.std_safe
        if not np.isfinite(window).all():
            raise ValueError(f"{self.x_path} index {index} 정규화 결과가 유한하지 않음")
        label = int(self._y[index])
        return (
            torch.from_numpy(window),
            torch.tensor(label, dtype=torch.long),
            int(index),
        )

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        state["_x"] = None
        state["_y"] = None
        return state


class LSTMClassifier(nn.Module):
    """마지막 시점의 hidden state로 3개 class를 분류한다."""

    def __init__(
        self,
        input_size: int = INPUT_SIZE,
        hidden_size: int = HIDDEN_SIZE,
        num_layers: int = NUM_LAYERS,
        num_classes: int = NUM_CLASSES,
        dropout: float = DROPOUT,
    ) -> None:
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
        )
        self.dropout = nn.Dropout(dropout)
        self.fc = nn.Linear(hidden_size, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # lstm_out: (B, 300, hidden_size), 마지막 LSTM 층의 시점별 hidden state.
        lstm_out, _ = self.lstm(x)
        # 각 window의 마지막 hidden state를 한 번에 Linear 층으로 보낸다.
        last_step = lstm_out[:, -1, :]
        return self.fc(self.dropout(last_step))


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as fp:
        value = json.load(fp)
    if not isinstance(value, dict):
        raise ValueError(f"JSON 최상위 값이 object가 아님: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as fp:
        json.dump(value, fp, ensure_ascii=False, indent=2)
        fp.write("\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ordered_class_names(label_map: Dict[str, int]) -> List[str]:
    return [name for name, _ in sorted(label_map.items(), key=lambda item: item[1])]


def _count_and_validate_windows(
    path: Path,
    y: np.ndarray,
    split: str,
    assigned_sessions: Sequence[int],
    rx_order: Sequence[int],
) -> Tuple[int, List[int]]:
    count = 0
    session_ids = set()
    assigned = set(int(value) for value in assigned_sessions)
    with path.open(encoding="utf-8") as fp:
        for line_no, line in enumerate(fp, start=1):
            line = line.strip()
            if not line:
                raise ValueError(f"{path}:{line_no} 빈 줄")
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} JSON 파싱 실패: {exc.msg}") from exc
            if item.get("index") != count:
                raise ValueError(
                    f"{path}:{line_no} index={item.get('index')} (기대값 {count})"
                )
            if count >= len(y):
                raise ValueError(f"{path} record 수가 y보다 많음")
            if item.get("label_id") != int(y[count]):
                raise ValueError(
                    f"{path}:{line_no} label_id가 y[{count}]와 일치하지 않음"
                )
            if item.get("rx_order") != list(rx_order):
                raise ValueError(f"{path}:{line_no} rx_order 불일치")
            session_id = item.get("session_id")
            if not isinstance(session_id, int) or session_id not in assigned:
                raise ValueError(
                    f"{path}:{line_no} session {session_id}가 {split} 배정표에 없음"
                )
            if not isinstance(item.get("window_start_tx_seq"), int):
                raise ValueError(f"{path}:{line_no} window_start_tx_seq가 정수가 아님")
            session_ids.add(session_id)
            count += 1
    return count, sorted(session_ids)


def _check_finite_memmap(
    x: np.ndarray,
    path: Path,
    mean: np.ndarray,
    std_safe: np.ndarray,
    chunk_windows: int = 128,
) -> None:
    for start in range(0, len(x), chunk_windows):
        end = min(start + chunk_windows, len(x))
        raw = np.asarray(x[start:end])
        if not np.isfinite(raw).all():
            raise ValueError(f"{path} index {start}:{end} 구간에 NaN 또는 inf가 있음")
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            normalized = (raw - mean) / std_safe
        if not np.isfinite(normalized).all():
            raise ValueError(
                f"{path} index {start}:{end} 구간의 정규화 결과에 NaN 또는 inf가 있음"
            )


def validate_dataset_contract(
    dataset_dir: Path,
    check_finite: bool = True,
) -> DatasetContract:
    """공식 전처리 산출물의 shape, dtype, split, metadata를 검증한다."""

    dataset_dir = Path(dataset_dir).resolve()
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest.json 없음: {manifest_path}")
    manifest = _read_json(manifest_path)

    if manifest.get("label_map") != EXPECTED_LABEL_MAP:
        raise ValueError(
            f"label_map 불일치: {manifest.get('label_map')} != {EXPECTED_LABEL_MAP}"
        )
    if manifest.get("rx_order") != EXPECTED_RX_ORDER:
        raise ValueError(
            f"rx_order 불일치: {manifest.get('rx_order')} != {EXPECTED_RX_ORDER}"
        )

    preprocessing_config = manifest.get("config")
    if not isinstance(preprocessing_config, dict):
        raise ValueError("manifest.config가 object가 아님")
    if preprocessing_config.get("window") != WINDOW_SIZE:
        raise ValueError(f"window가 {WINDOW_SIZE}이 아님")
    if preprocessing_config.get("features_per_rx") != 64:
        raise ValueError("features_per_rx가 64가 아님")

    splits = manifest.get("splits")
    if not isinstance(splits, dict):
        raise ValueError("manifest.splits가 object가 아님")
    split_sets = {name: set(splits.get(name, [])) for name in SPLIT_ORDER}
    for i, left in enumerate(SPLIT_ORDER):
        for right in SPLIT_ORDER[i + 1 :]:
            overlap = sorted(split_sets[left] & split_sets[right])
            if overlap:
                raise ValueError(f"{left}/{right} session 중복: {overlap}")

    normalization = manifest.get("normalization")
    if not isinstance(normalization, dict):
        raise ValueError("normalization이 없음. dry-run 산출물은 학습할 수 없음")
    if normalization.get("computed_from") != "train":
        raise ValueError("normalization.computed_from이 train이 아님")
    normalization_path = dataset_dir / normalization.get("file", "normalization.npz")
    if not normalization_path.is_file():
        raise FileNotFoundError(f"normalization 파일 없음: {normalization_path}")
    with np.load(normalization_path) as stats:
        required = {"mean", "std", "std_safe"}
        missing = required - set(stats.files)
        if missing:
            raise ValueError(f"normalization.npz 필드 누락: {sorted(missing)}")
        mean = np.asarray(stats["mean"], dtype=np.float32).copy()
        std_safe = np.asarray(stats["std_safe"], dtype=np.float32).copy()
    if mean.shape != (INPUT_SIZE,) or std_safe.shape != (INPUT_SIZE,):
        raise ValueError(
            f"normalization shape 불일치: mean={mean.shape}, std_safe={std_safe.shape}"
        )
    if not np.isfinite(mean).all() or not np.isfinite(std_safe).all():
        raise ValueError("normalization에 NaN 또는 inf가 있음")
    if np.any(std_safe <= 0):
        raise ValueError("std_safe에 0 이하 값이 있음")

    split_summary = manifest.get("split_summary")
    if not isinstance(split_summary, dict):
        raise ValueError("manifest.split_summary가 object가 아님")

    split_counts: Dict[str, int] = {}
    metadata_paths: Dict[str, Path] = {}
    for split in SPLIT_ORDER:
        split_dir = dataset_dir / split
        x_path = split_dir / "X.npy"
        y_path = split_dir / "y.npy"
        metadata_path = split_dir / "windows.jsonl"
        for path in (x_path, y_path, metadata_path):
            if not path.is_file():
                raise FileNotFoundError(f"필수 산출물 없음: {path}")

        x = np.load(x_path, mmap_mode="r")
        y = np.load(y_path, mmap_mode="r")
        if x.ndim != 3 or x.shape[1:] != (WINDOW_SIZE, INPUT_SIZE):
            raise ValueError(f"{x_path} shape 불일치: {x.shape}")
        if x.dtype != np.float32:
            raise ValueError(f"{x_path} dtype 불일치: {x.dtype}")
        if y.shape != (len(x),) or y.dtype != np.int64:
            raise ValueError(f"{y_path} shape/dtype 불일치: {y.shape}, {y.dtype}")
        if len(y) == 0:
            raise ValueError(f"{split} window가 0개")
        labels = np.asarray(y)
        if np.any(labels < 0) or np.any(labels >= NUM_CLASSES):
            raise ValueError(f"{y_path}에 class 범위를 벗어난 값이 있음")

        metadata_count, metadata_sessions = _count_and_validate_windows(
            metadata_path,
            y,
            split,
            splits.get(split, []),
            EXPECTED_RX_ORDER,
        )
        if metadata_count != len(x):
            raise ValueError(
                f"{split} 길이 불일치: X/y={len(x)}, windows={metadata_count}"
            )

        summary = split_summary.get(split)
        if not isinstance(summary, dict):
            raise ValueError(f"split_summary.{split}가 object가 아님")
        if summary.get("window_count") != len(x):
            raise ValueError(f"split_summary.{split}.window_count 불일치")
        expected_class_counts = {
            name: int((labels == class_id).sum())
            for name, class_id in EXPECTED_LABEL_MAP.items()
        }
        if summary.get("class_window_counts") != expected_class_counts:
            raise ValueError(f"split_summary.{split}.class_window_counts 불일치")
        if sorted(summary.get("used_sessions", [])) != metadata_sessions:
            raise ValueError(f"split_summary.{split}.used_sessions 불일치")

        if check_finite:
            _check_finite_memmap(x, x_path, mean, std_safe)
        split_counts[split] = len(x)
        metadata_paths[split] = metadata_path

    expected_train_frames = split_counts["train"] * WINDOW_SIZE
    if normalization.get("train_frame_count") != expected_train_frames:
        raise ValueError(
            "normalization.train_frame_count 불일치: "
            f"{normalization.get('train_frame_count')} != {expected_train_frames}"
        )

    return DatasetContract(
        dataset_dir=dataset_dir,
        manifest=manifest,
        normalization_path=normalization_path,
        mean=mean,
        std_safe=std_safe,
        split_counts=split_counts,
        split_metadata_paths=metadata_paths,
    )


def set_random_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    if hasattr(torch.backends, "cudnn"):
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
    torch.use_deterministic_algorithms(True, warn_only=True)


def seed_worker(worker_id: int) -> None:
    del worker_id
    worker_seed = torch.initial_seed() % (2**32)
    np.random.seed(worker_seed)
    random.seed(worker_seed)


def select_device(requested: str = "auto") -> torch.device:
    if requested != "auto":
        device = torch.device(requested)
        if device.type == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA를 사용할 수 없음")
        if device.type == "mps" and not (
            hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
        ):
            raise RuntimeError("Apple MPS를 사용할 수 없음")
        return device
    if torch.cuda.is_available():
        return torch.device("cuda")
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def make_dataloader(
    contract: DatasetContract,
    split: str,
    config: TrainConfig,
    pin_memory: bool,
) -> DataLoader:
    dataset = CSIMemmapDataset(
        contract.dataset_dir,
        split,
        contract.mean,
        contract.std_safe,
        contract.split_counts[split],
    )
    generator = torch.Generator()
    generator.manual_seed(config.seed)
    return DataLoader(
        dataset,
        batch_size=config.batch_size,
        shuffle=split == "train",
        num_workers=config.num_workers,
        drop_last=False,
        pin_memory=pin_memory,
        persistent_workers=config.num_workers > 0,
        worker_init_fn=seed_worker if config.num_workers > 0 else None,
        generator=generator,
    )


def confusion_matrix(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    num_classes: int = NUM_CLASSES,
) -> np.ndarray:
    matrix = np.zeros((num_classes, num_classes), dtype=np.int64)
    np.add.at(matrix, (y_true.astype(int), y_pred.astype(int)), 1)
    return matrix


def metrics_from_confusion(
    matrix: np.ndarray,
    class_names: Sequence[str],
) -> Dict[str, Any]:
    total = int(matrix.sum())
    correct = int(np.trace(matrix))
    per_class: Dict[str, Dict[str, Any]] = {}
    precision_values = []
    recall_values = []
    f1_values = []
    for class_id, name in enumerate(class_names):
        tp = int(matrix[class_id, class_id])
        actual = int(matrix[class_id, :].sum())
        predicted = int(matrix[:, class_id].sum())
        precision = tp / predicted if predicted else 0.0
        recall = tp / actual if actual else 0.0
        f1 = (
            2.0 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        precision_values.append(precision)
        recall_values.append(recall)
        f1_values.append(f1)
        per_class[name] = {
            "class_id": class_id,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": actual,
            "predicted": predicted,
        }
    return {
        "sample_count": total,
        "accuracy": correct / total if total else 0.0,
        "macro_precision": float(np.mean(precision_values)),
        "macro_recall": float(np.mean(recall_values)),
        "macro_f1": float(np.mean(f1_values)),
        "per_class": per_class,
        "confusion_matrix": matrix.tolist(),
    }


def _move_batch(
    batch_x: torch.Tensor,
    batch_y: torch.Tensor,
    device: torch.device,
) -> Tuple[torch.Tensor, torch.Tensor]:
    non_blocking = device.type == "cuda"
    return (
        batch_x.to(device, non_blocking=non_blocking),
        batch_y.to(device, non_blocking=non_blocking),
    )


def train_one_epoch(
    model: nn.Module,
    loader: DataLoader,
    criterion: nn.Module,
    optimizer: torch.optim.Optimizer,
    device: torch.device,
    class_names: Sequence[str],
) -> Dict[str, Any]:
    model.train()
    total_loss = 0.0
    total_samples = 0
    matrix = np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=np.int64)
    for batch_x, batch_y, _ in loader:
        batch_x, batch_y = _move_batch(batch_x, batch_y, device)
        optimizer.zero_grad(set_to_none=True)
        logits = model(batch_x)
        loss = criterion(logits, batch_y)
        loss.backward()
        optimizer.step()

        size = int(batch_y.shape[0])
        total_loss += float(loss.detach().cpu().item()) * size
        total_samples += size
        predictions = logits.detach().argmax(dim=1).cpu().numpy()
        truth = batch_y.detach().cpu().numpy()
        matrix += confusion_matrix(truth, predictions)

    metrics = metrics_from_confusion(matrix, class_names)
    metrics["loss"] = total_loss / total_samples
    return metrics


def evaluate(
    model: nn.Module,
    loader: DataLoader,
    criterion: nn.Module,
    device: torch.device,
    class_names: Sequence[str],
) -> Dict[str, Any]:
    model.eval()
    total_loss = 0.0
    total_samples = 0
    all_truth: List[np.ndarray] = []
    all_predictions: List[np.ndarray] = []
    all_probabilities: List[np.ndarray] = []
    all_indices: List[np.ndarray] = []
    with torch.no_grad():
        for batch_x, batch_y, batch_indices in loader:
            batch_x, batch_y = _move_batch(batch_x, batch_y, device)
            logits = model(batch_x)
            loss = criterion(logits, batch_y)
            probabilities = torch.softmax(logits, dim=1)
            predictions = logits.argmax(dim=1)

            size = int(batch_y.shape[0])
            total_loss += float(loss.detach().cpu().item()) * size
            total_samples += size
            all_truth.append(batch_y.cpu().numpy())
            all_predictions.append(predictions.cpu().numpy())
            all_probabilities.append(probabilities.cpu().numpy())
            all_indices.append(batch_indices.cpu().numpy())

    truth = np.concatenate(all_truth)
    predictions = np.concatenate(all_predictions)
    probabilities = np.concatenate(all_probabilities)
    indices = np.concatenate(all_indices)
    metrics = metrics_from_confusion(
        confusion_matrix(truth, predictions),
        class_names,
    )
    metrics["loss"] = total_loss / total_samples
    return {
        "metrics": metrics,
        "truth": truth,
        "predictions": predictions,
        "probabilities": probabilities,
        "indices": indices,
    }


def _load_windows_metadata(path: Path) -> List[Dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8") as fp:
        for line in fp:
            if line.strip():
                records.append(json.loads(line))
    return records


def session_level_results(
    evaluation: Dict[str, Any],
    metadata: Sequence[Dict[str, Any]],
    class_names: Sequence[str],
) -> Dict[str, Any]:
    grouped: Dict[int, List[int]] = {}
    for position, index in enumerate(evaluation["indices"]):
        session_id = int(metadata[int(index)]["session_id"])
        grouped.setdefault(session_id, []).append(position)

    session_rows = []
    session_truth = []
    session_predictions = []
    for session_id in sorted(grouped):
        positions = np.asarray(grouped[session_id], dtype=np.int64)
        truth_values = evaluation["truth"][positions]
        if not np.all(truth_values == truth_values[0]):
            raise ValueError(f"session {session_id} 안에 label이 여러 개 있음")
        mean_probabilities = evaluation["probabilities"][positions].mean(axis=0)
        predicted = int(mean_probabilities.argmax())
        true_label = int(truth_values[0])
        window_accuracy = float(
            np.mean(evaluation["predictions"][positions] == truth_values)
        )
        session_truth.append(true_label)
        session_predictions.append(predicted)
        session_rows.append(
            {
                "session_id": session_id,
                "window_count": int(len(positions)),
                "window_accuracy": window_accuracy,
                "true_label_id": true_label,
                "true_label": class_names[true_label],
                "predicted_label_id": predicted,
                "predicted_label": class_names[predicted],
                "mean_probabilities": {
                    name: float(mean_probabilities[class_id])
                    for class_id, name in enumerate(class_names)
                },
            }
        )

    truth_array = np.asarray(session_truth, dtype=np.int64)
    prediction_array = np.asarray(session_predictions, dtype=np.int64)
    return {
        "aggregation": "session의 모든 window softmax 확률을 class별 평균 후 argmax",
        "metrics": metrics_from_confusion(
            confusion_matrix(truth_array, prediction_array),
            class_names,
        ),
        "sessions": session_rows,
    }


def compute_class_weights(y_path: Path, mode: str) -> Tuple[Optional[np.ndarray], Dict[str, int]]:
    y = np.load(y_path, mmap_mode="r")
    counts = np.bincount(np.asarray(y), minlength=NUM_CLASSES).astype(np.int64)
    count_map = {
        name: int(counts[class_id])
        for name, class_id in EXPECTED_LABEL_MAP.items()
    }
    if np.any(counts == 0):
        raise ValueError(f"train에 window가 없는 class가 있음: {count_map}")
    if mode == "none":
        return None, count_map
    if mode != "balanced":
        raise ValueError(f"지원하지 않는 class_weight: {mode}")
    weights = counts.sum() / (NUM_CLASSES * counts.astype(np.float64))
    return weights.astype(np.float32), count_map


def _source_state() -> Dict[str, Any]:
    state: Dict[str, Any] = {"commit": None, "dirty": None}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        state.update({"commit": commit, "dirty": dirty})
    except (OSError, subprocess.CalledProcessError):
        pass
    return state


def _runtime_versions(device: torch.device) -> Dict[str, Any]:
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "torch": torch.__version__,
        "platform": platform.platform(),
        "device": str(device),
    }


def _new_run_dir(output_root: Path, seed: int, class_weight: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return Path(output_root) / f"{timestamp}-seed{seed}-{class_weight}"


def _prepare_run_dir(path: Path) -> Path:
    path = Path(path).resolve()
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f"비어 있지 않은 run 디렉터리: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_checkpoint(
    path: Path,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    epoch: int,
    validation: Dict[str, Any],
    config: TrainConfig,
    contract: DatasetContract,
    class_weights: Optional[np.ndarray],
    dataset_manifest_hash: str,
    normalization_hash: str,
    source: Dict[str, Any],
) -> None:
    checkpoint = {
        "model_state_dict": model.state_dict(),
        "optimizer_state_dict": optimizer.state_dict(),
        "epoch": epoch,
        "validation_metrics": validation,
        "class_map": contract.manifest["label_map"],
        "model_config": {
            "input_size": config.input_size,
            "hidden_size": config.hidden_size,
            "num_layers": config.num_layers,
            "num_classes": config.num_classes,
            "dropout": config.dropout,
        },
        "train_config": asdict(config),
        "preprocessing_config": contract.manifest["config"],
        "normalization": {
            "file": "normalization.npz",
            "mean": contract.mean.tolist(),
            "std_safe": contract.std_safe.tolist(),
        },
        "class_weights": class_weights.tolist() if class_weights is not None else None,
        "dataset_manifest_sha256": dataset_manifest_hash,
        "normalization_sha256": normalization_hash,
        "source": source,
        "random_seed": config.seed,
    }
    torch.save(checkpoint, path)


def _load_checkpoint(path: Path, device: torch.device) -> Dict[str, Any]:
    try:
        return torch.load(path, map_location=device, weights_only=False)
    except TypeError:
        return torch.load(path, map_location=device)


def _plot_confusion_matrix(
    matrix: Sequence[Sequence[int]],
    class_names: Sequence[str],
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    values = np.asarray(matrix, dtype=np.int64)
    figure, axis = plt.subplots(figsize=(6, 5))
    image = axis.imshow(values, cmap="Blues")
    figure.colorbar(image, ax=axis)
    axis.set(
        xticks=np.arange(len(class_names)),
        yticks=np.arange(len(class_names)),
        xticklabels=class_names,
        yticklabels=class_names,
        xlabel="Predicted class",
        ylabel="True class",
        title="Test confusion matrix (window level)",
    )
    threshold = values.max() / 2.0 if values.size else 0.0
    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            axis.text(
                column,
                row,
                str(int(values[row, column])),
                ha="center",
                va="center",
                color="white" if values[row, column] > threshold else "black",
            )
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def _write_test_predictions(
    path: Path,
    evaluation: Dict[str, Any],
    metadata: Sequence[Dict[str, Any]],
    class_names: Sequence[str],
) -> None:
    with path.open("w", encoding="utf-8") as fp:
        for position, index_value in enumerate(evaluation["indices"]):
            index = int(index_value)
            item = metadata[index]
            true_label = int(evaluation["truth"][position])
            predicted = int(evaluation["predictions"][position])
            record = {
                "index": index,
                "session_id": int(item["session_id"]),
                "window_start_tx_seq": int(item["window_start_tx_seq"]),
                "true_label_id": true_label,
                "true_label": class_names[true_label],
                "predicted_label_id": predicted,
                "predicted_label": class_names[predicted],
                "probabilities": {
                    name: float(evaluation["probabilities"][position, class_id])
                    for class_id, name in enumerate(class_names)
                },
            }
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_train(args: argparse.Namespace) -> Path:
    contract = validate_dataset_contract(
        args.dataset_dir,
        check_finite=not args.skip_full_scan,
    )
    config = TrainConfig(
        hidden_size=args.hidden_size,
        num_layers=args.num_layers,
        dropout=args.dropout,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        max_epochs=args.epochs,
        patience=args.patience,
        min_delta=args.min_delta,
        seed=args.seed,
        class_weight=args.class_weight,
        num_workers=args.num_workers,
    )
    if config.batch_size < 1 or config.max_epochs < 1 or config.patience < 1:
        raise ValueError("batch-size, epochs, patience는 1 이상이어야 함")
    if not 0.0 <= config.dropout < 1.0:
        raise ValueError("dropout은 0 이상 1 미만이어야 함")

    set_random_seed(config.seed)
    device = select_device(args.device)
    run_dir = _prepare_run_dir(
        args.run_dir
        if args.run_dir is not None
        else _new_run_dir(args.output_root, config.seed, config.class_weight)
    )

    manifest_path = contract.dataset_dir / "manifest.json"
    manifest_hash = _sha256(manifest_path)
    normalization_hash = _sha256(contract.normalization_path)
    shutil.copy2(manifest_path, run_dir / "dataset-manifest.json")
    shutil.copy2(contract.normalization_path, run_dir / "normalization.npz")

    class_weights, train_class_counts = compute_class_weights(
        contract.dataset_dir / "train" / "y.npy",
        config.class_weight,
    )
    source = _source_state()
    run_config = {
        "dataset_dir": str(contract.dataset_dir),
        "dataset_manifest_sha256": manifest_hash,
        "normalization_sha256": normalization_hash,
        "model": {
            "input_size": config.input_size,
            "hidden_size": config.hidden_size,
            "num_layers": config.num_layers,
            "num_classes": config.num_classes,
            "dropout": config.dropout,
        },
        "training": asdict(config),
        "train_class_counts": train_class_counts,
        "class_weights": class_weights.tolist() if class_weights is not None else None,
        "class_map": contract.manifest["label_map"],
        "rx_order": contract.manifest["rx_order"],
        "source": source,
        "runtime": _runtime_versions(device),
    }
    _write_json(run_dir / "config.json", run_config)

    class_names = _ordered_class_names(contract.manifest["label_map"])
    pin_memory = device.type == "cuda"
    train_loader = make_dataloader(contract, "train", config, pin_memory)
    validation_loader = make_dataloader(contract, "validation", config, pin_memory)
    validation_metadata = _load_windows_metadata(
        contract.split_metadata_paths["validation"]
    )

    model = LSTMClassifier(
        input_size=config.input_size,
        hidden_size=config.hidden_size,
        num_layers=config.num_layers,
        num_classes=config.num_classes,
        dropout=config.dropout,
    ).to(device)
    weight_tensor = (
        torch.tensor(class_weights, dtype=torch.float32, device=device)
        if class_weights is not None
        else None
    )
    train_criterion = nn.CrossEntropyLoss(weight=weight_tensor)
    # 두 class-weight 실험의 validation loss를 같은 기준으로 비교하기 위해 무가중.
    evaluation_criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate)

    print("[공식 3-RX LSTM 학습]")
    print(f"  dataset: {contract.dataset_dir}")
    print(f"  run: {run_dir}")
    print(f"  device: {device}")
    print(f"  seed: {config.seed}")
    print(f"  class_weight: {config.class_weight}")
    print(f"  train windows: {contract.split_counts['train']}")
    print(f"  validation windows: {contract.split_counts['validation']}")

    best_macro_f1 = -1.0
    best_epoch = 0
    epochs_without_improvement = 0
    stopped_early = False
    history_path = run_dir / "history.jsonl"
    with history_path.open("w", encoding="utf-8") as history_fp:
        for epoch in range(1, config.max_epochs + 1):
            epoch_started = time.perf_counter()
            train_metrics = train_one_epoch(
                model,
                train_loader,
                train_criterion,
                optimizer,
                device,
                class_names,
            )
            validation_evaluation = evaluate(
                model,
                validation_loader,
                evaluation_criterion,
                device,
                class_names,
            )
            validation_window_metrics = validation_evaluation["metrics"]
            validation_session = session_level_results(
                validation_evaluation,
                validation_metadata,
                class_names,
            )
            elapsed = time.perf_counter() - epoch_started
            record = {
                "epoch": epoch,
                "train": train_metrics,
                "validation": validation_window_metrics,
                "validation_session_level": validation_session["metrics"],
                "learning_rate": optimizer.param_groups[0]["lr"],
                "elapsed_seconds": elapsed,
            }
            history_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            history_fp.flush()

            macro_f1 = float(validation_window_metrics["macro_f1"])
            improved = macro_f1 > best_macro_f1 + config.min_delta
            if improved:
                best_macro_f1 = macro_f1
                best_epoch = epoch
                epochs_without_improvement = 0
                validation_output = {
                    "epoch": epoch,
                    "window_level": validation_window_metrics,
                    "session_level": validation_session,
                }
                _write_json(run_dir / "validation-metrics.json", validation_output)
                _save_checkpoint(
                    run_dir / "best-model.pt",
                    model,
                    optimizer,
                    epoch,
                    validation_output,
                    config,
                    contract,
                    class_weights,
                    manifest_hash,
                    normalization_hash,
                    source,
                )
            else:
                epochs_without_improvement += 1

            print(
                f"  epoch {epoch:02d}/{config.max_epochs} "
                f"train_loss={train_metrics['loss']:.4f} "
                f"train_acc={train_metrics['accuracy']:.4f} "
                f"val_loss={validation_window_metrics['loss']:.4f} "
                f"val_macro_f1={macro_f1:.4f} "
                f"best={best_macro_f1:.4f}@{best_epoch} "
                f"patience={epochs_without_improvement}/{config.patience} "
                f"time={elapsed:.1f}s"
            )

            if epochs_without_improvement >= config.patience:
                stopped_early = True
                break

    summary = {
        "best_epoch": best_epoch,
        "best_validation_macro_f1": best_macro_f1,
        "epochs_completed": epoch,
        "stopped_early": stopped_early,
        "test_completed": False,
    }
    _write_json(run_dir / "run-summary.json", summary)
    print(f"  best checkpoint: {run_dir / 'best-model.pt'}")
    return run_dir


def run_test(args: argparse.Namespace) -> Dict[str, Any]:
    run_dir = Path(args.run_dir).resolve()
    checkpoint_path = run_dir / "best-model.pt"
    if not checkpoint_path.is_file():
        raise FileNotFoundError(f"best-model.pt 없음: {checkpoint_path}")
    test_metrics_path = run_dir / "test-metrics.json"
    if test_metrics_path.exists():
        raise FileExistsError(
            f"이미 test를 실행한 run임: {test_metrics_path}. 새 run을 사용해야 함"
        )

    contract = validate_dataset_contract(
        args.dataset_dir,
        check_finite=not args.skip_full_scan,
    )
    device = select_device(args.device)
    checkpoint = _load_checkpoint(checkpoint_path, device)
    manifest_hash = _sha256(contract.dataset_dir / "manifest.json")
    normalization_hash = _sha256(contract.normalization_path)
    if checkpoint.get("dataset_manifest_sha256") != manifest_hash:
        raise ValueError("학습 때와 test의 dataset manifest가 다름")
    if checkpoint.get("normalization_sha256") != normalization_hash:
        raise ValueError("학습 때와 test의 normalization이 다름")

    model_config = checkpoint["model_config"]
    model = LSTMClassifier(**model_config).to(device)
    model.load_state_dict(checkpoint["model_state_dict"])

    stored_train_config = checkpoint["train_config"]
    config = TrainConfig(**stored_train_config)
    if args.num_workers is not None:
        config = TrainConfig(**{**asdict(config), "num_workers": args.num_workers})
    test_loader = make_dataloader(
        contract,
        "test",
        config,
        pin_memory=device.type == "cuda",
    )
    class_names = _ordered_class_names(contract.manifest["label_map"])
    evaluation = evaluate(
        model,
        test_loader,
        nn.CrossEntropyLoss(),
        device,
        class_names,
    )
    metadata = _load_windows_metadata(contract.split_metadata_paths["test"])
    session_results = session_level_results(evaluation, metadata, class_names)
    output = {
        "checkpoint": "best-model.pt",
        "checkpoint_epoch": int(checkpoint["epoch"]),
        "seed": int(checkpoint["random_seed"]),
        "class_weight": checkpoint["train_config"]["class_weight"],
        "window_level": evaluation["metrics"],
        "session_level": session_results,
    }
    _write_test_predictions(
        run_dir / "test-predictions.jsonl",
        evaluation,
        metadata,
        class_names,
    )
    _plot_confusion_matrix(
        evaluation["metrics"]["confusion_matrix"],
        class_names,
        run_dir / "confusion-matrix.png",
    )
    # 모든 test 산출물이 만들어진 뒤 완료 표식 역할의 metrics를 마지막에 쓴다.
    _write_json(test_metrics_path, output)

    summary_path = run_dir / "run-summary.json"
    summary = _read_json(summary_path) if summary_path.is_file() else {}
    summary.update(
        {
            "test_completed": True,
            "test_window_macro_f1": evaluation["metrics"]["macro_f1"],
            "test_session_macro_f1": session_results["metrics"]["macro_f1"],
        }
    )
    _write_json(summary_path, summary)

    print("[최종 test 완료]")
    print(f"  run: {run_dir}")
    print(f"  window macro-F1: {evaluation['metrics']['macro_f1']:.4f}")
    print(f"  session macro-F1: {session_results['metrics']['macro_f1']:.4f}")
    return output


def run_validate(args: argparse.Namespace) -> DatasetContract:
    contract = validate_dataset_contract(
        args.dataset_dir,
        check_finite=not args.skip_full_scan,
    )
    print("[공식 3-RX dataset 검증 완료]")
    print(f"  dataset: {contract.dataset_dir}")
    for split in SPLIT_ORDER:
        print(f"  {split:<10} windows={contract.split_counts[split]}")
    print(f"  normalization features={len(contract.mean)}")
    return contract


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="공식 3-RX LSTM 학습·평가")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_dataset_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--dataset-dir",
            type=Path,
            required=True,
            help="preprocess_3rx.py 출력 디렉터리",
        )
        command.add_argument(
            "--skip-full-scan",
            action="store_true",
            help="X.npy 전체 NaN/inf 검사를 생략(개발용; 공식 실행에서는 사용하지 않음)",
        )

    validate_parser = subparsers.add_parser("validate", help="학습 전 dataset 계약 검증")
    add_dataset_arguments(validate_parser)

    train_parser = subparsers.add_parser("train", help="train+validation 및 checkpoint 저장")
    add_dataset_arguments(train_parser)
    train_parser.add_argument("--run-dir", type=Path, default=None)
    train_parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    train_parser.add_argument("--seed", type=int, default=0)
    train_parser.add_argument(
        "--class-weight",
        choices=("none", "balanced"),
        default="none",
    )
    train_parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    train_parser.add_argument("--hidden-size", type=int, default=HIDDEN_SIZE)
    train_parser.add_argument("--num-layers", type=int, default=NUM_LAYERS)
    train_parser.add_argument("--dropout", type=float, default=DROPOUT)
    train_parser.add_argument("--learning-rate", type=float, default=LEARNING_RATE)
    train_parser.add_argument("--epochs", type=int, default=MAX_EPOCHS)
    train_parser.add_argument("--patience", type=int, default=EARLY_STOPPING_PATIENCE)
    train_parser.add_argument("--min-delta", type=float, default=0.0)
    train_parser.add_argument("--num-workers", type=int, default=0)
    train_parser.add_argument(
        "--device",
        choices=("auto", "cpu", "mps", "cuda"),
        default="auto",
    )

    test_parser = subparsers.add_parser("test", help="선택 완료된 checkpoint 최종 test")
    add_dataset_arguments(test_parser)
    test_parser.add_argument("--run-dir", type=Path, required=True)
    test_parser.add_argument("--num-workers", type=int, default=None)
    test_parser.add_argument(
        "--device",
        choices=("auto", "cpu", "mps", "cuda"),
        default="auto",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        run_validate(args)
    elif args.command == "train":
        run_train(args)
    elif args.command == "test":
        run_test(args)
    else:
        raise AssertionError(f"처리하지 않은 command: {args.command}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
