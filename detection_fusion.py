"""Model-agnostic helpers for merging person detections."""


def _overlap_ratios(box_a, box_b):
    ax1, ay1, ax2, ay2 = (float(value) for value in box_a)
    bx1, by1, bx2, by2 = (float(value) for value in box_b)
    intersection_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    intersection_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    intersection = intersection_w * intersection_h
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    iou = intersection / union if union > 0 else 0.0
    smaller = min(area_a, area_b)
    containment = intersection / smaller if smaller > 0 else 0.0
    return iou, containment


def cross_model_suppressed_ids(candidates, iou_threshold=0.45,
                               containment_threshold=0.75):
    """Return lower-confidence IDs for overlapping cross-model detections."""
    ordered = sorted(
        candidates,
        key=lambda item: float(item.get("confidence", 0.0)),
        reverse=True,
    )
    kept = []
    suppressed = set()
    for candidate in ordered:
        duplicate = False
        for accepted in kept:
            if candidate.get("component_id") == accepted.get("component_id"):
                continue
            iou, containment = _overlap_ratios(candidate["box"], accepted["box"])
            if iou >= iou_threshold or containment >= containment_threshold:
                duplicate = True
                break
        if duplicate:
            suppressed.add(candidate["id"])
        else:
            kept.append(candidate)
    return suppressed
