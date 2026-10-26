"""Context receipts: signed proof that a scoped retrieval was served, never that a model read it.

BRAIN_RECEIPT_KEYS lists signing keys as ``version=secret`` pairs, comma separated, so a
secret cannot contain a comma;
BRAIN_RECEIPT_KEY_VERSION names the one that signs new receipts. A receipt keeps
verifying across restarts until it expires while its key version stays listed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from collections import Counter, OrderedDict
from collections.abc import AsyncIterator, Callable

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import scope

RECEIPT_VERSION = "1"
PROVES = "retrieval_and_delivery_not_comprehension"
ERROR_CLASSES = {
    "brain_audience": "forbidden_scope",
    "forbidden_scope": "forbidden_scope",
    "wrong_generation": "stale_generation",
    "wrong_task_revision": "revision_conflict",
    "stale_task_revision": "revision_conflict",
    "expired": "revision_conflict",
}

log = logging.getLogger("brain-api.receipts")
clock: Callable[[], float] = time.time
Latest = Callable[[str, str], int]

_failures: Counter[str] = Counter()
_acks: OrderedDict[tuple[str, str], dict] = OrderedDict()
_lock = threading.Lock()


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    execution_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=256)
    task_revision: int = Field(ge=1)


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    receipt: dict
    expect: Expect
    payload: dict | None = None


class DeliveryRequest(VerifyRequest):
    payload: dict
    adapter: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


def keyring() -> dict[str, bytes]:
    keys = {}
    for pair in os.getenv("BRAIN_RECEIPT_KEYS", "").split(","):
        version, sep, secret = pair.strip().partition("=")
        if sep and version.strip() and secret.strip():
            keys[version.strip()] = secret.strip().encode("utf-8")
    return keys


def ttl_ms() -> int:
    return int(float(os.getenv("BRAIN_RECEIPT_TTL_SECONDS", "3600")) * 1000)


def failures() -> dict[str, int]:
    with _lock:
        return dict(_failures)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def payload_digest(payload: object) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _sign(key: bytes, claims: dict) -> str:
    return hmac.new(key, _canonical(claims), hashlib.sha256).hexdigest()


def _reject(status: int, reason: str, error: str) -> HTTPException:
    with _lock:
        _failures[reason] += 1
        total = sum(_failures.values())
    log.warning("context_receipt_validation_failures=%d reason=%s", total, reason)
    return HTTPException(
        status_code=status,
        detail={
            "valid": False,
            "reason": reason,
            "error_class": ERROR_CLASSES.get(reason, "invalid_request"),
            "retry_safe": False,
            "error": error,
        },
    )


def issue(response: dict) -> dict | None:
    keys = keyring()
    version = os.getenv("BRAIN_RECEIPT_KEY_VERSION", "").strip()
    if version not in keys:
        return None
    request, context = response["request"], response.get("context") or {}
    pack = context.get("pack") or {}
    now = int(clock() * 1000)
    claims = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": f"rcpt-{uuid.uuid4().hex}",
        "brain_id": response["brain_id"],
        "project_ids": response["scope"]["project_ids"],
        "execution_id": request["execution_id"],
        "task_id": request["task_id"],
        "task_revision": request["task_revision"],
        "lane": request["lane"],
        "purpose": request["purpose"],
        "operation_id": response["operation_id"],
        "policy": {
            "scope_policy": scope.POLICY_REVISION,
            "ranking_policy": context.get("policy_revision"),
            "packing": (pack.get("degraded") or "full") if pack else None,
        },
        "knowledge_revision": pack.get("knowledge_revision"),
        "references": [
            {
                "entity_id": item["id"],
                "revision": item.get("revision"),
                "knowledge_status": item.get("knowledge_status"),
            }
            for item in context.get("items", [])
        ],
        "omitted": [
            {"entity_id": o["id"], "revision": o["reference"]["revision"], "reason": o["reason"]}
            for o in pack.get("omitted", [])
        ],
        "excluded": context.get("excluded", {}),
        "reduced": bool(context.get("reduced")),
        "payload_digest": payload_digest(response.get("context")),
        "issued_at_ms": now,
        "expires_at_ms": now + ttl_ms(),
        "key_version": version,
    }
    return {"claims": claims, "signature": _sign(keys[version], claims)}


async def read_bounded(chunks: AsyncIterator[bytes]) -> bytes:
    limit = int(os.getenv("BRAIN_RECEIPT_MAX_BODY_BYTES", "262144"))
    body = bytearray()
    async for chunk in chunks:
        body.extend(chunk)
        if len(body) > limit:
            raise _reject(413, "oversized_request", f"request exceeds {limit} bytes")
    return bytes(body)


def _parse(body: bytes, model: type[VerifyRequest]) -> VerifyRequest:
    try:
        raw = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise _reject(422, "invalid_request", "request body is not JSON") from exc
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise _reject(422, "invalid_request", "request fields are invalid") from exc


def _authenticate(receipt: dict) -> dict:
    keys = keyring()
    if not keys:
        raise HTTPException(
            status_code=503,
            detail={
                "valid": False,
                "reason": "receipts_not_configured",
                "error_class": "dependency_unavailable",
                "retry_safe": True,
                "error": "no keys",
            },
        )
    claims, signature = receipt.get("claims"), receipt.get("signature")
    if not isinstance(claims, dict) or not isinstance(signature, str):
        raise _reject(422, "invalid_request", "receipt needs claims and a signature")
    version = claims.get("key_version")
    key = keys.get(version) if isinstance(version, str) else None
    if key is None:
        raise _reject(409, "unknown_key_version", "receipt key version is not configured")
    if not hmac.compare_digest(_sign(key, claims).encode(), signature.encode("utf-8")):
        raise _reject(409, "signature_mismatch", "receipt was not issued as presented")
    return claims


def _authorize(claims: dict, grant: frozenset[str], deny: Callable[[], None]) -> None:
    if claims["brain_id"] != scope.BRAIN_ID:
        deny()
        raise _reject(403, "brain_audience", "receipt was issued by another brain")
    if not scope.admits(grant, set(claims["project_ids"]), unattributed=False):
        deny()
        raise _reject(403, "forbidden_scope", "receipt project is outside this token's grant")


def _bind(claims: dict, request: VerifyRequest, latest: Latest) -> None:
    if int(clock() * 1000) >= claims["expires_at_ms"]:
        raise _reject(409, "expired", "receipt has expired")
    expect = request.expect
    if claims["execution_id"] != expect.execution_id:
        raise _reject(409, "wrong_generation", "receipt belongs to another execution")
    if (claims["task_id"], claims["task_revision"]) != (expect.task_id, expect.task_revision):
        raise _reject(409, "wrong_task_revision", "receipt belongs to another task revision")
    if claims["task_revision"] < latest(claims["project_ids"][0], expect.task_id):
        raise _reject(409, "stale_task_revision", "a newer task revision was already accepted")
    if request.payload is not None and payload_digest(request.payload) != claims["payload_digest"]:
        raise _reject(409, "payload_mismatch", "payload differs from the issued context")


def _check(
    request: VerifyRequest, grant: frozenset[str], deny: Callable[[], None], latest: Latest
) -> dict:
    claims = _authenticate(request.receipt)
    _authorize(claims, grant, deny)
    _bind(claims, request, latest)
    return claims


def _resolve(store, references: list[dict], visible: Callable[[dict], bool]) -> list[dict]:
    resolved = []
    for reference in references:
        entity = None if store is None else store.get_entity(reference["entity_id"])
        if entity is None or not visible(entity):
            resolved.append({**reference, "availability": "unavailable"})
            continue
        stale = (
            entity["revision"] != reference["revision"]
            or reference["knowledge_status"] == "superseded"
        )
        resolved.append(
            {
                **reference,
                "availability": "available",
                "current_revision": entity["revision"],
                "stale": stale,
            }
        )
    return resolved


def _deliveries(receipt_id: str) -> list[dict]:
    with _lock:
        return [dict(row) for (rid, _), row in _acks.items() if rid == receipt_id]


def verify(
    body: bytes,
    grant: frozenset[str],
    store,
    visible: Callable[[dict], bool],
    deny: Callable[[], None],
    latest: Latest,
) -> dict:
    request = _parse(body, VerifyRequest)
    claims = _check(request, grant, deny, latest)
    references = _resolve(store, claims["references"], visible)
    return {
        "valid": True,
        "receipt_id": claims["receipt_id"],
        "payload_verified": request.payload is not None,
        "authority": "recalled_evidence",
        "proves": PROVES,
        "references": references,
        "explanation": {
            "omitted": dict(Counter(o["reason"] for o in claims["omitted"])),
            "excluded": claims["excluded"],
            "reduced": claims["reduced"],
            "stale": [r["entity_id"] for r in references if r.get("stale")],
            "unavailable": sum(r["availability"] == "unavailable" for r in references),
        },
        "deliveries": _deliveries(claims["receipt_id"]),
    }


def deliver(
    receipt_id: str,
    body: bytes,
    grant: frozenset[str],
    deny: Callable[[], None],
    latest: Latest,
) -> dict:
    request = _parse(body, DeliveryRequest)
    claims = request.receipt.get("claims")
    if not isinstance(claims, dict) or claims.get("receipt_id") != receipt_id:
        raise _reject(409, "receipt_path_mismatch", "path names another receipt")
    claims = _check(request, grant, deny, latest)
    key = (receipt_id, request.adapter)
    with _lock:
        stored = _acks.get(key)
        if stored is not None:
            return {**stored, "replayed": True}
        row = {
            "ack_id": "rack-"
            + hashlib.sha256(f"{receipt_id}\n{request.adapter}".encode()).hexdigest()[:32],
            "receipt_id": receipt_id,
            "adapter": request.adapter,
            "payload_digest": claims["payload_digest"],
            "acknowledged_at_ms": int(clock() * 1000),
        }
        _acks[key] = row
        while len(_acks) > int(os.getenv("BRAIN_RECEIPT_ACKS_KEPT", "10000")):
            evicted = _acks.popitem(last=False)[1]
            log.warning("context receipt delivery evicted ack_id=%s", evicted["ack_id"])
    return {**row, "replayed": False}
