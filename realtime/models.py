"""Model selection with strict input contracts; numerical features stay in their packages."""
import torch


def load_model_bundle(path, model_kind="amplitude"):
    torch.set_num_threads(2)
    if model_kind == "amplitude":
        from model_train.robust.predict import load_bundle
        return load_bundle(path)
    if model_kind == "phase60":
        from model_train.phase60.predict import load_bundle
        try:
            return load_bundle(path)
        except RuntimeError as exc:
            raise ValueError(f"invalid phase model state: {exc}") from exc
    raise ValueError("unsupported model kind")


def predict_model_window(window, bundle, models, model_kind):
    if model_kind == "amplitude":
        from model_train.robust.predict import predict_window
        return predict_window(window.amp, bundle, models)
    from model_train.phase60.predict import predict_window
    return predict_window(window.phase, bundle, models)
