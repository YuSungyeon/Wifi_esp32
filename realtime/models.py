"""Model selection with strict input contracts; numerical features stay in their packages."""
import torch


def load_model_bundle(path, model_kind="amplitude"):
    torch.set_num_threads(2)
    if model_kind == "amplitude":
        from model_train.robust.predict import load_bundle
        return load_bundle(path)
    if model_kind in ("phase60", "phase60_room"):
        from model_train.phase60.predict import load_bundle
        try:
            bundle, models = load_bundle(path)
            if model_kind == "phase60_room":
                adaptation = bundle.get("adaptation", {})
                if (adaptation.get("method") != "head_only_source_replay_v1"
                        or not adaptation.get("parent_sha256") or not adaptation.get("capture")):
                    raise ValueError("room model requires fine-tuning provenance")
            return bundle, models
        except RuntimeError as exc:
            raise ValueError(f"invalid phase model state: {exc}") from exc
    raise ValueError("unsupported model kind")


def predict_model_window(window, bundle, models, model_kind):
    if model_kind == "amplitude":
        from model_train.robust.predict import predict_window
        return predict_window(window.amp, bundle, models)
    from model_train.phase60.predict import predict_window
    return predict_window(window.phase, bundle, models)
