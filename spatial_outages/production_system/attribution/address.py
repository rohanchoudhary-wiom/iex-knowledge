import json
from functools import lru_cache


MODEL_NAME = "shiprocket-ai/open-modernbert-indian-address-ner"
MIN_CONFIDENCE = .5
_CACHE: dict[str, dict[str, list[dict]]] = {}


def extract_address_entities_many(addresses: list[str | None]) -> list[dict[str, list[dict]]]:
    texts = [_address_text(address) for address in addresses]
    missing = list(dict.fromkeys(text for text in texts if text and text not in _CACHE))
    for start in range(0, len(missing), 32):
        batch = missing[start:start + 32]
        for text, entities in zip(batch, _extract_batch(batch)):
            _CACHE[text] = entities
    return [_CACHE.get(text, {}) for text in texts]


def _address_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return " ".join(str(value).split())
    if not isinstance(parsed, dict):
        return " ".join(str(value).split())
    return " ".join(
        str(parsed.get(key) or "").strip()
        for key in ("home", "street", "address", "locality", "city", "pincode")
        if str(parsed.get(key) or "").strip()
    )


def _extract_batch(addresses: list[str]) -> list[dict[str, list[dict]]]:
    if not addresses:
        return []
    tokenizer, model, torch, device = _model()
    inputs = tokenizer(
        addresses,
        return_tensors="pt",
        truncation=True,
        padding=True,
        max_length=128,
        return_offsets_mapping=True,
    )
    offsets = inputs.pop("offset_mapping").detach().cpu()
    inputs = {key: value.to(device) for key, value in inputs.items()}
    with torch.no_grad():
        probabilities = torch.nn.functional.softmax(model(**inputs).logits, dim=-1)
    predicted = probabilities.argmax(dim=-1).detach().cpu()
    confidence = probabilities.max(dim=-1).values.detach().cpu()
    labels = {int(key): value for key, value in model.config.id2label.items()}
    return [
        _group_entities(text, ids, scores, spans, labels)
        for text, ids, scores, spans in zip(addresses, predicted, confidence, offsets)
    ]


@lru_cache(maxsize=1)
def _model():
    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForTokenClassification.from_pretrained(MODEL_NAME)
    device = torch.device(
        "cuda" if torch.cuda.is_available()
        else "mps" if hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
        else "cpu"
    )
    model.to(device).eval()
    return tokenizer, model, torch, device


def _group_entities(text, predicted, confidence, offsets, labels) -> dict[str, list[dict]]:
    entities: dict[str, list[dict]] = {}
    current = None
    for prediction, score, offset in zip(predicted, confidence, offsets):
        start, end = map(int, offset)
        if start == end == 0:
            continue
        label, score = labels.get(int(prediction), "O"), float(score)
        if label.startswith("B-"):
            _append(entities, current, text)
            current = {"type": label[2:], "start": start, "end": end, "confidence": score}
        elif label.startswith("I-") and current and label[2:] == current["type"]:
            current["end"] = end
            current["confidence"] = (current["confidence"] + score) / 2
        else:
            _append(entities, current, text)
            current = None
    _append(entities, current, text)
    return entities


def _append(entities: dict[str, list[dict]], entity: dict | None, text: str) -> None:
    if not entity or entity["confidence"] < MIN_CONFIDENCE:
        return
    entities.setdefault(entity["type"], []).append({
        "text": text[entity["start"]:entity["end"]],
        "confidence": entity["confidence"],
    })
