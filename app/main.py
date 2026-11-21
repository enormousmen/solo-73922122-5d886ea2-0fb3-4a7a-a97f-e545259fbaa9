"""FastAPI entry point for the cross-zone DNS release-order planner."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from . import __version__
from .models import ChangeTicket, PlanRequest, Snapshot
from .schemas import (
    HealthResponse,
    PlanResponse,
    RejectionResponse,
    SnapshotResponse,
    TicketAppliedResponse,
)
from .service import build_plan, resolution_json, snapshot_resolution
from .staging import (
    BeforeMismatch,
    CandidateZoneInvalid,
    NameOutsideZones,
    RequiredDomainsBroken,
    StagingStore,
    get_store,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Eagerly load (or seed) the staging snapshot so a missing/invalid seed or
    # a corrupt snapshot file fails startup instead of the first request.
    get_store()
    yield


app = FastAPI(
    title="跨区 DNS 变更发布顺序规划器",
    version=__version__,
    summary="为内网 DNS 值班员推算整区切换的安全顺序，并维护带校验的 DNS 变更暂存库",
    description=(
        "提交最多 8 个互不嵌套的权威区的新旧 A/CNAME 记录，以及必须持续可解析的"
        "业务域名。服务按无缓存权威记录推演每个整区切换后的状态：\n\n"
        "* 若存在安全顺序，返回逐步发布顺序，并附每一步前后的解析结果；\n"
        "* 若现状已无法解析，返回故障链路（缺记录 / 成环 / 链出权威区 / 链过长）；\n"
        "* 若无安全顺序，返回最大安全前缀，以及剩余每个区发布后会断在哪条解析链上。\n\n"
        "另提供变更暂存库：值班员可读取当前暂存快照，并按记录名提交改前/改后整组 "
        "A/CNAME 记录的变更单。暂存库首次启动从本地样例载入，重启后沿用已保存的快照；"
        "每张单在改前记录与当前快照一致、合入后通过区记录校验、且必保域名都能解析到 "
        "IPv4 时才整单落库，任一不满足则整单拒收。\n\n"
        "假设：每个区一次整区切换，区内没有新旧共存的中间态；解析无缓存；"
        "CNAME 链离开已提交的区即视为无法确认（可把外部区按新旧相同一并提交）。"
    ),
    lifespan=lifespan,
)


@app.get("/healthz", response_model=HealthResponse, summary="健康检查")
def healthz() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@app.post(
    "/plan",
    response_model=PlanResponse,
    response_model_exclude_none=True,
    summary="计算跨区发布顺序",
    responses={
        200: {"description": "推演完成，status 指明三种结果之一"},
        422: {"description": "请求校验失败（区/记录/域名不合法或超出限制）"},
    },
)
def plan(request: PlanRequest) -> dict[str, Any]:
    return build_plan(request)


def _snapshot_response(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "revision": snapshot.revision,
        "zones": snapshot.zones,
        "required_domains": snapshot.required_domains,
        **snapshot_resolution(snapshot),
    }


@app.get(
    "/staging/snapshot",
    response_model=SnapshotResponse,
    summary="读取当前暂存快照",
    responses={200: {"description": "当前暂存快照及必保域名的实时解析状态"}},
)
def read_staging_snapshot(
    store: StagingStore = Depends(get_store),
) -> dict[str, Any]:
    return _snapshot_response(store.get_snapshot())


@app.post(
    "/staging/tickets",
    response_model=TicketAppliedResponse,
    summary="提交 DNS 变更单",
    responses={
        200: {"description": "整单通过校验并已落库，返回最新快照"},
        409: {
            "description": "改前记录与暂存库不一致（冲突记录名），或合入后必保域名无法解析（故障链）；整单未落库",
            "model": RejectionResponse,
        },
        422: {
            "description": "变更单本身不合法，或合入后违反区记录校验；整单未落库",
        },
    },
)
def submit_ticket(
    ticket: ChangeTicket,
    store: StagingStore = Depends(get_store),
) -> dict[str, Any]:
    try:
        snapshot = store.apply_ticket(ticket)
    except BeforeMismatch as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "status": "rejected",
                "reason": exc.reason,
                "message": str(exc),
                "conflicting_names": [c["name"] for c in exc.conflicts],
                "conflicts": exc.conflicts,
            },
        ) from exc
    except NameOutsideZones as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "status": "rejected",
                "reason": exc.reason,
                "message": str(exc),
                "names": exc.names,
            },
        ) from exc
    except CandidateZoneInvalid as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "status": "rejected",
                "reason": exc.reason,
                "message": str(exc),
                "errors": exc.errors,
            },
        ) from exc
    except RequiredDomainsBroken as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "status": "rejected",
                "reason": exc.reason,
                "message": str(exc),
                "fault_chains": [resolution_json(f) for f in exc.failures],
            },
        ) from exc
    return {
        "status": "applied",
        "ticket_id": ticket.ticket_id,
        "revision": snapshot.revision,
        "applied_changes": [c.name for c in ticket.changes],
        "snapshot": _snapshot_response(snapshot),
    }
