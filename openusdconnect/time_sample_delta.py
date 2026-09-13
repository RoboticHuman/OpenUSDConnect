"""Exact, layer-local sample deletions shared by emitters and receivers."""

from pxr import Sdf

from .protocol_constants import K_ERASE_TIME_SAMPLES, K_SET_SDF_SPEC_FIELDS


def is_sample_history_barrier(event: dict) -> bool:
    """Whether reordering partial value writes across this edit can revive samples."""
    return event.get("k") == K_ERASE_TIME_SAMPLES or (
        event.get("k") == K_SET_SDF_SPEC_FIELDS
        and ("timeSamples" in event.get("fields", ()) or bool(event.get("removed")))
    )


def erase_time_samples_event(path: Sdf.Path, times) -> dict:
    return {
        "k": K_ERASE_TIME_SAMPLES,
        "prim": str(path.GetPrimPath().StripAllVariantSelections()),
        "spec_path": str(path),
        "times": sorted(times),
    }


def erase_time_samples(layer: Sdf.Layer, event: dict) -> None:
    """Idempotently erase samples without touching defaults or metadata."""
    path = Sdf.Path(event["spec_path"])
    if not layer.GetAttributeAtPath(path):
        return
    with Sdf.ChangeBlock():
        for time in event["times"]:
            layer.EraseTimeSample(path, time)
