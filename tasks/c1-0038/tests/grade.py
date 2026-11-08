#!/usr/bin/env python3
"""Grade one dementia-bench response.

Label weight is 0.7. Do not change that weight. The other 0.3 is the
HealthBench rubric score from an LLM judge.

The judge is LiteLLM against https://api.x.ai/v1 using XAI_API_KEY.
Grok models only. Claude is refused.

T1 scores gold.T1 with a Brier reward. T2 scores cdr_sb_change_2y and
decline. T3 scores A, T, N. T4 scores copathologies with Jaccard. T5
scores next_test by exact match after light normalization.

Research eval tool, not medical advice. The API key is never printed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
import sys
from typing import Any, Callable

# Keep this tuple identical to draft.py.
DIAGNOSES = (
    "AD",
    "DLB",
    "FTLD-tau",
    "FTLD-TDP",
    "vascular",
    "LATE",
    "mixed",
)
DIAGNOSIS_SET = set(DIAGNOSES)
TASK_TYPES = ("T1", "T2", "T3", "T4", "T5")

# Bench weight. Harbor and generate.py already use 0.7. Do not change it.
LABEL_WEIGHT = 0.7

API_BASE = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4.7"
VIGNETTE_CAP = 20000
SCORE_VERSION = "1"

Judge = Callable[[list[dict[str, str]]], str]


class GradeError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def clip01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


def round6(value: float | None) -> float | None:
    if value is None:
        return None
    return float(f"{value:.6f}")


def redact(text: str) -> str:
    key = os.environ.get("XAI_API_KEY") or ""
    if key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bxai-[A-Za-z0-9_\-]{8,}", "xai-[REDACTED]", text)
    return text


def load_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GradeError(f"{path} is not valid JSON: {exc}", 1) from exc
    except OSError as exc:
        raise GradeError(f"could not read {path}: {exc}", 1) from exc


def task_type_of(answer: dict) -> str:
    for key in ("task_type", "task"):
        value = answer.get(key)
        if isinstance(value, str) and value.strip().upper() in TASK_TYPES:
            return value.strip().upper()
    return "T1"


def gold_block(answer: dict, task: str) -> Any:
    gold = answer.get("gold")
    if isinstance(gold, dict) and task in gold:
        return gold[task]
    return None


def parse_hidden_label(raw: Any) -> tuple[str, list[str]]:
    contributing: list[str] = []
    if isinstance(raw, str):
        primary = raw.strip()
    elif isinstance(raw, dict):
        primary_raw = raw.get("primary")
        if not isinstance(primary_raw, str):
            raise GradeError("hidden_label.primary must be a diagnosis string", 1)
        primary = primary_raw.strip()
        extra = raw.get("contributing", []) or []
        if not isinstance(extra, list):
            raise GradeError("hidden_label.contributing must be a list", 1)
        for item in extra:
            if not isinstance(item, str) or item.strip() not in DIAGNOSIS_SET:
                raise GradeError(f"contributing label {item!r} is not a known diagnosis", 1)
            contributing.append(item.strip())
    else:
        raise GradeError("hidden_label must be a diagnosis string or {primary, contributing}", 1)
    if primary not in DIAGNOSIS_SET:
        raise GradeError(f"hidden_label {primary!r} is not one of {list(DIAGNOSES)}", 1)
    return primary, contributing


def _diagnosis_name(item: dict) -> str | None:
    for key in ("diagnosis", "name", "label"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _probability_value(item: dict) -> float | None:
    for key in ("probability", "p"):
        if key in item:
            value = item[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return float(value)
    return None


def parse_probabilities(response: Any) -> tuple[dict[str, float] | None, list[str], str]:
    rationale = ""
    if isinstance(response, dict):
        for key in ("rationale", "reasoning", "explanation"):
            value = response.get(key)
            if isinstance(value, str):
                rationale = value
                break
        payload = None
        for key in ("probabilities", "probability", "probs"):
            if key in response:
                payload = response[key]
                break
        if payload is None:
            for key in ("differential", "diagnoses", "ranked"):
                if isinstance(response.get(key), list):
                    payload = response[key]
                    break
    elif isinstance(response, str):
        return None, [], response
    else:
        return None, [], ""

    if not isinstance(payload, (dict, list)) or len(payload) == 0:
        return None, [], rationale

    raw: dict[str, float] = {}
    unexpected: list[str] = []
    if isinstance(payload, dict):
        for name, value in payload.items():
            if not isinstance(name, str):
                return None, unexpected, rationale
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, unexpected, rationale
            number = float(value)
            if number < 0.0 or number > 1.0 or math.isnan(number) or math.isinf(number):
                return None, unexpected, rationale
            if name not in DIAGNOSIS_SET:
                unexpected.append(name)
                continue
            raw[name] = number
    else:
        for item in payload:
            if not isinstance(item, dict):
                return None, unexpected, rationale
            name = _diagnosis_name(item)
            number = _probability_value(item)
            if name is None or number is None:
                return None, unexpected, rationale
            if number < 0.0 or number > 1.0 or math.isnan(number) or math.isinf(number):
                return None, unexpected, rationale
            if name not in DIAGNOSIS_SET:
                unexpected.append(name)
                continue
            raw[name] = number

    if not raw:
        return None, unexpected, rationale
    filled = {name: raw.get(name, 0.0) for name in DIAGNOSES}
    return filled, unexpected, rationale


def parse_distribution(raw: Any, *, fill_missing: bool) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    values: dict[str, float] = {}
    for key, value in raw.items():
        if key not in DIAGNOSIS_SET:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if number < 0.0 or number > 1.0 or math.isnan(number) or math.isinf(number):
            return None
        values[key] = number
    if not values:
        return None
    if fill_missing:
        for name in DIAGNOSES:
            values.setdefault(name, 0.0)
    elif any(name not in values for name in DIAGNOSES):
        return None
    return {name: values[name] for name in DIAGNOSES}


def brier_score(probabilities: dict[str, float], target: dict[str, float]) -> float:
    total = 0.0
    for name in DIAGNOSES:
        gap = probabilities[name] - target[name]
        total += gap * gap
    return total


def soft_log_loss(probabilities: dict[str, float], target: dict[str, float], eps: float) -> float:
    total = 0.0
    for name in DIAGNOSES:
        weight = target[name]
        if weight <= 0.0:
            continue
        probability = min(1.0, max(float(probabilities[name]), eps))
        total += weight * (-math.log(probability))
    return total


def log_reward(loss: float, eps: float) -> float:
    span = -math.log(eps)
    if span <= 0.0:
        return 0.0
    return clip01(1.0 - loss / span)


def overconfidence_penalty(
    probabilities: dict[str, float],
    target: dict[str, float],
    penalty: float,
    threshold: float,
) -> float:
    if penalty <= 0.0 or not 0.0 <= threshold < 1.0:
        return 0.0
    mode = max(DIAGNOSES, key=lambda name: target[name])
    max_p = max(probabilities.values())
    winners = [name for name in DIAGNOSES if math.isclose(probabilities[name], max_p, abs_tol=1e-6)]
    if mode in winners or max_p < threshold:
        return 0.0
    return penalty * (max_p - threshold) / (1.0 - threshold)


def t1_components(
    probabilities: dict[str, float] | None,
    target: dict[str, float] | None,
    *,
    label_rule: str,
    brier_weight: float,
    log_eps: float,
    overconfidence: float,
    confidence_threshold: float,
) -> dict[str, Any]:
    empty = {
        "brier": None,
        "brier_reward": 0.0,
        "log_loss": None,
        "log_reward": 0.0,
        "label_core": 0.0,
        "overconfidence_penalty_applied": 0.0,
        "label_score": 0.0,
        "probability_sum": None,
        "label_rule": label_rule,
    }
    if probabilities is None or target is None:
        return empty
    brier = brier_score(probabilities, target)
    brier_r = clip01(1.0 - brier / 2.0)
    loss = soft_log_loss(probabilities, target, log_eps)
    log_r = log_reward(loss, log_eps)
    if label_rule == "brier":
        core = brier_r
    elif label_rule == "log":
        core = log_r
    elif label_rule == "mean":
        core = brier_weight * brier_r + (1.0 - brier_weight) * log_r
    else:
        raise GradeError(f"unknown label rule {label_rule}", 1)
    applied = overconfidence_penalty(probabilities, target, overconfidence, confidence_threshold)
    empty.update(
        {
            "brier": brier,
            "brier_reward": brier_r,
            "log_loss": loss,
            "log_reward": log_r,
            "label_core": core,
            "overconfidence_penalty_applied": applied,
            "label_score": clip01(core - applied),
            "probability_sum": sum(probabilities.values()),
        }
    )
    return empty


def _lookup(response: Any, key: str, task: str) -> Any:
    if not isinstance(response, dict):
        return None
    if key in response:
        return response[key]
    for nest in (task, "answer", "prediction", "outputs"):
        block = response.get(nest)
        if isinstance(block, dict) and key in block:
            return block[key]
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _as_decline(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"decline", "declining", "true", "yes", "y", "1"}:
            return True
        if token in {"stable", "improved", "improve", "false", "no", "n", "0"}:
            return False
    return None


def t2_score(response: Any, gold: Any) -> tuple[float, dict[str, Any]]:
    """Partial credit on the numeric change. Exact match on decline.

    The numeric denominator is max(1, |gold change|). That is a grader
    scale so the part stays in [0, 1]. It is not a published MCID.
    """
    detail: dict[str, Any] = {}
    if not isinstance(gold, dict):
        return 0.0, detail
    parts: list[float] = []
    if "cdr_sb_change_2y" in gold:
        target = _as_float(gold.get("cdr_sb_change_2y"))
        pred = _as_float(_lookup(response, "cdr_sb_change_2y", "T2"))
        if target is None or pred is None:
            parts.append(0.0)
            detail["cdr_sb_change_2y"] = 0.0
        else:
            denom = max(1.0, abs(target))
            score = clip01(1.0 - abs(pred - target) / denom)
            parts.append(score)
            detail["cdr_sb_change_2y"] = score
    if "decline" in gold:
        pred_flag = _as_decline(_lookup(response, "decline", "T2"))
        gold_flag = _as_decline(gold.get("decline"))
        score = 1.0 if pred_flag is not None and pred_flag == gold_flag else 0.0
        parts.append(score)
        detail["decline"] = score
    if not parts:
        return 0.0, detail
    return sum(parts) / len(parts), detail


def _norm_atn(value: Any) -> str | None:
    if isinstance(value, bool):
        return "positive" if value else "negative"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return "positive"
        if value == 0:
            return "negative"
        return None
    if not isinstance(value, str):
        return None
    token = value.strip().lower().replace(" ", "")
    if len(token) == 2 and token[0] in "atn" and token[1] in "+-":
        token = token[1]
    if token in {"+", "positive", "pos", "abnormal", "true", "yes", "1"}:
        return "positive"
    if token in {"-", "negative", "neg", "normal", "false", "no", "0"}:
        return "negative"
    if token in {"unknown", "u", "indeterminate", "na", "n/a"}:
        return "unknown"
    return None


def t3_score(response: Any, gold: Any) -> tuple[float, dict[str, Any]]:
    detail: dict[str, Any] = {}
    if not isinstance(gold, dict):
        return 0.0, detail
    keys = [key for key in ("A", "T", "N") if key in gold]
    if not keys:
        return 0.0, detail
    hits = 0
    for key in keys:
        pred = _norm_atn(_lookup(response, key, "T3"))
        target = _norm_atn(gold.get(key))
        matched = pred is not None and pred == target
        detail[key] = 1.0 if matched else 0.0
        hits += int(matched)
    return hits / len(keys), detail


def _label_set(value: Any) -> set[str] | None:
    if isinstance(value, dict) and "copathologies" in value:
        value = value.get("copathologies")
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return None
    found: set[str] = set()
    for item in value:
        if isinstance(item, str) and item.strip() in DIAGNOSIS_SET:
            found.add(item.strip())
    return found


def t4_score(response: Any, gold: Any) -> tuple[float, dict[str, Any]]:
    target = _label_set(gold)
    pred = _label_set(_lookup(response, "copathologies", "T4"))
    if target is None:
        return 0.0, {}
    if pred is None:
        pred = set()
    union = target | pred
    if not union:
        score = 1.0
    else:
        score = len(target & pred) / len(union)
    return score, {"jaccard": score, "gold": sorted(target), "response": sorted(pred)}


def _norm_test(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip().lower().replace("-", " ").replace("/", " ")
    token = "_".join(token.split())
    return token or None


def t5_score(response: Any, gold: Any) -> tuple[float, dict[str, Any]]:
    if isinstance(gold, str):
        target_raw = gold
    elif isinstance(gold, dict):
        target_raw = gold.get("next_test", gold.get("recommended_test"))
    else:
        target_raw = None
    target = _norm_test(target_raw)
    pred = _norm_test(_lookup(response, "next_test", "T5"))
    if pred is None:
        pred = _norm_test(_lookup(response, "recommended_test", "T5"))
    score = 1.0 if target is not None and pred == target else 0.0
    return score, {"next_test": score}


def rubric_score(criteria: list[dict], met_by_id: dict[str, bool]) -> dict[str, Any]:
    earned = 0.0
    positive = 0.0
    rows = []
    axes: dict[str, dict[str, float]] = {}
    unjudged: list[str] = []
    for criterion in criteria:
        points = float(criterion["points"])
        met = met_by_id.get(criterion["id"])
        if met is None:
            met = False
            unjudged.append(criterion["id"])
        awarded = points if met else 0.0
        earned += awarded
        if points > 0.0:
            positive += points
        axis = criterion.get("axis") or "unspecified"
        bucket = axes.setdefault(axis, {"earned": 0.0, "positive_points": 0.0})
        bucket["earned"] += awarded
        if points > 0.0:
            bucket["positive_points"] += points
        rows.append(
            {
                "id": criterion["id"],
                "met": bool(met),
                "points": criterion["points"],
                "points_awarded": awarded,
                "axis": axis,
            }
        )
    if not criteria:
        score = 0.0
    elif positive == 0.0 and earned >= 0.0:
        score = 1.0
    elif positive == 0.0:
        score = 0.0
    else:
        score = clip01(earned / positive)
    axis_scores = {}
    for axis, bucket in axes.items():
        denom = bucket["positive_points"]
        if denom > 0.0:
            axis_score = clip01(bucket["earned"] / denom)
        elif bucket["earned"] >= 0.0:
            axis_score = 1.0
        else:
            axis_score = 0.0
        axis_scores[axis] = {
            "earned": round6(bucket["earned"]),
            "positive_points": round6(denom),
            "score": round6(axis_score),
        }
    return {
        "rubric_score": score,
        "rubric_earned": earned,
        "rubric_positive_points": positive,
        "criteria": rows,
        "axes": axis_scores,
        "unjudged_criterion_ids": unjudged,
        "empty_rubric": not criteria,
    }


def combine_reward(label_score: float, rubric: float, label_weight: float, length_applied: float) -> tuple[float, float]:
    rubric_adj = clip01(rubric - length_applied)
    reward = clip01(label_weight * label_score + (1.0 - label_weight) * rubric_adj)
    return rubric_adj, reward


def assert_grok_model(model: str) -> str:
    raw = model.strip()
    lowered = raw.lower()
    if not raw:
        raise GradeError("model id is empty", 2)
    banned = ("claude", "anthropic", "openai/", "gpt-", "gemini", "sonnet", "opus", "haiku")
    if any(token in lowered for token in banned):
        raise GradeError("Refusing this model. The grader calls Grok through xAI only.", 2)
    if lowered.startswith("xai/"):
        raw = raw.split("/", 1)[1]
        lowered = raw.lower()
    if "/" in raw:
        raise GradeError("Pass a bare Grok id such as grok-4.7, not a provider prefix other than xai/.", 2)
    if not lowered.startswith("grok"):
        raise GradeError("Model id must be a Grok model.", 2)
    return raw


def extract_json(text: str) -> Any:
    body = text.strip()
    if body.startswith("```"):
        lines = body.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        body = "\n".join(lines).strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        start = body.find("{")
        end = body.rfind("}")
        if start >= 0 and end > start:
            return json.loads(body[start : end + 1])
        start = body.find("[")
        end = body.rfind("]")
        if start >= 0 and end > start:
            return json.loads(body[start : end + 1])
        raise


def _met_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "yes", "met", "y", "present"}:
            return True
        if token in {"false", "no", "unmet", "n", "absent", "not met"}:
            return False
    raise ValueError(f"unrecognized met value {value!r}")


def judgments_from_payload(payload: Any) -> dict[str, bool]:
    if isinstance(payload, dict):
        rows = payload.get("results", payload.get("criteria", payload.get("judgments")))
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("judge JSON has no results list")
    found: dict[str, bool] = {}
    for row in rows:
        if not isinstance(row, dict) or "id" not in row:
            continue
        found[str(row["id"])] = _met_flag(row.get("met"))
    if not found:
        raise ValueError("judge JSON contained no criterion ids")
    return found


def judge_messages(
    criteria: list[dict],
    probabilities: dict[str, float] | None,
    rationale: str,
    vignette: str,
    length_cap: int,
    task: str,
    response: Any,
) -> list[dict[str, str]]:
    shown = rationale if len(rationale) <= length_cap else rationale[:length_cap]
    truncated = len(rationale) > length_cap
    task_fields = None
    if isinstance(response, dict):
        keys = {
            "T1": ("probabilities",),
            "T2": ("cdr_sb_change_2y", "decline"),
            "T3": ("A", "T", "N"),
            "T4": ("copathologies",),
            "T5": ("next_test", "recommended_test"),
        }.get(task, ())
        task_fields = {}
        for key in keys:
            if key in response:
                task_fields[key] = response[key]
        nested = response.get(task)
        if isinstance(nested, dict):
            task_fields.update(nested)
    user = {
        "task_type": task,
        "vignette": vignette if vignette else None,
        "vignette_missing": not bool(vignette),
        "probabilities": probabilities,
        "task_fields": task_fields,
        "rationale": shown,
        "rationale_truncated_to_chars": length_cap if truncated else None,
        "criteria": [{"id": item["id"], "text": item["text"]} for item in criteria],
    }
    system = (
        "You grade a model response against written criteria for a research benchmark. "
        "Judge only the response and the vignette in the user message. "
        "For each criterion, set met to true only when the criterion text is true of the response. "
        "When the criterion describes an unwanted action, met is true only if the response does that action. "
        "If the criterion says to apply a penalty when the response does something, met is true only when the response does that thing. "
        "If the criterion says 'Do not' do something, met is true only when the response follows that instruction. "
        "Do not give medical advice. Do not add or drop criteria. "
        'Reply with one JSON object and no other text: {"results":[{"id":"<criterion id>","met":true}]}'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        content = "".join(parts)
    if isinstance(content, str) and content.strip():
        return content
    reasoning = getattr(message, "reasoning_content", None)
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    return ""


def call_litellm(model_id: str, messages: list[dict[str, str]], max_tokens: int) -> str:
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise GradeError("XAI_API_KEY is not set", 2)
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    import logging

    logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        import litellm
    except ImportError as exc:
        raise GradeError("litellm is not installed", 2) from exc
    litellm.suppress_debug_info = True
    litellm.turn_off_message_logging = True
    request = {
        "model": f"xai/{model_id}",
        "messages": messages,
        "temperature": 0,
        "max_tokens": max(256, min(4096, max_tokens)),
        "api_key": key,
        "api_base": API_BASE,
        "timeout": 90,
        "num_retries": 0,
    }
    try:
        response = litellm.completion(**request)
    except TypeError:
        request.pop("num_retries", None)
        try:
            response = litellm.completion(**request)
        except Exception as exc:
            raise GradeError(redact(f"grader call failed: {exc}"), 3) from None
    except GradeError:
        raise
    except Exception as exc:
        raise GradeError(redact(f"grader call failed: {exc}"), 3) from None
    try:
        content = _message_text(response.choices[0].message)
    except Exception as exc:
        raise GradeError(redact(f"grader response had no content: {exc}"), 3) from None
    if not content.strip():
        raise GradeError("grader returned an empty message", 3)
    return content


def resolve_vignette(answer: dict, answer_path: pathlib.Path) -> tuple[str, bool]:
    raw = answer.get("vignette_path")
    if not isinstance(raw, str) or not raw.strip():
        return "", False
    path = pathlib.Path(raw)
    candidates = [path]
    if not path.is_absolute():
        candidates = [answer_path.parent / path, pathlib.Path.cwd() / path]
    for candidate in candidates:
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            if len(text) > VIGNETTE_CAP:
                text = text[:VIGNETTE_CAP]
            return text, True
    return "", False


def load_criteria(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("criteria"), list):
        rows = payload["criteria"]
    else:
        raise GradeError("rubric.json must be an object with criteria or a list", 1)
    criteria = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise GradeError(f"criterion {index} is not an object", 1)
        if not isinstance(row.get("id"), str) or not row["id"].strip():
            raise GradeError(f"criterion {index} is missing id", 1)
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            text = row.get("criterion")
        if not isinstance(text, str) or not text.strip():
            raise GradeError(f"criterion {row.get('id')} is missing text", 1)
        points = row.get("points")
        if isinstance(points, bool) or not isinstance(points, (int, float)):
            raise GradeError(f"criterion {row['id']} points must be a number", 1)
        criterion_id = row["id"].strip()
        if criterion_id in seen:
            raise GradeError(f"duplicate criterion id {criterion_id}", 1)
        seen.add(criterion_id)
        item = {
            "id": criterion_id,
            "text": text.strip(),
            "points": int(points) if isinstance(points, float) and points.is_integer() else points,
            "axis": row.get("axis") if isinstance(row.get("axis"), str) else "unspecified",
        }
        if isinstance(row.get("citation"), str) and row["citation"].strip():
            item["citation"] = row["citation"].strip()
        criteria.append(item)
    return criteria


def resolve_hidden_label(answer: dict) -> tuple[str | None, list[str]]:
    if "hidden_label" in answer and answer.get("hidden_label") not in (None, ""):
        primary, contributing = parse_hidden_label(answer.get("hidden_label"))
        return primary, contributing
    if isinstance(answer.get("primary"), str):
        extra = answer.get("contributing", [])
        return parse_hidden_label({"primary": answer["primary"], "contributing": extra})
    return None, []


def _one_hot(label: str) -> dict[str, float]:
    return {name: (1.0 if name == label else 0.0) for name in DIAGNOSES}


def label_for_task(
    task: str,
    response: Any,
    answer: dict,
    probabilities: dict[str, float] | None,
    *,
    label_rule: str,
    brier_weight: float,
    log_eps: float,
    overconfidence: float,
    confidence_threshold: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    gold = gold_block(answer, task)
    detail: dict[str, Any] = {"task_type": task, "gold_present": gold is not None}
    if task == "T1":
        vector = None
        if isinstance(gold, dict):
            if isinstance(gold.get("label_probs"), dict):
                vector = gold["label_probs"]
            elif isinstance(gold.get("probabilities"), dict):
                vector = gold["probabilities"]
            else:
                vector = gold
        target = parse_distribution(vector, fill_missing=True) if isinstance(vector, dict) else None
        source = "gold.T1"
        if target is None:
            primary, _extra = resolve_hidden_label(answer)
            if primary is None:
                raise GradeError("T1 needs gold.T1 probabilities or hidden_label", 1)
            target = _one_hot(primary)
            source = "hidden_label"
        detail["label_target"] = source
        parts = t1_components(
            probabilities,
            target,
            label_rule=label_rule,
            brier_weight=brier_weight,
            log_eps=log_eps,
            overconfidence=overconfidence,
            confidence_threshold=confidence_threshold,
        )
        return parts, detail
    if task == "T2":
        score, detail_task = t2_score(response, gold)
        detail.update(detail_task)
    elif task == "T3":
        score, detail_task = t3_score(response, gold)
        detail.update(detail_task)
    elif task == "T4":
        score, detail_task = t4_score(response, gold)
        detail.update(detail_task)
    elif task == "T5":
        score, detail_task = t5_score(response, gold)
        detail.update(detail_task)
    else:
        score = 0.0
    return {
        "brier": None,
        "brier_reward": None,
        "log_loss": None,
        "log_reward": None,
        "label_core": score,
        "overconfidence_penalty_applied": 0.0,
        "label_score": clip01(score),
        "probability_sum": None,
        "label_rule": "task",
    }, detail


def length_penalty_amount(n_chars: int, cap: int, weight: float) -> float:
    if weight <= 0.0 or cap <= 0 or n_chars <= cap:
        return 0.0
    return float(weight)


def grade_case(
    response: Any,
    answer: dict,
    criteria: list[dict],
    vignette: str,
    *,
    label_rule: str,
    label_weight: float,
    brier_weight: float,
    log_eps: float,
    overconfidence: float,
    confidence_threshold: float,
    length_cap: int,
    length_penalty: float,
    probability_tol: float,
    judge: Judge | None,
    model_id: str,
) -> dict[str, Any]:
    task = task_type_of(answer)
    hidden, contributing = resolve_hidden_label(answer)
    probabilities, unexpected, rationale = parse_probabilities(response)
    parts, detail = label_for_task(
        task,
        response,
        answer,
        probabilities,
        label_rule=label_rule,
        brier_weight=brier_weight,
        log_eps=log_eps,
        overconfidence=overconfidence,
        confidence_threshold=confidence_threshold,
    )
    n_chars = len(rationale)
    length_applied = length_penalty_amount(n_chars, length_cap, length_penalty)
    call_judge = label_weight < 1.0 and bool(criteria)
    met: dict[str, bool] = {}
    if call_judge:
        if judge is None:
            raise GradeError("internal: judge callable is required", 3)
        batch_size = 12
        for start in range(0, len(criteria), batch_size):
            batch = criteria[start : start + batch_size]
            messages = judge_messages(batch, probabilities, rationale, vignette, length_cap, task, response)
            last_error: Exception | None = None
            parsed: dict[str, bool] | None = None
            for _attempt in range(2):
                try:
                    parsed = judgments_from_payload(extract_json(judge(messages)))
                    last_error = None
                    break
                except GradeError:
                    raise
                except Exception as exc:
                    last_error = exc
            if last_error is not None or parsed is None:
                raise GradeError(redact(f"grader output was not usable JSON: {last_error}"), 4) from None
            met.update(parsed)
    aggregated = rubric_score(criteria, met)
    if not call_judge and label_weight >= 1.0:
        rubric_for_reward = 0.0
        length_for_reward = 0.0
    else:
        rubric_for_reward = float(aggregated["rubric_score"])
        length_for_reward = length_applied
    rubric_adj, reward = combine_reward(
        float(parts["label_score"]),
        rubric_for_reward,
        label_weight,
        length_for_reward,
    )
    probability_sum = parts["probability_sum"]
    sum_ok = probability_sum is not None and abs(float(probability_sum) - 1.0) <= probability_tol
    reported_rule = parts["label_rule"] if task == "T1" else parts["label_rule"]
    scores: dict[str, Any] = {
        "score_version": SCORE_VERSION,
        "task_type": task,
        "reward": round6(reward),
        "label_score": round6(float(parts["label_score"])),
        "label_rule": reported_rule if task == "T1" else "task",
        "label_weight": label_weight,
        "label_detail": detail,
        "brier_weight": brier_weight if task == "T1" and label_rule == "mean" else None,
        "brier": round6(parts["brier"]) if parts["brier"] is not None else None,
        "brier_reward": round6(float(parts["brier_reward"])) if parts["brier_reward"] is not None else None,
        "log_loss": round6(parts["log_loss"]) if parts["log_loss"] is not None else None,
        "log_reward": round6(float(parts["log_reward"])) if parts["log_reward"] is not None else None,
        "log_eps": log_eps,
        "label_core": round6(float(parts["label_core"])),
        "overconfidence_penalty": overconfidence,
        "confidence_threshold": confidence_threshold,
        "overconfidence_penalty_applied": round6(float(parts["overconfidence_penalty_applied"])),
        "rubric_score": None if label_weight >= 1.0 and not call_judge else round6(float(aggregated["rubric_score"])),
        "rubric_score_after_length": None if label_weight >= 1.0 else round6(rubric_adj),
        "rubric_earned": round6(float(aggregated["rubric_earned"])) if call_judge or criteria else 0.0,
        "rubric_positive_points": round6(float(aggregated["rubric_positive_points"])),
        "length_cap": length_cap,
        "length_penalty": length_penalty,
        "length_penalty_applied": round6(length_applied if label_weight < 1.0 else 0.0),
        "rationale_chars": n_chars,
        "probabilities_valid": probabilities is not None,
        "probability_sum": round6(float(probability_sum)) if probability_sum is not None else None,
        "probability_sum_ok": bool(sum_ok),
        "unexpected_diagnoses": unexpected,
        "hidden_label": hidden,
        "contributing_labels": contributing,
        "model": model_id,
        "judge_called": call_judge,
        "empty_rubric": aggregated["empty_rubric"],
        "unjudged_criterion_ids": aggregated["unjudged_criterion_ids"] if call_judge else [],
        "axes": aggregated["axes"] if call_judge else {},
        "criteria": aggregated["criteria"] if call_judge else [],
    }
    harbor = {
        key: scores[key]
        for key in (
            "reward",
            "label_score",
            "brier",
            "brier_reward",
            "log_loss",
            "log_reward",
            "overconfidence_penalty_applied",
            "rubric_score",
            "rubric_earned",
            "rubric_positive_points",
            "length_penalty_applied",
            "rationale_chars",
            "label_weight",
        )
        if isinstance(scores[key], (int, float)) and not isinstance(scores[key], bool)
    }
    scores["harbor_metrics"] = {key: round6(float(value)) for key, value in harbor.items()}
    return scores


def _read_response(path: pathlib.Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Grade a dementia-bench response against gold and a rubric")
    parser.add_argument("--response", required=True, help="Model answer JSON")
    parser.add_argument("--answer", required=True, help="answer.json with gold and task_type")
    parser.add_argument("--rubric", required=True, help="rubric.json")
    parser.add_argument("--out", required=True, help="scores.json path")
    parser.add_argument("--model", default=os.environ.get("DEMENTIA_BENCH_GRADER_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--label-weight",
        type=float,
        default=LABEL_WEIGHT,
        help="Weight w on the label score. The bench value is 0.7. Rubric weight is 1-w.",
    )
    parser.add_argument("--label-rule", choices=("brier", "log", "mean"), default="brier")
    parser.add_argument("--brier-weight", type=float, default=0.5, help="Used only when --label-rule mean on T1")
    parser.add_argument("--overconfidence-penalty", type=float, default=0.0)
    parser.add_argument("--confidence-threshold", type=float, default=0.80)
    parser.add_argument("--length-cap", type=int, default=4000)
    parser.add_argument("--length-penalty", type=float, default=0.0)
    parser.add_argument("--log-eps", type=float, default=1e-3)
    parser.add_argument("--probability-tol", type=float, default=0.02)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not 0.0 <= args.label_weight <= 1.0:
        raise GradeError("--label-weight must be in [0, 1]", 1)
    if not math.isclose(args.label_weight, LABEL_WEIGHT) and args.label_weight != 1.0:
        # 1.0 skips the judge for a label-only check. Any other value is a
        # different bench weight. The bench weight stays 0.7.
        raise GradeError(f"--label-weight stays {LABEL_WEIGHT}", 1)
    if not 0.0 <= args.brier_weight <= 1.0:
        raise GradeError("--brier-weight must be in [0, 1]", 1)
    if args.overconfidence_penalty < 0.0:
        raise GradeError("--overconfidence-penalty must be >= 0", 1)
    if not 0.0 <= args.confidence_threshold < 1.0:
        raise GradeError("--confidence-threshold must be in [0, 1)", 1)
    if args.length_cap < 1:
        raise GradeError("--length-cap must be >= 1", 1)
    if args.length_penalty < 0.0:
        raise GradeError("--length-penalty must be >= 0", 1)
    if not 0.0 < args.log_eps < 1.0:
        raise GradeError("--log-eps must be in (0, 1)", 1)
    if args.probability_tol < 0.0:
        raise GradeError("--probability-tol must be >= 0", 1)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        model_id = assert_grok_model(args.model)
        answer_path = pathlib.Path(args.answer)
        answer = load_json(answer_path)
        if not isinstance(answer, dict):
            raise GradeError("answer.json must be an object", 1)
        criteria = load_criteria(load_json(pathlib.Path(args.rubric)))
        response = _read_response(pathlib.Path(args.response))
        vignette, _found = resolve_vignette(answer, answer_path)
        judge = None
        if args.label_weight < 1.0 and criteria:
            output_tokens = min(4096, 200 + 80 * len(criteria))

            def judge(messages: list[dict[str, str]], _model: str = model_id, _tokens: int = output_tokens) -> str:
                return call_litellm(_model, messages, _tokens)

        scores = grade_case(
            response,
            answer,
            criteria,
            vignette,
            label_rule=args.label_rule,
            label_weight=args.label_weight,
            brier_weight=args.brier_weight,
            log_eps=args.log_eps,
            overconfidence=args.overconfidence_penalty,
            confidence_threshold=args.confidence_threshold,
            length_cap=args.length_cap,
            length_penalty=args.length_penalty,
            probability_tol=args.probability_tol,
            judge=judge,
            model_id=model_id,
        )
    except GradeError as exc:
        print(redact(str(exc)), file=sys.stderr)
        return exc.code
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(scores, indent=2, ensure_ascii=False) + "\n"
    key = os.environ.get("XAI_API_KEY") or ""
    if key and key in text:
        print("refusing to write scores that contain the API key", file=sys.stderr)
        return 3
    out.write_text(text, encoding="utf-8")
    print(json.dumps({"reward": scores["reward"], "out": str(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
